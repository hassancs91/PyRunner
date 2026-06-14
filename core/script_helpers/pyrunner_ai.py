"""
PyRunner Claude AI helper for scripts.

Run AI directly inside your scripts using the Claude account you configured in
PyRunner (Services -> Claude AI). It reuses your Claude subscription (via a
Claude Code OAuth token) or an Anthropic API key -- whichever you set there --
so you don't manage credentials per script. Web search and web fetch are
enabled by default.

Under the hood this wraps the official `claude-agent-sdk` and exposes a simple
*synchronous* API, so you can use it from ordinary (non-async) scripts.

Requirements
------------
1. Configure and enable Claude in PyRunner: Services -> Claude AI.
2. Install the SDK into this script's Environment:
   Environments -> (your env) -> Packages -> install ``claude-agent-sdk``.
   (The Claude Code CLI itself ships with the PyRunner Docker image.)

Usage
-----
    from pyrunner_ai import ask_claude

    # One-shot question. Web search + fetch are on by default.
    answer = ask_claude("What is the latest stable Python version? Search the web.")
    print(answer)

    # Only allow web fetch (no search):
    summary = ask_claude("Summarize https://peps.python.org/pep-0008/", tools=["WebFetch"])

    # Pick a model and add a system prompt:
    tweet = ask_claude(
        "Write a punchy tweet about today's top AI story.",
        system_prompt="You are a concise social media writer.",
        model="claude-sonnet-4-6",
    )

    # Need details (tools used, cost, turns)? Ask for the full result:
    result = ask_claude("Research the latest Django release.", raw=True)
    print(result.text)
    print("tools used:", result.tools_used, "cost $:", result.cost_usd)

    # Stream the answer as it is generated:
    from pyrunner_ai import stream_claude
    for chunk in stream_claude("Write a short poem about automation."):
        print(chunk, end="", flush=True)

Available tools you can pass to ``tools=``: "WebSearch", "WebFetch", "Read",
"Glob", "Grep". File-writing and shell tools (Write, Edit, Bash) are NOT enabled
by default for safety -- scripts run with full access to the PyRunner host, so
only enable those if you fully trust the prompt.
"""

import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

# Tools enabled when the caller does not specify any. Read-only / web only.
DEFAULT_TOOLS: List[str] = ["WebSearch", "WebFetch"]

# "dontAsk" auto-runs anything in allowed_tools and *denies* everything else
# without ever prompting -- the correct mode for headless, unattended use.
_PERMISSION_MODE = "dontAsk"

# Default cap on agent loop iterations, to avoid runaway cost/time.
_DEFAULT_MAX_TURNS = 12


class ClaudeNotConfiguredError(RuntimeError):
    """Raised when no Claude credentials are available to the script."""


class ClaudeSDKMissingError(RuntimeError):
    """Raised when the claude-agent-sdk package is not installed in this env."""


@dataclass
class ClaudeResult:
    """Full result of a Claude run."""

    text: str = ""
    tools_used: List[str] = field(default_factory=list)
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    model: str = ""
    duration_ms: int = 0
    is_error: bool = False
    error: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def __str__(self) -> str:  # so print(result) shows the answer
        return self.text


def _has_credentials() -> bool:
    return bool(
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


def _import_sdk():
    try:
        import claude_agent_sdk as sdk  # noqa: WPS433 (runtime import by design)
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ClaudeSDKMissingError(
            "The 'claude-agent-sdk' package is not installed in this script's "
            "Environment. Install it in PyRunner under "
            "Environments -> Packages (add 'claude-agent-sdk')."
        ) from exc
    return sdk


def _build_options(
    sdk,
    *,
    tools: Sequence[str],
    system_prompt: Optional[str],
    model: Optional[str],
    max_turns: int,
    cwd: Optional[str],
    env: Optional[dict],
    lean: bool = False,
):
    kwargs = {
        "allowed_tools": list(tools),
        "permission_mode": _PERMISSION_MODE,
        # Don't load any user/project/local CLI settings -- stay isolated and
        # predictable regardless of what is on the host.
        "setting_sources": [],
        "max_turns": max_turns,
    }
    if lean:
        # Restrict the tool *definitions* sent to the model to just what we
        # allow. Without this, the CLI loads ALL built-in tool schemas (~50k
        # cached tokens of agent overhead). With it, only these tools are
        # defined, which slashes the cached context (and is tighter, since the
        # model can't reference tools it was never told about).
        kwargs["tools"] = list(tools)
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    if model:
        kwargs["model"] = model
    if cwd:
        kwargs["cwd"] = cwd
    if env:
        kwargs["env"] = env
    return sdk.ClaudeAgentOptions(**kwargs)


def _summarize_error(message) -> str:
    """Build a useful error string from an errored ResultMessage.

    `errors` is often empty; when an API call fails the SDK sets is_error=True
    with subtype="success" and the HTTP status in api_error_status.
    """
    parts = []
    subtype = getattr(message, "subtype", None)
    if subtype and subtype != "success":
        parts.append(str(subtype))
    api_error = getattr(message, "api_error_status", None)
    if api_error:
        parts.append(f"API error (HTTP {api_error})")
    errs = getattr(message, "errors", None)
    if errs:
        parts.append(str(errs))
    denials = getattr(message, "permission_denials", None)
    if denials:
        parts.append(f"permission denied: {denials}")
    return "; ".join(parts) or "unknown error"


async def _drive(sdk, prompt, options, on_text=None):
    """Run a query to completion, returning a ClaudeResult.

    If ``on_text`` is given it is called with each chunk of assistant text as it
    arrives (used for streaming).
    """
    result = ClaudeResult()
    text_parts: List[str] = []

    try:
        async for message in sdk.query(prompt=prompt, options=options):
            cls_name = type(message).__name__

            if cls_name == "AssistantMessage":
                model = getattr(message, "model", "") or ""
                if model:
                    result.model = model
                for block in getattr(message, "content", []) or []:
                    block_name = type(block).__name__
                    if block_name == "TextBlock":
                        chunk = getattr(block, "text", "") or ""
                        if chunk:
                            text_parts.append(chunk)
                            if on_text:
                                on_text(chunk)
                    elif block_name in ("ToolUseBlock", "ServerToolUseBlock"):
                        name = getattr(block, "name", "")
                        if name and name not in result.tools_used:
                            result.tools_used.append(name)

            elif cls_name == "ResultMessage":
                result.cost_usd = getattr(message, "total_cost_usd", None)
                result.num_turns = getattr(message, "num_turns", None)
                result.is_error = bool(getattr(message, "is_error", False))
                result.duration_ms = getattr(message, "duration_ms", 0) or 0
                usage = getattr(message, "usage", None) or {}
                result.input_tokens = int(usage.get("input_tokens", 0) or 0)
                result.output_tokens = int(usage.get("output_tokens", 0) or 0)
                result.cache_creation_tokens = int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                )
                result.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
                # Prefer the SDK's final result text when present.
                final = getattr(message, "result", None)
                if final:
                    result.text = final
                if result.is_error:
                    result.error = _summarize_error(message)
    except Exception as exc:
        # The CLI exits non-zero after an error result, so the SDK raises a
        # trailing ProcessError on the iteration after the ResultMessage. Keep
        # the structured error we already captured; only fall back to raw text.
        if not result.error:
            result.error = str(exc)
        result.is_error = True

    if not result.text:
        result.text = "".join(text_parts)
    return result


def _run_coro(coro):
    """Run an async coroutine from sync code, even if a loop is already running."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # We're inside an existing event loop (rare for PyRunner scripts): run the
    # coroutine in a dedicated thread with its own loop.
    box = {}

    def _worker():
        box["result"] = asyncio.run(coro)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box["result"]


def _record_usage(result: "ClaudeResult", source: str = "script") -> None:
    """Best-effort: record one usage row into the PyRunner DB.

    Writes directly via sqlite3 (same approach as DataStore), attributed to the
    current run/script via env vars injected by the executor. Never raises --
    usage tracking must never break a user's script.
    """
    db_path = os.environ.get("PYRUNNER_DB_PATH")
    if not db_path:
        return
    try:
        import sqlite3
        import uuid as _uuid

        row_id = _uuid.uuid4().hex
        script_id = os.environ.get("PYRUNNER_SCRIPT_ID") or None
        run_id = os.environ.get("PYRUNNER_RUN_ID") or None
        script_name = os.environ.get("PYRUNNER_SCRIPT_NAME", "") or ""

        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute(
                """
                INSERT INTO claude_usage (
                    id, created_at, script_id, run_id, script_name, source, model,
                    input_tokens, output_tokens, cache_creation_tokens,
                    cache_read_tokens, num_turns, duration_ms, cost_usd
                ) VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    script_id,
                    run_id,
                    script_name,
                    source,
                    result.model or "",
                    int(result.input_tokens or 0),
                    int(result.output_tokens or 0),
                    int(result.cache_creation_tokens or 0),
                    int(result.cache_read_tokens or 0),
                    int(result.num_turns or 0),
                    int(result.duration_ms or 0),
                    result.cost_usd,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Telemetry is best-effort; swallow everything.
        pass


def _prepare(tools):
    if not _has_credentials():
        raise ClaudeNotConfiguredError(
            "Claude is not configured for this script. Enable it in PyRunner "
            "under Services -> Claude AI (the credential is injected "
            "automatically into your script runs)."
        )
    sdk = _import_sdk()
    selected = list(tools) if tools else list(DEFAULT_TOOLS)
    return sdk, selected


def ask_claude(
    prompt: str,
    *,
    tools: Optional[Sequence[str]] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    cwd: Optional[str] = None,
    lean: bool = False,
    raw: bool = False,
):
    """Ask Claude a question and return its answer.

    Args:
        prompt: What you want Claude to do.
        tools: Tool names Claude may use. Defaults to ["WebSearch", "WebFetch"].
            Pass [] to disable all tools (pure text generation).
        system_prompt: Optional system instruction.
        model: Optional model id (e.g. "claude-sonnet-4-6"). Defaults to the
            account's default.
        max_turns: Max agent loop iterations (default 12).
        cwd: Working directory for any file tools you enable.
        lean: If True, only define the tools you requested instead of the full
            built-in toolset. This cuts ~50k tokens of cached agent overhead per
            call -- recommended for simple web-search/text tasks. (Visible as a
            big drop in "Cache tokens" on the usage page.)
        raw: If True, return a ``ClaudeResult`` (text + tools_used + cost +
            turns). If False (default), return the answer text as a string.

    Returns:
        str (default) or ClaudeResult (when raw=True).
    """
    sdk, selected = _prepare(tools)
    options = _build_options(
        sdk,
        tools=selected,
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        cwd=cwd,
        env=None,
        lean=lean,
    )
    result = _run_coro(_drive(sdk, prompt, options))
    _record_usage(result)
    if result.is_error and not result.text:
        raise RuntimeError(f"Claude run failed: {result.error or 'unknown error'}")
    return result if raw else result.text


def stream_claude(
    prompt: str,
    *,
    tools: Optional[Sequence[str]] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    cwd: Optional[str] = None,
    lean: bool = False,
) -> Iterator[str]:
    """Stream Claude's answer text as it is generated.

    Yields chunks of text. Same arguments as ``ask_claude`` (minus ``raw``),
    including ``lean`` to cut cached agent overhead.

    Example:
        for chunk in stream_claude("Explain async IO in 3 lines"):
            print(chunk, end="", flush=True)
    """
    import asyncio

    sdk, selected = _prepare(tools)
    options = _build_options(
        sdk,
        tools=selected,
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        cwd=cwd,
        env=None,
        lean=lean,
    )

    # Bridge the async generator to a sync generator via a thread + queue.
    q: "queue.Queue" = queue.Queue()
    _DONE = object()

    def _producer():
        def on_text(chunk: str):
            q.put(chunk)

        try:
            result = asyncio.run(_drive(sdk, prompt, options, on_text=on_text))
            _record_usage(result)
        except Exception as exc:  # surface errors to the consumer
            q.put(exc)
        finally:
            q.put(_DONE)

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    while True:
        item = q.get()
        if item is _DONE:
            break
        if isinstance(item, Exception):
            raise item
        yield item

    thread.join()
