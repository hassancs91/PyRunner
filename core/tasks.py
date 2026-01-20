"""
Async tasks for PyRunner using django-q2.

This module contains task functions that are executed asynchronously
by django-q2 workers.
"""

import logging
from uuid import UUID

from django.utils import timezone
from django_q.tasks import async_task

from core.models import Run, Script, ScriptSchedule, GlobalSettings, PackageOperation

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


def execute_scheduled_run(script_id: str) -> dict:
    """
    Execute a scheduled script run.
    Called by django-q2 scheduler.

    Args:
        script_id: The UUID of the Script (as string)

    Returns:
        dict: Execution result summary
    """
    from core.services.schedule_service import ScheduleService

    # Check global pause
    settings = GlobalSettings.get_settings()
    if settings.schedules_paused:
        logger.info(f"Scheduled run for script {script_id} skipped - schedules paused")
        return {"success": False, "error": "Schedules globally paused"}

    try:
        script = Script.objects.get(id=UUID(script_id))
    except Script.DoesNotExist:
        logger.error(f"Script {script_id} not found for scheduled run")
        return {"success": False, "error": "Script not found"}
    except ValueError as e:
        logger.error(f"Invalid script_id format: {script_id} - {e}")
        return {"success": False, "error": f"Invalid UUID format: {e}"}

    # Check if script is enabled
    if not script.is_enabled:
        logger.info(f"Scheduled run for script {script.name} skipped - script disabled")
        return {"success": False, "error": "Script disabled"}

    # Check if schedule is active
    try:
        schedule = script.schedule
        if not schedule.is_active:
            logger.info(
                f"Scheduled run for script {script.name} skipped - schedule inactive"
            )
            return {"success": False, "error": "Schedule inactive"}
    except ScriptSchedule.DoesNotExist:
        logger.error(f"No schedule found for script {script.name}")
        return {"success": False, "error": "No schedule"}

    # Create the run
    run = Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=None,  # System-triggered
        trigger_type=Run.TriggerType.SCHEDULED,
        code_snapshot=script.code,
    )

    # Update schedule tracking
    schedule.last_scheduled_run = timezone.now()
    schedule.save(update_fields=["last_scheduled_run"])

    # Queue for execution
    queue_script_run(run)

    # Update next_run cache
    schedule.next_run = ScheduleService._calculate_next_run(schedule)
    schedule.save(update_fields=["next_run"])

    logger.info(f"Scheduled run {run.id} created for script {script.name}")

    return {
        "success": True,
        "run_id": str(run.id),
        "script_id": script_id,
    }


def execute_package_operation(operation_id: str) -> dict:
    """
    Execute a package operation (install/uninstall) asynchronously.
    Called by django-q2 workers.

    Args:
        operation_id: The UUID of the PackageOperation record (as string)

    Returns:
        dict: Operation result summary
    """
    from core.services.environment_service import EnvironmentService

    try:
        operation = PackageOperation.objects.select_related("environment").get(
            id=UUID(operation_id)
        )
    except PackageOperation.DoesNotExist:
        logger.error(f"PackageOperation {operation_id} not found")
        return {"success": False, "error": "Operation not found"}
    except ValueError as e:
        logger.error(f"Invalid operation_id format: {operation_id} - {e}")
        return {"success": False, "error": f"Invalid UUID format: {e}"}

    # Update to running
    operation.status = PackageOperation.Status.RUNNING
    operation.started_at = timezone.now()
    operation.save(update_fields=["status", "started_at"])

    environment = operation.environment

    try:
        if operation.operation == PackageOperation.Operation.INSTALL:
            success, stdout, stderr = EnvironmentService.install_package(
                environment, operation.package_spec
            )
        elif operation.operation == PackageOperation.Operation.UNINSTALL:
            success, stdout, stderr = EnvironmentService.uninstall_package(
                environment, operation.package_spec
            )
        elif operation.operation == PackageOperation.Operation.BULK_INSTALL:
            success, stdout, stderr = EnvironmentService.install_requirements(
                environment, operation.package_spec
            )
        else:
            success, stdout, stderr = False, "", "Unknown operation type"

        operation.output = stdout
        operation.error = stderr
        operation.status = (
            PackageOperation.Status.SUCCESS
            if success
            else PackageOperation.Status.FAILED
        )

        # Update environment requirements cache on success
        if success:
            environment.requirements = EnvironmentService.pip_freeze(environment)
            environment.save(update_fields=["requirements", "updated_at"])

    except Exception as e:
        operation.status = PackageOperation.Status.FAILED
        operation.error = str(e)
        logger.exception(f"Package operation {operation_id} failed with exception")

    operation.completed_at = timezone.now()
    operation.save()

    logger.info(
        f"Package operation {operation_id} completed with status {operation.status}"
    )

    return {
        "success": operation.status == PackageOperation.Status.SUCCESS,
        "operation_id": operation_id,
        "status": operation.status,
    }
