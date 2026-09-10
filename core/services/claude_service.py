"""
AI provider service (historically the "Claude service" — module/class names kept).

Centralizes the AI integration: resolving the active AIProvider profile,
decrypting its credential, building the environment that routes the Claude
Code CLI / claude-agent-sdk at the right backend, and testing connections
from the control panel.

Provider profiles (Anthropic, Z.AI, OpenRouter, Ollama, custom) are stored
encrypted on AIProvider rows; the active one is selected by
``GlobalSettings.active_ai_provider``. Scripts consume the env through the
``pyrunner_ai`` helper; Py AI and connection tests share the same plumbing.

Routing model: the CLI talks to Anthropic natively (API key or subscription
OAuth token), and to any Anthropic-compatible endpoint via
``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN`` with ``ANTHROPIC_API_KEY``
explicitly blanked (otherwise the CLI falls back to its own auth).
"""

import logging
from typing import Optional, Tuple

from django.conf import settings as django_settings

from core.models import AIProvider, GlobalSettings
from core.services.encryption_service import EncryptionService, EncryptionError

logger = logging.getLogger(__name__)

# Canned prompt used by "Test Connection" on Anthropic: forces a web search so
# we verify the full path (auth + CLI + web tools) in one shot. WebSearch is an
# Anthropic server-side tool, so this test is Anthropic-only.
_TEST_PROMPT = (
    "Use web search to find the current latest stable version of Python, then "
    "reply with a single short sentence stating the version number."
)
_TEST_TOOLS = ["WebSearch", "WebFetch"]

# Tool-call test used for every other provider: an in-process MCP `ping` tool.
# This verifies auth + base_url routing + the agentic tool-calling loop — the
# thing that actually varies by model, and the thing Py AI and
# pyrunner_ai(tools=...) depend on.
_PING_WORD = "PYRUNNER-PONG"
_PING_SERVER = "pyrunner_test"
_PING_TOOL = f"mcp__{_PING_SERVER}__ping"
_PING_PROMPT = (
    "Call the ping tool once, then reply with exactly the single word it "
    "returns and nothing else."
)


def _describe_result_error(message) -> str:
    """Build a useful error string from an errored ResultMessage.

    The SDK spreads error info across several fields; `errors` is often empty,
    so we also pull `subtype`, `api_error_status`, the final `result` text, and
    any `permission_denials`.
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
    final = getattr(message, "result", None)
    if final:
        parts.append(str(final))
    return "; ".join(parts) or "The provider returned an error (no details provided)"


class ClaudeServiceError(Exception):
    """Raised when AI provider operations fail."""


class ClaudeService:
    """Configuration, status, env building, and connection testing for AI providers."""

    # -- configuration helpers --------------------------------------------

    @classmethod
    def get_active_provider(cls) -> Optional[AIProvider]:
        """The provider profile selected in settings, or None."""
        return GlobalSettings.get_settings().active_ai_provider

    @classmethod
    def is_configured(cls) -> bool:
        """True if an active provider exists and has a usable credential."""
        provider = cls.get_active_provider()
        if provider is None:
            return False
        return bool(provider.credential_encrypted) or not provider.credential_required

    @classmethod
    def _decrypt_credential(cls, provider: AIProvider) -> str:
        """Return the provider's decrypted credential (or its preset default)."""
        if not provider.credential_encrypted:
            default = provider.preset.get("default_credential", "")
            if default and not provider.credential_required:
                return default
            raise ClaudeServiceError("AI provider credential is not configured")
        try:
            return EncryptionService.decrypt(provider.credential_encrypted)
        except EncryptionError as exc:
            raise ClaudeServiceError(f"Failed to decrypt AI provider credential: {exc}")

    @classmethod
    def _build_env(
        cls,
        provider_type: str,
        credential: str,
        *,
        auth_method: str = AIProvider.AuthMethod.API_KEY,
        base_url: str = "",
        model: str = "",
        extra_env: Optional[dict] = None,
    ) -> dict:
        """Env vars that route the CLI/SDK at the given provider.

        Anthropic output is identical to the historical single-provider
        behavior (regression-tested); third-party providers ride
        ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN with ANTHROPIC_API_KEY
        explicitly blanked.
        """
        env = {"CLAUDE_CONFIG_DIR": str(django_settings.CLAUDE_CONFIG_DIR)}
        if provider_type == AIProvider.ProviderType.ANTHROPIC:
            if auth_method == AIProvider.AuthMethod.API_KEY:
                env["ANTHROPIC_API_KEY"] = credential
            else:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
        else:
            env["ANTHROPIC_BASE_URL"] = base_url
            env["ANTHROPIC_AUTH_TOKEN"] = credential
            # Must be explicitly empty — unset, the CLI falls back to its own auth.
            env["ANTHROPIC_API_KEY"] = ""
            for key, value in (extra_env or {}).items():
                env.setdefault(key, value)
        if model:
            env["ANTHROPIC_MODEL"] = model
        return env

    @classmethod
    def _env_for_provider(cls, provider: AIProvider, credential: str) -> dict:
        return cls._build_env(
            provider.provider_type,
            credential,
            auth_method=provider.auth_method,
            base_url=provider.base_url or provider.preset.get("base_url", ""),
            model=provider.default_model,
            extra_env=provider.preset.get("extra_env"),
        )

    @classmethod
    def get_script_env(cls) -> dict:
        """Environment variables to inject into script runs so AI works.

        Returns an empty dict when AI is disabled or no usable provider is
        active. Also carries PYRUNNER_AI_PROVIDER so the pyrunner_ai helper can
        attribute usage rows to the serving provider.
        """
        s = GlobalSettings.get_settings()
        if not s.claude_enabled:
            return {}
        provider = s.active_ai_provider
        if provider is None:
            return {}

        try:
            credential = cls._decrypt_credential(provider)
        except ClaudeServiceError as exc:
            logger.error("AI env not injected: %s", exc)
            return {}

        env = cls._env_for_provider(provider, credential)
        env["PYRUNNER_AI_PROVIDER"] = provider.provider_type
        return env

    @classmethod
    def conflicting_env_keys(cls) -> list:
        """Routing/credential env var(s) that must be removed for the active provider.

        Prevents a stray host-level key from overriding (or rerouting) the
        configured one. With no active provider, strip everything — nothing is
        injected, so nothing from the host should leak into runs either.
        """
        all_keys = [
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        ]
        provider = cls.get_active_provider()
        if provider is None:
            return all_keys
        if provider.provider_type == AIProvider.ProviderType.ANTHROPIC:
            if provider.auth_method == AIProvider.AuthMethod.API_KEY:
                return ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]
            return ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]
        # Third-party: BASE_URL/AUTH_TOKEN/API_KEY are ours; only the OAuth
        # token could hijack auth back to Anthropic.
        return ["CLAUDE_CODE_OAUTH_TOKEN"]

    @classmethod
    def get_status(cls) -> dict:
        """Status dict for the Services UI card."""
        s = GlobalSettings.get_settings()
        provider = s.active_ai_provider
        return {
            "enabled": s.claude_enabled,
            "configured": cls.is_configured(),
            "provider": provider,
            "provider_name": provider.name if provider else "",
            "provider_type": provider.provider_type if provider else "",
            "provider_type_label": provider.get_provider_type_display() if provider else "None",
            "auth_method": provider.auth_method if provider else "",
            "auth_method_label": provider.get_auth_method_display() if provider else "",
            "model": (provider.default_model if provider else "") or "Account default",
            "cli_available": cls.cli_available(),
            "last_tested": provider.last_tested_at if provider else None,
            "providers_count": AIProvider.objects.count(),
        }

    @staticmethod
    def cli_available() -> bool:
        """Whether a Claude Code CLI is usable.

        True if `claude` is on PATH, or if the claude-agent-sdk ships a bundled
        CLI binary (the SDK prefers its bundled CLI, so PATH alone is not enough).
        """
        import os
        import shutil

        if shutil.which("claude"):
            return True
        try:
            import claude_agent_sdk

            bundled = os.path.join(
                os.path.dirname(claude_agent_sdk.__file__), "_bundled"
            )
            if os.path.isdir(bundled):
                for name in ("claude", "claude.exe"):
                    if os.path.isfile(os.path.join(bundled, name)):
                        return True
        except Exception:
            pass
        return False

    # -- connection testing -----------------------------------------------

    @classmethod
    def test_connection_with_credentials(
        cls,
        provider_type: str,
        credential: str,
        *,
        auth_method: str = AIProvider.AuthMethod.API_KEY,
        base_url: str = "",
        model: str = "",
        extra_env: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """Validate provider credentials with a real SDK round-trip.

        Anthropic runs the canned web-search query (verifies auth + CLI + web
        tools). Every other provider runs the MCP ping tool-call test (verifies
        auth + base_url routing + tool calling — WebSearch does not exist on
        third-party backends).

        Returns (success, message): the answer on success, the error otherwise.
        """
        is_anthropic = provider_type == AIProvider.ProviderType.ANTHROPIC
        if not credential:
            # Non-Anthropic presets may define a default token (Ollama).
            from core.models import PROVIDER_PRESETS

            credential = PROVIDER_PRESETS.get(provider_type, {}).get("default_credential", "")
            if not credential:
                return False, "No credential provided to test."

        if not is_anthropic and not base_url:
            return False, "No endpoint URL provided to test."

        if not cls.cli_available():
            return (
                False,
                "The Claude Code CLI is not installed on this server. It ships "
                "with the PyRun Docker image -- make sure you are running the "
                "current image.",
            )

        env = cls._build_env(
            provider_type,
            credential,
            auth_method=auth_method,
            base_url=base_url,
            extra_env=extra_env,
        )

        try:
            if is_anthropic:
                text, tools, usage = cls._run_test_query(env, model)
            else:
                text, tools, usage = cls._run_ping_test(env, model)
        except Exception as exc:  # noqa: BLE001 - surface a friendly message
            return False, cls._friendly_error(str(exc))

        # Record the test call's token usage (best-effort, source='test').
        cls._record_test_usage(usage, provider_type)

        if not text:
            return (
                False,
                "Connected, but no response was returned. Check the credential "
                "and that your account has access.",
            )

        if not is_anthropic and _PING_TOOL not in tools:
            return (
                False,
                "The model replied but never called the test tool. It may not "
                "support tool calling reliably — pick a stronger model for "
                "agentic use (Py AI, pyrunner_ai tools).",
            )

        used = f" (used: {', '.join(tools)})" if tools else ""
        return True, f"{text.strip()}{used}"

    @classmethod
    def test_provider(
        cls, provider: AIProvider, credential_override: str = ""
    ) -> Tuple[bool, str]:
        """Test one saved provider row; stamp last_tested_at on success.

        credential_override supports test-before-save from the edit form.
        """
        from django.utils import timezone

        credential = credential_override
        if not credential and provider.credential_encrypted:
            try:
                credential = cls._decrypt_credential(provider)
            except ClaudeServiceError as exc:
                return False, str(exc)

        success, message = cls.test_connection_with_credentials(
            provider.provider_type,
            credential,
            auth_method=provider.auth_method,
            base_url=provider.base_url or provider.preset.get("base_url", ""),
            model=provider.default_model,
            extra_env=provider.preset.get("extra_env"),
        )
        if success and provider.pk:
            provider.last_tested_at = timezone.now()
            provider.save(update_fields=["last_tested_at"])
        return success, message

    @classmethod
    def test_saved_connection(cls) -> Tuple[bool, str]:
        """Test using the currently-active provider."""
        provider = cls.get_active_provider()
        if provider is None:
            return False, "No active AI provider is configured."
        return cls.test_provider(provider)

    # -- internals --------------------------------------------------------

    @classmethod
    def _run_test_query(cls, env: dict, model: str) -> Tuple[str, list, dict]:
        """Execute the canned web-search query (Anthropic test)."""
        return cls._run_query(
            env,
            model,
            prompt=_TEST_PROMPT,
            kwargs_extra={
                "allowed_tools": list(_TEST_TOOLS),
                # Only define the web tools (not the full built-in toolset) so the
                # test doesn't burn ~50k cached tokens of agent overhead.
                "tools": list(_TEST_TOOLS),
            },
        )

    @classmethod
    def _run_ping_test(cls, env: dict, model: str) -> Tuple[str, list, dict]:
        """Execute the MCP ping tool-call test (third-party providers)."""
        import claude_agent_sdk as sdk

        @sdk.tool("ping", "Responds with a fixed verification word.", {})
        async def _ping(args):
            return {"content": [{"type": "text", "text": _PING_WORD}]}

        server = sdk.create_sdk_mcp_server(name=_PING_SERVER, tools=[_ping])
        return cls._run_query(
            env,
            model,
            prompt=_PING_PROMPT,
            kwargs_extra={
                "mcp_servers": {_PING_SERVER: server},
                "allowed_tools": [_PING_TOOL],
                # No built-in tools: smaller tool surface is friendlier to
                # non-frontier models and keeps the test cheap.
                "tools": [],
            },
        )

    @classmethod
    def _run_query(
        cls, env: dict, model: str, *, prompt: str, kwargs_extra: dict
    ) -> Tuple[str, list, dict]:
        """Drive one canned query via claude-agent-sdk.

        Returns (text, tools_used, usage) where usage has token counts, model,
        num_turns, duration_ms, and cost_usd.
        """
        import asyncio

        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:
            raise ClaudeServiceError(
                "claude-agent-sdk is not installed on the server."
            ) from exc

        # Holder so a captured error survives the SDK's trailing ProcessError
        # (the CLI exits non-zero after an error result, which the SDK re-raises
        # on the iteration *after* it yields the errored ResultMessage).
        err_box = {"msg": None}

        async def _go():
            kwargs = {
                "permission_mode": "dontAsk",
                "setting_sources": [],
                "max_turns": 10,
                "env": env,
            }
            kwargs.update(kwargs_extra)
            if model:
                kwargs["model"] = model
            options = sdk.ClaudeAgentOptions(**kwargs)

            text_parts = []
            tools_used = []
            final = ""
            usage = {
                "model": model or "",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "num_turns": 0,
                "duration_ms": 0,
                "cost_usd": None,
            }
            async for message in sdk.query(prompt=prompt, options=options):
                name = type(message).__name__
                if name == "AssistantMessage":
                    msg_model = getattr(message, "model", "") or ""
                    if msg_model:
                        usage["model"] = msg_model
                    for block in getattr(message, "content", []) or []:
                        bname = type(block).__name__
                        if bname == "TextBlock":
                            text_parts.append(getattr(block, "text", "") or "")
                        elif bname in ("ToolUseBlock", "ServerToolUseBlock"):
                            tool_name = getattr(block, "name", "")
                            if tool_name and tool_name not in tools_used:
                                tools_used.append(tool_name)
                elif name == "ResultMessage":
                    final = getattr(message, "result", "") or ""
                    raw = getattr(message, "usage", None) or {}
                    usage["input_tokens"] = int(raw.get("input_tokens", 0) or 0)
                    usage["output_tokens"] = int(raw.get("output_tokens", 0) or 0)
                    usage["cache_creation_tokens"] = int(
                        raw.get("cache_creation_input_tokens", 0) or 0
                    )
                    usage["cache_read_tokens"] = int(
                        raw.get("cache_read_input_tokens", 0) or 0
                    )
                    usage["num_turns"] = int(getattr(message, "num_turns", 0) or 0)
                    usage["duration_ms"] = int(getattr(message, "duration_ms", 0) or 0)
                    usage["cost_usd"] = getattr(message, "total_cost_usd", None)
                    if getattr(message, "is_error", False):
                        err_box["msg"] = _describe_result_error(message)
            return (final or "".join(text_parts)), tools_used, usage

        try:
            text, tools, usage = asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001
            # The SDK raises a trailing ProcessError after an error result.
            # Prefer the structured error we captured from the ResultMessage
            # (which includes api_error_status); fall back to the raw message.
            if not err_box["msg"]:
                err_box["msg"] = str(exc)
            text, tools, usage = "", [], {}

        if err_box["msg"]:
            raise ClaudeServiceError(err_box["msg"])
        return text, tools, usage

    @staticmethod
    def _record_test_usage(usage: dict, provider_type: str = "") -> None:
        """Record a test call's usage row (source='test'). Best-effort."""
        try:
            from core.models import ClaudeUsage

            if not any(
                usage.get(k)
                for k in ("input_tokens", "output_tokens", "cache_read_tokens")
            ):
                return  # nothing to record
            ClaudeUsage.objects.create(
                source=ClaudeUsage.Source.TEST,
                provider=provider_type or "",
                model=usage.get("model", ""),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                num_turns=usage.get("num_turns", 0),
                duration_ms=usage.get("duration_ms", 0),
                cost_usd=usage.get("cost_usd"),
            )
        except Exception:
            logger.debug("Failed to record test usage", exc_info=True)

    @staticmethod
    def _friendly_error(error_msg: str) -> str:
        low = error_msg.lower()
        if "cli" in low and ("not found" in low or "notfound" in low):
            return (
                "Claude Code CLI not found on the server. Use the current "
                "PyRun Docker image, which bundles it."
            )
        if "error_max_turns" in low:
            return (
                "The test agent ran out of turns before finishing. Auth itself "
                "looks OK; try again, or the model may be struggling with the "
                "test's tool call."
            )
        if "http 429" in low or ("rate" in low and "limit" in low):
            return (
                "Rate limited by the provider (HTTP 429). Your plan's usage "
                "limit was hit - wait a while and try again."
            )
        if "http 529" in low or "overloaded" in low:
            return "The provider is temporarily overloaded (HTTP 529). Try again in a moment."
        if "http 401" in low or "401" in low or "unauthorized" in low or ("invalid" in low and ("token" in low or "key" in low or "api" in low)):
            return "Authentication failed (HTTP 401). Check your token / API key (re-run `claude setup-token` if it expired)."
        if "http 403" in low or "403" in low or "forbidden" in low:
            return "Access denied (HTTP 403). Your account may not have access to this model."
        if "http 500" in low or "http 502" in low or "http 503" in low:
            return "Provider server error. Try again shortly."
        if "timeout" in low or "timed out" in low:
            return "The request timed out. Check the server's network access to the provider."
        return f"Connection failed: {error_msg}"
