"""
Script executor module for PyRunner.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.
"""

import json
import logging
import os
import signal
import subprocess
import tempfile
import traceback
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.models import Run, Secret
from core.services import ClaudeService, EncryptionService

logger = logging.getLogger(__name__)

# Maximum output size (1MB) to prevent database bloat
MAX_OUTPUT_BYTES = 1_000_000


def _kill_process_tree(pid: int) -> None:
    """
    Kill a script job's entire process tree.

    This targets *only* the child process the executor spawned (the user's
    Python script and anything it spawned) — never the long-lived django-q
    worker. The child is launched in its own process group/session (see
    ``execute_run``) so the kill is isolated from the worker.

    Args:
        pid: PID of the spawned script subprocess (the process-group leader).
    """
    if not pid:
        return
    try:
        if os.name == "nt":
            # /T kills the whole tree (children too), /F forces it.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            # Child was started with start_new_session=True, so it leads its own
            # process group; killing the group takes out the script + children.
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        # Process already gone (or PID reused/invalid) — nothing to do.
        logger.warning(f"Could not kill process tree {pid}: {e}")


def _get_secrets_env() -> dict:
    """
    Get all secrets as environment variables.

    Returns:
        Dict of {key: decrypted_value} for all secrets
    """
    secrets_env = {}

    # Only try to get secrets if encryption is configured
    if not EncryptionService.is_configured():
        logger.debug("Encryption not configured - secrets will not be injected")
        return secrets_env

    try:
        for secret in Secret.objects.all():
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

    # Add secrets (overriding any existing vars with same name)
    secrets = _get_secrets_env()
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

    # Add DataStore support
    # Set the database path for the pyrunner_datastore module
    env["PYRUNNER_DB_PATH"] = str(settings.DATABASES["default"]["NAME"])

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
            secrets = _get_secrets_env()

            # Also mask the injected Claude credential in output, if any.
            claude_env = ClaudeService.get_script_env()
            for cred_key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if claude_env.get(cred_key):
                    secrets[cred_key] = claude_env[cred_key]

            # Launch the script in its own process group/session so the web
            # process can later kill the whole job tree (force stop / timeout)
            # without touching the django-q worker that spawned it.
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": str(workdir),
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": script_env,
            }
            if os.name == "nt":
                # CREATE_NO_WINDOW: no console popup. CREATE_NEW_PROCESS_GROUP:
                # isolate the child so signals/kills don't reach the worker.
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                # Child becomes the leader of a new session/process group.
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)

            # Record the PID so the web process can kill this exact job tree.
            run.pid = proc.pid
            run.save(update_fields=["pid"])

            try:
                stdout, stderr = proc.communicate(timeout=run.script.timeout_seconds)
                returncode = proc.returncode

                # Process results - mask secrets in output
                run.stdout = _truncate_output(_mask_secrets_in_output(stdout, secrets))
                run.stderr = _truncate_output(_mask_secrets_in_output(stderr, secrets))
                run.exit_code = returncode
                run.status = (
                    Run.Status.SUCCESS if returncode == 0 else Run.Status.FAILED
                )

            except subprocess.TimeoutExpired:
                # Kill the whole job tree (not just the immediate child), then
                # drain whatever output was buffered before the kill.
                _kill_process_tree(proc.pid)
                stdout, stderr = proc.communicate()
                run.status = Run.Status.TIMEOUT
                run.stdout = _truncate_output(
                    _mask_secrets_in_output(stdout or "", secrets)
                )
                run.stderr = _truncate_output(
                    _mask_secrets_in_output(stderr or "", secrets)
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
