"""
Async tasks for PyRunner using django-q2.

This module contains task functions that are executed asynchronously
by django-q2 workers.
"""

import logging
from uuid import UUID

from django_q.tasks import async_task

from core.models import Run

logger = logging.getLogger(__name__)


def execute_run_task(run_id: str) -> dict:
    """
    Execute a script run asynchronously.

    This is the task function called by django-q2 workers.
    It fetches the Run by ID and delegates to the executor.

    Args:
        run_id: The UUID of the Run record (as string)

    Returns:
        dict: Execution result summary for logging/monitoring
    """
    from core.executor import execute_run  # Import here to avoid circular imports

    try:
        run = Run.objects.select_related("script", "script__environment").get(
            id=UUID(run_id)
        )
    except Run.DoesNotExist:
        logger.error(f"Run {run_id} not found - task cannot execute")
        return {
            "success": False,
            "run_id": run_id,
            "error": "Run not found",
        }
    except ValueError as e:
        logger.error(f"Invalid run_id format: {run_id} - {e}")
        return {
            "success": False,
            "run_id": run_id,
            "error": f"Invalid UUID format: {e}",
        }

    execute_run(run)
    run.refresh_from_db()

    logger.info(f"Task completed for Run {run_id} with status {run.status}")

    return {
        "success": run.status == Run.Status.SUCCESS,
        "run_id": run_id,
        "status": run.status,
        "exit_code": run.exit_code,
    }


def queue_script_run(run: Run) -> str:
    """
    Queue a Run for async execution.

    This is the main entry point for queuing script runs.
    It handles setting up the async task and storing the task_id.

    Args:
        run: The Run model instance to execute

    Returns:
        str: The django-q2 task ID

    Raises:
        ValueError: If the run is not in PENDING status
    """
    if run.status != Run.Status.PENDING:
        raise ValueError(
            f"Cannot queue run {run.id}: status is {run.status}, expected PENDING"
        )

    task_id = async_task(
        "core.tasks.execute_run_task",
        str(run.id),
        task_name=f"run-{run.id}",
        timeout=run.script.timeout_seconds + 60,
    )

    run.task_id = task_id
    run.save(update_fields=["task_id"])

    logger.info(f"Queued Run {run.id} as task {task_id}")

    return task_id
