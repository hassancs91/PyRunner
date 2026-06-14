"""
Claude AI service.

Centralizes the Claude integration: reading/decrypting the configured
credential, building the environment that exposes Claude to script runs, and
testing the connection (including web search) from the control panel.

Credentials are stored encrypted on GlobalSettings and configured under
Services -> Claude AI. Scripts use them through the ``pyrunner_ai`` helper.
"""

import logging
from typing import Optional, Tuple

from django.conf import settings as django_settings

from core.models import GlobalSettings
from core.services.encryption_service import EncryptionService, EncryptionError

logger = logging.getLogger(__name__)

# Canned prompt used by "Test Connection": forces a web search so we verify the
# full path (auth + CLI + web tools) in one shot.
_TEST_PROMPT = (
    "Use web search to find the current latest stable version of Python, then "
    "reply with a single short sentence stating the version number."
)
_TEST_TOOLS = ["WebSearch", "WebFetch"]


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
    return "; ".join(parts) or "Claude returned an error (no details provided)"


class ClaudeServiceError(Exception):
    """Raised when Claude operations fail."""


class ClaudeService:
    """Provides configuration, status, and connection testing for Claude AI."""

    # -- configuration helpers --------------------------------------------

    @classmethod
    def is_configured(cls) -> bool:
        """True if the credential for the selected auth method is present."""
        s = GlobalSettings.get_settings()
        if s.claude_auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            return bool(s.claude_api_key_encrypted)
        return bool(s.claude_oauth_token_encrypted)

    @classmethod
    def _decrypt_credential(cls, settings: GlobalSettings) -> str:
        """Return the decrypted credential for the selected auth method."""
        if settings.claude_auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            encrypted = settings.claude_api_key_encrypted
        else:
            encrypted = settings.claude_oauth_token_encrypted
        if not encrypted:
            raise ClaudeServiceError("Claude credential is not configured")
        try:
            return EncryptionService.decrypt(encrypted)
        except EncryptionError as exc:
            raise ClaudeServiceError(f"Failed to decrypt Claude credential: {exc}")

    @classmethod
    def get_script_env(cls) -> dict:
        """Environment variables to inject into script runs so Claude works.

        Returns an empty dict when Claude is disabled or not configured. When
        enabled, returns the credential env var, the writable config dir, and
        the model hint. Only the credential for the *selected* method is set, so
        the two auth methods never conflict.
        """
        s = GlobalSettings.get_settings()
        if not s.claude_enabled or not cls.is_configured():
            return {}

        try:
            credential = cls._decrypt_credential(s)
        except ClaudeServiceError as exc:
            logger.error("Claude env not injected: %s", exc)
            return {}

        env = {"CLAUDE_CONFIG_DIR": str(django_settings.CLAUDE_CONFIG_DIR)}
        if s.claude_auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            env["ANTHROPIC_API_KEY"] = credential
        else:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
        if s.claude_default_model:
            env["ANTHROPIC_MODEL"] = s.claude_default_model
        return env

    @classmethod
    def conflicting_env_keys(cls) -> list:
        """Credential env var(s) that must be removed for the selected method.

        Prevents a stray host-level key from overriding the configured one.
        """
        s = GlobalSettings.get_settings()
        if s.claude_auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            return ["CLAUDE_CODE_OAUTH_TOKEN"]
        return ["ANTHROPIC_API_KEY"]

    @classmethod
    def get_status(cls) -> dict:
        """Status dict for the Services UI card."""
        s = GlobalSettings.get_settings()
        return {
            "enabled": s.claude_enabled,
            "configured": cls.is_configured(),
            "auth_method": s.claude_auth_method,
            "auth_method_label": s.get_claude_auth_method_display(),
            "model": s.claude_default_model or "Account default",
            "cli_available": cls._cli_available(),
            "last_tested": s.claude_last_tested_at,
        }

    @staticmethod
    def _cli_available() -> bool:
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
        auth_method: str,
        credential: str,
        model: str = "",
    ) -> Tuple[bool, str]:
        """Run a canned web-search query to validate credentials.

        Args:
            auth_method: "subscription" or "api_key".
            credential: The token/key to test (already plaintext).
            model: Optional model id.

        Returns:
            (success, message) where message is the answer on success or the
            error on failure.
        """
        if not credential:
            return False, "No credential provided to test."

        if not cls._cli_available():
            return (
                False,
                "The Claude Code CLI is not installed on this server. It ships "
                "with the PyRunner Docker image -- make sure you are running the "
                "current image.",
            )

        env = {"CLAUDE_CONFIG_DIR": str(django_settings.CLAUDE_CONFIG_DIR)}
        if auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            env["ANTHROPIC_API_KEY"] = credential
        else:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential

        try:
            text, tools, usage = cls._run_test_query(env, model)
        except Exception as exc:  # noqa: BLE001 - surface a friendly message
            return False, cls._friendly_error(str(exc))

        # Record the test call's token usage (best-effort, source='test').
        cls._record_test_usage(usage)

        if not text:
            return (
                False,
                "Connected, but no response was returned. Check the credential "
                "and that your account has access.",
            )

        used = f" (used: {', '.join(tools)})" if tools else ""
        return True, f"{text.strip()}{used}"

    @classmethod
    def test_saved_connection(cls) -> Tuple[bool, str]:
        """Test using the currently-saved settings; record success time."""
        from django.utils import timezone

        s = GlobalSettings.get_settings()
        try:
            credential = cls._decrypt_credential(s)
        except ClaudeServiceError as exc:
            return False, str(exc)

        success, message = cls.test_connection_with_credentials(
            auth_method=s.claude_auth_method,
            credential=credential,
            model=s.claude_default_model,
        )
        if success:
            s.claude_last_tested_at = timezone.now()
            s.save(update_fields=["claude_last_tested_at"])
        return success, message

    # -- internals --------------------------------------------------------

    @classmethod
    def _run_test_query(cls, env: dict, model: str) -> Tuple[str, list, dict]:
        """Execute the canned query via claude-agent-sdk.

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
                "allowed_tools": list(_TEST_TOOLS),
                # Only define the web tools (not the full built-in toolset) so the
                # test doesn't burn ~50k cached tokens of agent overhead.
                "tools": list(_TEST_TOOLS),
                "permission_mode": "dontAsk",
                "setting_sources": [],
                "max_turns": 10,
                "env": env,
            }
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
            async for message in sdk.query(prompt=_TEST_PROMPT, options=options):
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
    def _record_test_usage(usage: dict) -> None:
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
                "PyRunner Docker image, which bundles it."
            )
        if "error_max_turns" in low:
            return (
                "The test agent ran out of turns before finishing the web "
                "search. Auth itself looks OK; try again, or your network may be "
                "throttling outbound calls to Anthropic."
            )
        if "http 429" in low or ("rate" in low and "limit" in low):
            return (
                "Rate limited by Anthropic (HTTP 429). Your Claude plan's usage "
                "limit was hit - wait a while and try again."
            )
        if "http 529" in low or "overloaded" in low:
            return "Anthropic is temporarily overloaded (HTTP 529). Try again in a moment."
        if "http 401" in low or "401" in low or "unauthorized" in low or ("invalid" in low and ("token" in low or "key" in low or "api" in low)):
            return "Authentication failed (HTTP 401). Check your token / API key (re-run `claude setup-token` if it expired)."
        if "http 403" in low or "403" in low or "forbidden" in low:
            return "Access denied (HTTP 403). Your account may not have access to this model."
        if "http 500" in low or "http 502" in low or "http 503" in low:
            return "Anthropic server error. Try again shortly."
        if "timeout" in low or "timed out" in low:
            return "The request timed out. Check the server's network access to Anthropic."
        return f"Connection failed: {error_msg}"
