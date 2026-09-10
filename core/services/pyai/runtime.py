"""
Py AI runtime — drives claude-agent-sdk with the read-only in-process tool server.

Reuses the configured Claude credential (subscription token or API key via
ClaudeService), exposes ONLY the Py AI MCP tools, and records usage to
ClaudeUsage with source='pyai'. The whole SDK drive lives in ``_drive`` so the
non-LLM surface (config, tools, handler, UI) stays testable by mocking it.
"""

import logging
from dataclasses import dataclass, field

from core.models import GlobalSettings
from core.services.claude_service import ClaudeService

from .tools import ALLOWED_TOOLS, SERVER_NAME, build_tools

logger = logging.getLogger(__name__)

_MAX_TURNS = 12
_BASE_SYSTEM = (
    "You are Py AI, a read-only assistant embedded in a PyRun instance. Answer "
    "questions about THIS instance using the provided tools (scripts, runs, "
    "schedules, datastores). You can only READ — you cannot create, edit, run, or "
    "delete anything, and you have no access to secrets. Be concise and concrete. "
    "If a tool returns nothing, say so plainly rather than guessing."
)


class PyAIError(Exception):
    """Raised when Py AI cannot answer (config or run failure)."""


@dataclass
class PyAIResult:
    text: str = ""
    tools_used: list = field(default_factory=list)
    is_error: bool = False
    error: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
    cost_usd: float | None = None


def _describe_error(message) -> str:
    parts = []
    subtype = getattr(message, "subtype", None)
    if subtype and subtype != "success":
        parts.append(str(subtype))
    api_error = getattr(message, "api_error_status", None)
    if api_error:
        parts.append(f"API error (HTTP {api_error})")
    final = getattr(message, "result", None)
    if final:
        parts.append(str(final))
    return "; ".join(parts) or "Py AI returned an error."


def _run(coro):
    """Run a coroutine from sync code, even inside an existing event loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    box = {}

    def _worker():
        box["result"] = asyncio.run(coro)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box["result"]


class PyAIService:
    @classmethod
    def is_available(cls) -> bool:
        s = GlobalSettings.get_settings()
        return bool(
            s.pyai_enabled
            and s.claude_enabled
            and ClaudeService.is_configured()
            and ClaudeService.cli_available()
        )

    @classmethod
    def respond(cls, message: str, *, workspace, history=None) -> PyAIResult:
        """Answer ``message`` about ``workspace``. Raises PyAIError on config/run failure."""
        s = GlobalSettings.get_settings()
        if not s.pyai_enabled:
            raise PyAIError("Py AI is not enabled.")
        if not (s.claude_enabled and ClaudeService.is_configured()):
            raise PyAIError("AI is not configured. Set it up under Services → AI Provider.")

        env = ClaudeService.get_script_env()
        if not env:
            raise PyAIError("AI provider credentials are unavailable.")

        provider = s.active_ai_provider
        model = s.pyai_model or (provider.default_model if provider else "") or ""
        env = dict(env)
        if model:
            env.pop("ANTHROPIC_MODEL", None)  # explicit model kwarg wins

        system = _BASE_SYSTEM
        if s.pyai_system_prompt:
            system = f"{system}\n\n{s.pyai_system_prompt}"

        result = _run(
            cls._drive(cls._build_prompt(message, history), system, model, env, workspace)
        )
        cls._record_usage(result)
        if result.is_error and not result.text:
            raise PyAIError(result.error or "Py AI run failed.")
        return result

    @staticmethod
    def _build_prompt(message: str, history) -> str:
        if not history:
            return message
        lines = []
        for turn in history[-6:]:
            who = "User" if turn.get("role") == "user" else "Py AI"
            lines.append(f"{who}: {turn.get('text', '')}")
        lines.append(f"User: {message}")
        return "\n".join(lines)

    @staticmethod
    async def _drive(prompt, system, model, env, workspace) -> PyAIResult:
        import claude_agent_sdk as sdk

        server = sdk.create_sdk_mcp_server(name=SERVER_NAME, tools=build_tools(workspace))
        kwargs = {
            "mcp_servers": {SERVER_NAME: server},
            "allowed_tools": list(ALLOWED_TOOLS),
            "system_prompt": system,
            "permission_mode": "dontAsk",
            "setting_sources": [],
            "max_turns": _MAX_TURNS,
            "env": env,
        }
        if model:
            kwargs["model"] = model
        options = sdk.ClaudeAgentOptions(**kwargs)

        result = PyAIResult(model=model)
        text_parts = []
        try:
            async for msg in sdk.query(prompt=prompt, options=options):
                name = type(msg).__name__
                if name == "AssistantMessage":
                    m = getattr(msg, "model", "") or ""
                    if m:
                        result.model = m
                    for block in getattr(msg, "content", []) or []:
                        bn = type(block).__name__
                        if bn == "TextBlock":
                            text_parts.append(getattr(block, "text", "") or "")
                        elif bn in ("ToolUseBlock", "ServerToolUseBlock"):
                            tn = getattr(block, "name", "")
                            if tn and tn not in result.tools_used:
                                result.tools_used.append(tn)
                elif name == "ResultMessage":
                    final = getattr(msg, "result", "") or ""
                    if final:
                        result.text = final
                    usage = getattr(msg, "usage", None) or {}
                    result.input_tokens = int(usage.get("input_tokens", 0) or 0)
                    result.output_tokens = int(usage.get("output_tokens", 0) or 0)
                    result.cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    result.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
                    result.num_turns = int(getattr(msg, "num_turns", 0) or 0)
                    result.duration_ms = int(getattr(msg, "duration_ms", 0) or 0)
                    result.cost_usd = getattr(msg, "total_cost_usd", None)
                    if getattr(msg, "is_error", False):
                        result.is_error = True
                        result.error = _describe_error(msg)
        except Exception as exc:  # noqa: BLE001 — trailing ProcessError after error result
            if not result.error:
                result.error = str(exc)
            result.is_error = True

        if not result.text:
            result.text = "".join(text_parts)
        return result

    @staticmethod
    def _record_usage(result: PyAIResult) -> None:
        try:
            from core.models import ClaudeUsage

            provider = ClaudeService.get_active_provider()
            ClaudeUsage.objects.create(
                source=ClaudeUsage.Source.PYAI,
                provider=provider.provider_type if provider else "",
                model=result.model or "",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_creation_tokens=result.cache_creation_tokens,
                cache_read_tokens=result.cache_read_tokens,
                num_turns=result.num_turns,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
            )
        except Exception:
            logger.debug("Failed to record Py AI usage", exc_info=True)
