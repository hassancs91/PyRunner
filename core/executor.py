"""
Script executor module for PyRunner.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.
"""

import json
import logging
import os
import subprocess
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core.executor_backends import LocalSubprocessBackend, RunSpec, get_run_backend

# Re-exported so ``from core.executor import _kill_process_tree`` keeps working
# (TaskService.force_stop_task imports it). The implementation now lives with
# the local backend; force-stop stays pid-based for the local backend.
from core.executor_backends.local import kill_process_tree as _kill_process_tree
from core.models import GlobalSettings, Run, Secret, Workspace
from core.services import ClaudeService, EncryptionService

logger = logging.getLogger(__name__)

# Maximum output size (1MB) to prevent database bloat
MAX_OUTPUT_BYTES = 1_000_000


def resolve_secrets_for_run(run) -> dict:
    """Resolve the secrets to inject into (and mask in) a run, scoped to its workspace.

    The SINGLE shared resolver feeding BOTH env injection and output masking, so
    the two can never drift (no leak, no over-mask). Tenancy Stage 1.

    Workspace scoping (transitional until the Stage 3 creation-sweep, chosen to
    keep a single-workspace instance byte-for-byte):
    - ``run`` is None or ``run.workspace_id`` is None ⇒ ALL secrets (today's
      behavior; an un-scoped run cannot narrow).
    - otherwise ⇒ the run's-workspace secrets PLUS still-unassigned
      (``workspace IS NULL``) secrets — because secret *creation* is not yet
      workspace-scoped, so a freshly-created (NULL) secret must keep injecting on
      a single-workspace instance.

    Phase B (plugin platform) layers owner_plugin / per-script grant rules INSIDE
    this same function; workspace scoping composes with them here, never forks.

    Returns:
        Dict of {key: decrypted_value}.
    """
    secrets_env = {}

    # Only try to get secrets if encryption is configured
    if not EncryptionService.is_configured():
        logger.debug("Encryption not configured - secrets will not be injected")
        return secrets_env

    try:
        queryset = Secret.objects.all()
        ws_id = getattr(run, "workspace_id", None) if run is not None else None
        if ws_id is not None:
            queryset = queryset.filter(
                Q(workspace_id=ws_id) | Q(workspace__isnull=True)
            )
        for secret in queryset:
            try:
                secrets_env[secret.key] = secret.get_decrypted_value()
            except Exception as e:
                logger.error(f"Failed to decrypt secret {secret.key}: {e}")
    except Exception as e:
        logger.error(f"Failed to load secrets: {e}")

    return secrets_env


def _build_script_environment(
    webhook_data: dict | None = None, run: "Run | None" = None
) -> dict:
    """
    Build the environment dict for script execution.

    Combines system environment with secrets, webhook data, and DataStore access.
    Secrets override any same-named system variables.
    Webhook data is added with WEBHOOK_ prefix.

    Args:
        webhook_data: Optional webhook data from HTTP request
        run: Optional Run being executed (used for Claude usage attribution)

    Returns:
        Environment dict to pass to subprocess
    """
    # Start with system environment
    env = os.environ.copy()

    # Hardening: drop PyRunner's own infra secrets so they never leak into a
    # user's run (a user Secret of the same name still injects below).
    for _denied in settings.PYRUNNER_RUN_ENV_DENYLIST:
        env.pop(_denied, None)

    # Add secrets (overriding any existing vars with same name), scoped to the
    # run's workspace via the shared resolver.
    secrets = resolve_secrets_for_run(run)
    env.update(secrets)

    # Add webhook data if present
    if webhook_data:
        env["WEBHOOK_METHOD"] = webhook_data.get("method", "")
        env["WEBHOOK_QUERY"] = json.dumps(webhook_data.get("query", {}))
        env["WEBHOOK_CONTENT_TYPE"] = webhook_data.get("content_type", "")

        if "body" in webhook_data:
            env["WEBHOOK_BODY"] = webhook_data["body"]

        if "body_json" in webhook_data:
            env["WEBHOOK_BODY_JSON"] = json.dumps(webhook_data["body_json"])

    # Add Claude AI support (Services -> Claude AI). Injects the configured
    # credential + config dir so the pyrunner_ai helper works in scripts.
    # Empty dict when Claude is disabled/unconfigured.
    claude_env = ClaudeService.get_script_env()
    if claude_env:
        # Remove any stray host credential for the *other* auth method so it
        # can't override the configured one.
        for key in ClaudeService.conflicting_env_keys():
            env.pop(key, None)
        env.update(claude_env)

        # Attribution so pyrunner_ai can record usage against this run/script.
        # Use .hex to match Django's UUID storage on SQLite (no dashes).
        if run is not None:
            env["PYRUNNER_RUN_ID"] = run.id.hex
            if run.script_id:
                env["PYRUNNER_SCRIPT_ID"] = run.script.id.hex
                env["PYRUNNER_SCRIPT_NAME"] = run.script.name

    # DataStore + Claude-usage access for the script helpers.
    #
    # On SQLite (the default) the helpers read the DB file directly via
    # PYRUNNER_DB_PATH — byte-for-byte with before, and with no dependency on
    # the web tier being up. On any other engine (Postgres) there is no local DB
    # file, so the helpers go through the internal loopback API instead,
    # authenticated by a signed per-run token. The token is only available with
    # a run context (it is keyed to run.id).
    from django.db import connection as _db_connection

    if _db_connection.vendor == "sqlite":
        env["PYRUNNER_DB_PATH"] = str(settings.DATABASES["default"]["NAME"])
    if run is not None:
        from core.services.datastore_token import mint_datastore_token

        env["PYRUNNER_INTERNAL_URL"] = settings.PYRUNNER_INTERNAL_BASE_URL
        env["PYRUNNER_INTERNAL_TOKEN"] = mint_datastore_token(run.id)

        # Trusted workspace id for datastore-by-name scoping (tenancy Stage 2).
        # The SQLite helper scopes lookups to this workspace; it is derived from
        # the run (never script-supplied), defaulting to the default workspace
        # for an un-scoped run so single-workspace resolution is unchanged.
        ws_id = run.workspace_id
        if ws_id is None:
            default_ws = Workspace.get_default()
            ws_id = default_ws.id if default_ws else None
        if ws_id is not None:
            env["PYRUNNER_WORKSPACE_ID"] = ws_id.hex

    # Add script_helpers to PYTHONPATH so scripts can import pyrunner_datastore
    helpers_path = str(Path(settings.BASE_DIR) / "core" / "script_helpers")
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{helpers_path}{os.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = helpers_path

    return env


def _mask_secrets_in_output(output: str, secrets: dict) -> str:
    """
    Mask secret values in output to prevent accidental exposure.

    Args:
        output: The script output
        secrets: Dict of {key: value} secrets

    Returns:
        Output with secret values replaced with [KEY:MASKED]
    """
    if not output or not secrets:
        return output

    masked = output
    for key, value in secrets.items():
        if value and len(value) >= 4:  # Only mask non-trivial values
            masked = masked.replace(value, f"[{key}:MASKED]")

    return masked


class ExecutorError(Exception):
    """Base exception for executor errors."""

    pass


class EnvironmentNotFoundError(ExecutorError):
    """Raised when the environment directory does not exist."""

    pass


class PythonNotFoundError(ExecutorError):
    """Raised when the Python executable is not found."""

    pass


def _truncate_output(output: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """
    Truncate output if it exceeds max_bytes.

    Args:
        output: The output string to potentially truncate
        max_bytes: Maximum size in bytes (default 1MB)

    Returns:
        The original or truncated output with notice
    """
    if not output:
        return output

    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return output

    # Truncate and decode back, keeping a buffer for the notice
    notice = "\n\n[OUTPUT TRUNCATED - exceeded maximum size]"
    truncated = encoded[: max_bytes - len(notice.encode())].decode(
        "utf-8", errors="replace"
    )
    return truncated + notice


def _validate_environment(run: Run) -> str:
    """
    Validate the environment and return the Python executable path.

    Args:
        run: The Run instance containing the script and environment

    Returns:
        The absolute path to the Python executable

    Raises:
        EnvironmentNotFoundError: If environment directory doesn't exist
        PythonNotFoundError: If Python executable doesn't exist
    """
    environment = run.script.environment

    if not environment.exists():
        raise EnvironmentNotFoundError(
            f"Environment directory not found: {environment.get_full_path()}"
        )

    python_path = environment.get_python_executable()
    if not os.path.isfile(python_path):
        raise PythonNotFoundError(f"Python executable not found: {python_path}")

    return python_path


def _run_resource_limits() -> dict | None:
    """Build the posix resource-cap dict from the legacy env settings, or None.

    These ``PYRUNNER_RUN_RLIMIT_*`` env vars predate the dashboard sandbox
    controls and are now the break-glass fallback: they apply only for caps the
    dashboard leaves unset (see ``_resolve_run_limits``). Off by default (every
    setting 0), so an unconfigured instance carries no limits — unchanged.
    """
    mem = settings.PYRUNNER_RUN_RLIMIT_MEMORY_MB
    cpu = settings.PYRUNNER_RUN_RLIMIT_CPU_SECONDS
    nproc = settings.PYRUNNER_RUN_RLIMIT_NPROC
    if not (mem or cpu or nproc):
        return None
    limits = {}
    if mem:
        limits["memory_bytes"] = mem * 1024 * 1024
    if cpu:
        limits["cpu_seconds"] = cpu
    if nproc:
        limits["nproc"] = nproc
    return limits


def _resolve_run_limits() -> dict | None:
    """Resolve the posix resource caps for a run, dashboard-managed (DB) first.

    Sandbox plan Stage 1 (Seam 2): isolation config lives in ``GlobalSettings``
    and is resolved per-run at execution time (no restart, no env reliance). Each
    cap is read from the DB; a DB value of 0 means "unset" and falls back to the
    legacy ``PYRUNNER_RUN_RLIMIT_*`` env var for that one cap (break-glass), so
    the two layers degrade independently. ``RLIMIT_FSIZE`` is dashboard-only.

    Returns a ``{...}`` limits dict for ``RunSpec.limits`` (consumed by the posix
    ``preexec_fn`` in ``LocalSubprocessBackend``; a no-op on Windows), or ``None``
    when nothing is configured anywhere — a default instance, byte-for-byte today.
    """
    try:
        gs = GlobalSettings.get_settings()
    except Exception as e:
        # DB not ready (e.g. mid-migration boot window) — fall back to env only
        # rather than failing the run.
        logger.warning("Could not load GlobalSettings for run limits: %s", e)
        return _run_resource_limits()

    env_limits = _run_resource_limits() or {}
    limits = {}

    mem_mb = gs.sandbox_rlimit_memory_mb
    if mem_mb:
        limits["memory_bytes"] = mem_mb * 1024 * 1024
    elif "memory_bytes" in env_limits:
        limits["memory_bytes"] = env_limits["memory_bytes"]

    cpu_s = gs.sandbox_rlimit_cpu_seconds
    if cpu_s:
        limits["cpu_seconds"] = cpu_s
    elif "cpu_seconds" in env_limits:
        limits["cpu_seconds"] = env_limits["cpu_seconds"]

    nproc = gs.sandbox_rlimit_nproc
    if nproc:
        limits["nproc"] = nproc
    elif "nproc" in env_limits:
        limits["nproc"] = env_limits["nproc"]

    fsize_mb = gs.sandbox_rlimit_fsize_mb
    if fsize_mb:
        limits["fsize_bytes"] = fsize_mb * 1024 * 1024

    return limits or None


# Strictness ordering for the isolation policy. A workspace may TIGHTEN the
# instance default toward 'required' but never weaken below it.
_SANDBOX_STRICTNESS = {"off": 0, "optional": 1, "required": 2}


@dataclass
class IsolationDecision:
    """The resolved isolation policy for one run."""

    sandbox: bool  # True => run under the sandbox backend
    mandatory: bool  # True => effective policy is 'required' (fail-closed may apply)
    reason: str = ""  # short human explanation (logs)


def resolve_isolation(run, gs=None) -> IsolationDecision:
    """Resolve whether a run is sandboxed from the DB policy hierarchy.

    instance default (``GlobalSettings.sandbox_default``) → workspace policy
    (``Workspace.sandbox_policy``, can only TIGHTEN) → per-script toggle
    (``Script.isolation_mode``, honored only under 'optional'). Resolved at run
    time, so a policy change applies on the next run with no restart.

    A default instance (sandbox_default 'off', workspace policy null, script
    'inherit') returns ``sandbox=False`` — today's behavior, byte-for-byte.
    """
    if gs is None:
        gs = GlobalSettings.get_settings()
    base = gs.sandbox_default or "off"

    # Workspace policy can only make a run stricter than the instance default —
    # an Owner/Admin can mandate isolation but can't disable an instance floor.
    workspace = None
    if run is not None:
        workspace = getattr(run, "workspace", None)
        if workspace is None and getattr(run, "script_id", None):
            workspace = run.script.workspace
    ws_policy = getattr(workspace, "sandbox_policy", None) if workspace else None

    effective = base
    if ws_policy and _SANDBOX_STRICTNESS.get(ws_policy, 0) > _SANDBOX_STRICTNESS.get(base, 0):
        effective = ws_policy

    if effective == "required":
        return IsolationDecision(True, True, "policy: required")
    if effective == "off":
        return IsolationDecision(False, False, "policy: off")

    # 'optional': honor the per-script toggle ('sandboxed' opts in; else plain).
    mode = (
        getattr(run.script, "isolation_mode", "inherit")
        if (run is not None and getattr(run, "script_id", None))
        else "inherit"
    )
    if mode == "sandboxed":
        return IsolationDecision(True, False, "optional: script sandboxed")
    return IsolationDecision(False, False, "optional: script not sandboxed")


def _select_backend_for_run(run, gs):
    """Pick the backend for a run. An explicit ``PYRUNNER_RUN_BACKEND`` env value
    is a break-glass override; otherwise the DB isolation policy decides.

    Returns ``(backend, decision)`` — ``decision.mandatory`` drives fail-closed.
    The plain/local path deliberately flows through ``get_run_backend()`` so the
    single backend seam (and the ``PYRUNNER_RUN_BACKEND=local`` break-glass value)
    keeps working unchanged; only the sandbox-policy path bypasses it.
    """
    decision = resolve_isolation(run, gs)
    override = os.environ.get("PYRUNNER_RUN_BACKEND", "").strip().lower()

    # Sandbox is selected by an explicit 'sandbox' override or by the resolved
    # policy — unless an explicit 'local' override forces the plain path.
    want_sandbox = override == "sandbox" or (decision.sandbox and override != "local")
    if want_sandbox:
        from core.executor_backends import SandboxedSubprocessBackend

        reason = "override: sandbox" if override == "sandbox" else decision.reason
        return SandboxedSubprocessBackend(), IsolationDecision(
            True, decision.mandatory, reason
        )

    # Plain path: keep using the get_run_backend() seam (byte-for-byte default;
    # honors a 'local' break-glass value), so existing callers/tests are intact.
    return get_run_backend(), IsolationDecision(False, False, decision.reason)


def execute_run(run: Run, webhook_data: dict | None = None) -> None:
    """
    Execute a script run and update the Run record with results.

    This function is designed to be called from a django-q2 async task.
    It handles all aspects of script execution including:
    - Writing script code to a temporary file
    - Running the script with the appropriate Python executable
    - Capturing stdout/stderr
    - Handling timeouts
    - Updating the Run record with results

    Args:
        run: The Run model instance to execute
        webhook_data: Optional webhook data to inject as environment variables

    Note:
        This function always saves the Run state, even on errors.
        The Run status will be updated to one of:
        SUCCESS, FAILED, TIMEOUT, or remain FAILED on errors.
    """
    script_file_path = None

    try:
        # Phase 1: Pre-execution validation
        if run.status != Run.Status.PENDING:
            logger.warning(
                f"Run {run.id} is not in PENDING status (current: {run.status}). "
                "Skipping execution."
            )
            return

        # Update to RUNNING status
        run.status = Run.Status.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])

        # Validate environment
        try:
            python_path = _validate_environment(run)
        except EnvironmentNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return
        except PythonNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return

        # Ensure working directory exists
        workdir = Path(settings.SCRIPTS_WORKDIR)
        workdir.mkdir(parents=True, exist_ok=True)

        # Phase 2: Create temporary script file
        # Use delete=False for Windows compatibility (must close before subprocess reads)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
            dir=str(workdir),
        ) as script_file:
            # Use code_snapshot if available (preserves code at queue time)
            code = run.code_snapshot if run.code_snapshot else run.script.code
            script_file.write(code)
            script_file_path = script_file.name

        # Phase 3: Execute script
        try:
            # Build subprocess arguments
            cmd = [python_path, script_file_path]

            # Build environment with secrets and webhook data injected
            script_env = _build_script_environment(webhook_data, run=run)
            # Masking uses the SAME resolved set as injection (shared resolver),
            # so masking can never drift from what was injected.
            secrets = resolve_secrets_for_run(run)

            # Also mask the injected Claude credential in output, if any.
            claude_env = ClaudeService.get_script_env()
            for cred_key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if claude_env.get(cred_key):
                    secrets[cred_key] = claude_env[cred_key]

            # Select the RunBackend from the DB isolation policy (instance →
            # workspace → script), resolved per-run; an explicit
            # PYRUNNER_RUN_BACKEND env value is a break-glass override. The chosen
            # backend launches the script in its own process group/session so the
            # web process can later kill the whole job tree (force stop / timeout)
            # without touching the django-q worker. The Run lifecycle below — pid
            # record, masking, truncation, status mapping, the cancel-safe save —
            # stays in core.
            gs = GlobalSettings.get_settings()
            backend, decision = _select_backend_for_run(run, gs)

            # Fail-closed: when isolation is REQUIRED but the host can't deliver a
            # full sandbox, an opt-in instance fails the run rather than silently
            # degrading to a weaker tier. Default (fail_closed off) degrades+warns
            # inside the backend, preserving the accident-model behavior.
            if decision.sandbox and decision.mandatory and gs.sandbox_fail_closed:
                from core.executor_backends.sandboxed import runtime_tier
                from core.services.sandbox import CAP_FULL

                if runtime_tier() != CAP_FULL:
                    run.status = Run.Status.FAILED
                    run.exit_code = -1
                    run.stderr = (
                        "Isolation is required for this run, but a full sandbox is "
                        "unavailable on this host and fail-closed is enabled "
                        f"(active tier: {runtime_tier()}). The run was not executed."
                    )
                    logger.error(
                        "Run %s blocked by fail-closed: sandbox required but unavailable",
                        run.id,
                    )
                    return  # the finally block persists this FAILED state

            spec = RunSpec(
                cmd=cmd,
                env=script_env,
                cwd=str(workdir),
                timeout=run.script.timeout_seconds,
                limits=_resolve_run_limits(),
            )
            handle = backend.start(spec)

            # Record the PID so the web process can kill this exact job tree.
            run.pid = handle.pid
            run.save(update_fields=["pid"])

            result = backend.wait(handle, spec.timeout)

            if result.timed_out:
                run.status = Run.Status.TIMEOUT
                run.stdout = _truncate_output(
                    _mask_secrets_in_output(result.stdout or "", secrets)
                )
                run.stderr = _truncate_output(
                    _mask_secrets_in_output(result.stderr or "", secrets)
                )
                if run.stderr:
                    run.stderr += "\n\n[TIMEOUT: Script exceeded maximum execution time]"
                else:
                    run.stderr = (
                        f"[TIMEOUT: Script exceeded {run.script.timeout_seconds} seconds]"
                    )
                run.exit_code = -1
                logger.warning(
                    f"Run {run.id} timed out after {run.script.timeout_seconds}s"
                )
            else:
                # Process results - mask secrets in output
                run.stdout = _truncate_output(_mask_secrets_in_output(result.stdout, secrets))
                run.stderr = _truncate_output(_mask_secrets_in_output(result.stderr, secrets))
                run.exit_code = result.exit_code
                run.status = (
                    Run.Status.SUCCESS if result.exit_code == 0 else Run.Status.FAILED
                )

        except subprocess.SubprocessError as e:
            # Handle other subprocess errors
            run.status = Run.Status.FAILED
            run.stderr = f"Subprocess error: {str(e)}"
            run.exit_code = -1
            logger.error(f"Run {run.id} subprocess error: {e}")

    except Exception as e:
        # Catch-all for unexpected errors
        run.status = Run.Status.FAILED
        run.stderr = f"Unexpected executor error: {str(e)}\n\n{traceback.format_exc()}"
        run.exit_code = -1
        logger.exception(f"Run {run.id} unexpected error")

    finally:
        # Phase 4: Cleanup and save
        # Always set end time if not already set
        if not run.ended_at:
            run.ended_at = timezone.now()

        # Persist results — but never clobber a CANCELLED status set externally
        # by the web process when it killed this job (TaskService.force_stop_task).
        # A plain refresh-then-save would leave a race window where a kill landing
        # between the two could be overwritten, so use a conditional UPDATE that
        # excludes CANCELLED. This is the documented gotcha for this feature.
        updated = (
            Run.objects.filter(pk=run.pk)
            .exclude(status=Run.Status.CANCELLED)
            .update(
                status=run.status,
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                ended_at=run.ended_at,
                pid=None,
            )
        )
        if not updated:
            # Externally cancelled/killed — keep that terminal state; only make
            # sure the now-dead PID is cleared so it can't be reused for a kill.
            Run.objects.filter(pk=run.pk).update(pid=None)
            run.status = Run.Status.CANCELLED
        run.pid = None

        # Cleanup temporary file
        if script_file_path is not None:
            try:
                os.unlink(script_file_path)
            except OSError as e:
                logger.warning(f"Failed to delete temp script file: {e}")

        logger.info(
            f"Run {run.id} completed with status {run.status} "
            f"(exit_code={run.exit_code})"
        )


def run_in_environment(
    environment,
    *,
    code: str | None = None,
    path: str | None = None,
    args: list | None = None,
    timeout: int | None = 60,
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Run a helper in a PyRunner environment's venv as an isolated subprocess.

    This is the safe path for plugin *compute*: third-party packages run in the
    chosen environment's venv (a subprocess), never inside the Django process —
    so a bad package fails a *call*, not the server. It reuses the executor's
    hardened pattern: the environment's Python, process-group/session isolation,
    a timeout, and captured + size-capped output.

    Provide exactly one of ``code`` (a string of Python source) or ``path`` (an
    absolute path to a ``.py`` file already on disk). ``args`` are passed as argv.

    Returns ``(exit_code, stdout, stderr)``. On timeout the whole process tree is
    killed and ``exit_code`` is ``-1``.
    """
    if (code is None) == (path is None):
        raise ValueError("Provide exactly one of `code` or `path`.")

    if not environment.exists():
        raise EnvironmentNotFoundError(
            f"Environment directory not found: {environment.get_full_path()}"
        )
    python_path = environment.get_python_executable()
    if not os.path.isfile(python_path):
        raise PythonNotFoundError(f"Python executable not found: {python_path}")

    workdir = Path(settings.SCRIPTS_WORKDIR)
    workdir.mkdir(parents=True, exist_ok=True)

    temp_file = None
    try:
        if code is not None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
                dir=str(workdir),
            ) as fh:
                fh.write(code)
                temp_file = fh.name
            script_path = temp_file
        else:
            script_path = path

        cmd = [python_path, script_path] + [str(a) for a in (args or [])]

        run_env = dict(os.environ)
        if env:
            run_env.update({k: str(v) for k, v in env.items()})

        # Same single execution path as execute_run: the configured backend
        # spawns in an isolated process group/session and kills the tree on
        # timeout. No Run row here — plugin compute just gets (code, out, err).
        backend = get_run_backend()
        spec = RunSpec(
            cmd=cmd,
            env=run_env,
            cwd=str(cwd) if cwd else str(workdir),
            timeout=timeout,
        )
        handle = backend.start(spec)
        result = backend.wait(handle, spec.timeout)
        if result.timed_out:
            stderr = (result.stderr or "") + f"\n\n[TIMEOUT: exceeded {timeout}s]"
            return -1, _truncate_output(result.stdout or ""), _truncate_output(stderr)
        return (
            result.exit_code,
            _truncate_output(result.stdout or ""),
            _truncate_output(result.stderr or ""),
        )
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
