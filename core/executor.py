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

from core.models import Run

logger = logging.getLogger(__name__)

# Maximum output size (1MB) to prevent database bloat
MAX_OUTPUT_BYTES = 1_000_000


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

            # Subprocess kwargs
            kwargs = {
                "capture_output": True,
                "timeout": run.script.timeout_seconds,
                "cwd": str(workdir),
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }

            # Windows-specific: prevent console window popup
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)

            # Process results
            run.stdout = _truncate_output(result.stdout)
            run.stderr = _truncate_output(result.stderr)
            run.exit_code = result.returncode
            run.status = (
                Run.Status.SUCCESS if result.returncode == 0 else Run.Status.FAILED
            )

        except subprocess.TimeoutExpired as e:
            # Handle timeout - process is automatically killed
            run.status = Run.Status.TIMEOUT
            run.stdout = _truncate_output(e.stdout or "") if e.stdout else ""
            run.stderr = _truncate_output(e.stderr or "") if e.stderr else ""
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
