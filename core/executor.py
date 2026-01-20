"""
Script executor module for PyRunner.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.
"""

import logging
import os
import subprocess
import tempfile
import traceback
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.models import Run, Secret
from core.services import EncryptionService

logger = logging.getLogger(__name__)

# Maximum output size (1MB) to prevent database bloat
MAX_OUTPUT_BYTES = 1_000_000


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


def _build_script_environment() -> dict:
    """
    Build the environment dict for script execution.

    Combines system environment with secrets.
    Secrets override any same-named system variables.

    Returns:
        Environment dict to pass to subprocess
    """
    # Start with system environment
    env = os.environ.copy()

    # Add secrets (overriding any existing vars with same name)
    secrets = _get_secrets_env()
    env.update(secrets)

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


def execute_run(run: Run) -> None:
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

            # Build environment with secrets injected
            script_env = _build_script_environment()
            secrets = _get_secrets_env()

            # Subprocess kwargs
            kwargs = {
                "capture_output": True,
                "timeout": run.script.timeout_seconds,
                "cwd": str(workdir),
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": script_env,
            }

            # Windows-specific: prevent console window popup
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            # Process results - mask secrets in output
            run.stdout = _truncate_output(_mask_secrets_in_output(result.stdout, secrets))
            run.stderr = _truncate_output(_mask_secrets_in_output(result.stderr, secrets))
            run.exit_code = result.returncode
            run.status = (
                Run.Status.SUCCESS if result.returncode == 0 else Run.Status.FAILED
            )

        except subprocess.TimeoutExpired as e:
            # Handle timeout - process is automatically killed
            run.status = Run.Status.TIMEOUT
            stdout_raw = e.stdout or "" if e.stdout else ""
            stderr_raw = e.stderr or "" if e.stderr else ""
            run.stdout = _truncate_output(_mask_secrets_in_output(stdout_raw, secrets))
            run.stderr = _truncate_output(_mask_secrets_in_output(stderr_raw, secrets))
            if run.stderr:
                run.stderr += "\n\n[TIMEOUT: Script exceeded maximum execution time]"
            else:
                run.stderr = (
                    f"[TIMEOUT: Script exceeded {run.script.timeout_seconds} seconds]"
                )
            run.exit_code = -1
            logger.warning(f"Run {run.id} timed out after {run.script.timeout_seconds}s")

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

        # Always save the run state
        run.save()

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
