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
from core.services.library_service import resync_dev_libraries, stamp_library_versions

logger = logging.getLogger(__name__)


def execute_run_task(run_id: str, webhook_data: dict | None = None) -> dict:
    """
    Execute a script run asynchronously.

    This is the task function called by django-q2 workers.
    It fetches the Run by ID and delegates to the executor.

    Args:
        run_id: The UUID of the Run record (as string)
        webhook_data: Optional webhook data to inject as environment variables

    Returns:
        dict: Execution result summary for logging/monitoring
    """
    from core.executor import execute_run  # Import here to avoid circular imports
    from core.services.notification_service import NotificationService

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

    execute_run(run, webhook_data=webhook_data)
    run.refresh_from_db()

    # Send notifications after run completion
    try:
        notification_results = NotificationService.send_notification(run)
        if (
            notification_results.get("email_sent")
            or notification_results.get("webhook_sent")
            or notification_results.get("channels_sent")
        ):
            logger.info(f"Notifications sent for run {run_id}: {notification_results}")
    except Exception as e:
        logger.error(f"Failed to send notifications for run {run_id}: {e}")

    logger.info(f"Task completed for Run {run_id} with status {run.status}")

    return {
        "success": run.status == Run.Status.SUCCESS,
        "run_id": run_id,
        "status": run.status,
        "exit_code": run.exit_code,
    }


def queue_script_run(run: Run, webhook_data: dict | None = None) -> str:
    """
    Queue a Run for async execution.

    This is the main entry point for queuing script runs.
    It handles setting up the async task and storing the task_id.

    Args:
        run: The Run model instance to execute
        webhook_data: Optional webhook data to inject as environment variables

    Returns:
        str: The django-q2 task ID

    Raises:
        ValueError: If the run is not in PENDING status
    """
    if run.status != Run.Status.PENDING:
        raise ValueError(
            f"Cannot queue run {run.id}: status is {run.status}, expected PENDING"
        )

    # Dev mode only: re-read the owning plugin's lib/ folder first, so an edit on
    # disk is picked up by the next run with no manual sync. Inert in production
    # (settings.DEV_PLUGIN is None there), where libraries stay DB-deterministic.
    resync_dev_libraries(run.script)

    # Pin each attached Library to its head revision, HERE rather than at each
    # Run.objects.create site: every trigger (manual, scheduled, webhook, channel,
    # plugin SDK) funnels through this function, so stamping here is a structural
    # guarantee that all entry points stamp — not a rule six call sites must
    # remember. Must happen at queue time, before the worker can pick the run up:
    # stamping at execute would let an edit in between change what the run imports.
    stamp_library_versions(run)

    task_id = async_task(
        "core.tasks.execute_run_task",
        str(run.id),
        webhook_data,
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

    # Check if script can run (enabled and not archived)
    if not script.can_run:
        reason = "archived" if script.is_archived else "disabled"
        logger.info(f"Scheduled run for script {script.name} skipped - script {reason}")
        return {"success": False, "error": f"Script {reason}"}

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

    # Create the run. A scheduled run fires with no request/user — its workspace
    # comes from the script (tenancy Stage 1), the only authoritative source.
    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
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
    schedule.next_run = ScheduleService.calculate_next_run(schedule)
    schedule.save(update_fields=["next_run"])

    logger.info(f"Scheduled run {run.id} created for script {script.name}")

    return {
        "success": True,
        "run_id": str(run.id),
        "script_id": script_id,
    }


def execute_yearly_scheduled_run(schedule_id: str) -> dict:
    """Run a yearly schedule only when its configured local date exists.

    A timezone conversion can place 29 February on 28 February or 1 March in
    UTC. The cron therefore fires in non-leap years too; this guard preserves
    the schedule's local-calendar semantics before delegating to the normal
    scheduled-run path.
    """
    from core.services.schedule_service import ScheduleService

    try:
        schedule = ScriptSchedule.objects.select_related("script").get(id=UUID(schedule_id))
    except ScriptSchedule.DoesNotExist:
        return {"success": False, "error": "Schedule not found"}
    except ValueError as exc:
        return {"success": False, "error": f"Invalid UUID format: {exc}"}

    if not schedule.is_active or schedule.run_mode != ScriptSchedule.RunMode.YEARLY:
        return {"success": False, "error": "Yearly schedule inactive"}

    local_now = timezone.now().astimezone(ScheduleService._schedule_tz(schedule))
    if (local_now.month, local_now.day) != (
        schedule.yearly_month,
        schedule.yearly_day,
    ):
        logger.info(
            "Yearly schedule %s skipped outside configured local date",
            schedule.id,
        )
        return {"success": False, "error": "Outside configured local date"}

    return execute_scheduled_run(str(schedule.script_id))


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

    # Don't re-run an operation that already reached a terminal state. A stale
    # django-q2 re-queue (or a manual replay) must not flip a finished/reconciled
    # operation back to "running" and repeat the pip install.
    if operation.is_finished:
        logger.info(
            f"Package operation {operation_id} already {operation.status}; skipping re-run"
        )
        return {
            "success": operation.is_successful,
            "operation_id": operation_id,
            "status": operation.status,
            "skipped": True,
        }

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


def cleanup_old_runs_task() -> dict:
    """
    Clean up old runs based on retention settings.
    Called by django-q2 scheduler or manually.

    Returns:
        dict: Cleanup result summary with count of deleted runs
    """
    from core.services.retention_service import RetentionService

    try:
        deleted_count = RetentionService.cleanup_all_runs()

        # Update last_cleanup_at timestamp
        settings = GlobalSettings.get_settings()
        settings.last_cleanup_at = timezone.now()
        settings.save(update_fields=["last_cleanup_at"])

        logger.info(f"Cleanup task completed: {deleted_count} runs deleted")
        return {"success": True, "deleted_count": deleted_count}
    except Exception as e:
        logger.exception("Cleanup task failed")
        return {"success": False, "error": str(e)}


def check_for_updates_task() -> dict:
    """
    Check GitHub for a newer PyRunner release and store the result.
    Called by django-q2 scheduler (daily) or manually.

    Returns:
        dict: Result with the latest version found, or an error message.
    """
    from core.services.update_service import UpdateService

    try:
        latest = UpdateService.refresh()
        logger.info(f"Update check completed: latest version on GitHub is {latest}")
        return {"success": True, "latest_version": latest}
    except Exception as e:
        # Network/rate-limit errors are non-fatal — just try again next run.
        logger.warning(f"Update check failed: {e}")
        return {"success": False, "error": str(e)}


def resync_schedules_task() -> dict:
    """
    Rebuild timezone-aware schedule crons with today's UTC offsets.

    Cron strings can't encode DST rules: sync_schedule converts a schedule's
    local times to UTC using the offset valid at sync time. This daily task
    re-runs sync for every non-UTC schedule (and the scheduled backup) so a
    DST transition drifts fire times for at most a day. UTC schedules never
    drift, so they are skipped.

    Returns:
        dict: Result with the number of schedules resynced.
    """
    from core.services.backup_schedule_service import BackupScheduleService
    from core.services.schedule_service import ScheduleService

    try:
        settings = GlobalSettings.get_settings()
        if settings.schedules_paused:
            return {"success": True, "resynced": 0, "note": "schedules paused"}

        count = 0
        for schedule in (
            ScriptSchedule.objects.filter(
                is_active=True,
                run_mode__in=[
                    ScriptSchedule.RunMode.DAILY,
                    ScriptSchedule.RunMode.WEEKLY,
                    ScriptSchedule.RunMode.MONTHLY,
                    ScriptSchedule.RunMode.CRON,
                    ScriptSchedule.RunMode.YEARLY,
                ],
            )
            .exclude(timezone__in=["", "UTC"])
            .select_related("script")
        ):
            ScheduleService.sync_schedule(schedule)
            count += 1

        if (settings.timezone or "UTC") != "UTC":
            BackupScheduleService.sync_schedule()

        if count:
            logger.info(f"Resynced {count} timezone-aware schedules")
        return {"success": True, "resynced": count}
    except Exception as e:
        logger.warning(f"Schedule resync failed: {e}")
        return {"success": False, "error": str(e)}


def worker_heartbeat_task() -> dict:
    """
    Heartbeat task that runs periodically to indicate workers are alive.
    Updates the worker_heartbeat_at timestamp in GlobalSettings.

    This task is scheduled to run every minute when qcluster starts.
    It allows the system to detect when workers have stopped.

    Returns:
        dict: Result with timestamp of heartbeat
    """
    try:
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])

        # Advertise the retry window this cluster actually booted with (the
        # web process only has env defaults in settings.Q_CLUSTER — the DB
        # values are applied by pyrunner_qcluster after apps are ready). The
        # script form reads this to warn when a timeout outruns the RUNNING
        # workers. Best-effort: the heartbeat must never fail over it.
        try:
            from django.core.cache import cache
            from django_q.conf import Conf

            from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY

            cache.set(EFFECTIVE_RETRY_CACHE_KEY, Conf.RETRY, None)
        except Exception:
            logger.exception("Failed to advertise effective worker settings")

        # Piggyback stale-run reconciliation on the heartbeat: it already runs
        # every minute on the cluster, which is the cadence stale detection
        # needs — plugin guards and API consumers read run status without ever
        # loading a page that could reconcile. Never fails the heartbeat.
        try:
            reconciled = Run.reconcile_stale()
            if reconciled:
                logger.warning(
                    f"Reconciled {reconciled} stale run(s) whose worker died "
                    "without finalizing them"
                )
        except Exception:
            logger.exception("Stale-run reconciliation failed")

        # Same for package operations: the packages page used to be the only
        # reconciler, so an install whose worker died stayed "running" (and the
        # page kept polling) until someone opened it.
        try:
            from core.models import PackageOperation

            stale_ops = PackageOperation.reconcile_stale()
            if stale_ops:
                logger.warning(f"Reconciled {stale_ops} stale package operation(s)")
        except Exception:
            logger.exception("Stale package-operation reconciliation failed")

        logger.debug("Worker heartbeat updated")
        return {"success": True, "heartbeat_at": str(settings.worker_heartbeat_at)}
    except Exception as e:
        logger.exception("Worker heartbeat failed")
        return {"success": False, "error": str(e)}


def scheduled_backup_task() -> dict:
    """
    Execute a scheduled backup to S3.
    Called by django-q2 scheduler.

    Creates a backup, uploads to S3, and applies retention policy.

    Returns:
        dict: Backup result summary
    """
    from core.services.backup_service import BackupService
    from core.services.s3_service import S3Service
    from core.services.backup_schedule_service import BackupScheduleService

    settings = GlobalSettings.get_settings()

    # Validate the backup storage connection is configured and switched on.
    connection = S3Service.for_backup()
    if connection is None or not connection.enabled:
        logger.warning("Scheduled backup skipped - S3 not enabled")
        return {"success": False, "error": "S3 not enabled"}

    if not S3Service.is_configured(connection):
        logger.warning("Scheduled backup skipped - S3 not configured")
        return {"success": False, "error": "S3 not configured"}

    try:
        # Create backup
        backup_data = BackupService.create_backup(
            include_runs=settings.s3_backup_include_runs,
            max_runs=settings.s3_backup_max_runs,
            include_package_operations=False,
            include_datastores=settings.s3_backup_include_datastores,
            created_by_user=None,  # System backup
        )

        # Serialize to gzip
        file_bytes, _ = BackupService.serialize_backup(
            backup_data,
            format=BackupService.FORMAT_GZIP,
        )

        # Generate S3 key
        key = S3Service.generate_backup_key()

        # Upload to S3
        result = S3Service.upload_file(connection, file_bytes, key)

        if result["success"]:
            # Update tracking
            settings.s3_backup_last_run_at = timezone.now()
            settings.s3_backup_last_status = "success"
            settings.s3_backup_last_error = ""
            settings.s3_backup_last_size = result["size"]
            settings.save(update_fields=[
                "s3_backup_last_run_at",
                "s3_backup_last_status",
                "s3_backup_last_error",
                "s3_backup_last_size",
            ])

            # Apply retention cleanup
            deleted = BackupScheduleService.apply_retention()

            logger.info(f"Scheduled backup completed: {key} ({result['size']} bytes)")

            return {
                "success": True,
                "key": key,
                "size": result["size"],
                "retention_deleted": deleted,
            }
        else:
            # Update tracking with error
            settings.s3_backup_last_run_at = timezone.now()
            settings.s3_backup_last_status = "failed"
            settings.s3_backup_last_error = result.get("error", "Unknown error")
            settings.save(update_fields=[
                "s3_backup_last_run_at",
                "s3_backup_last_status",
                "s3_backup_last_error",
            ])

            logger.error(f"Scheduled backup failed: {result.get('error')}")
            return {"success": False, "error": result.get("error")}

    except Exception as e:
        # Update tracking with error
        settings.s3_backup_last_run_at = timezone.now()
        settings.s3_backup_last_status = "failed"
        settings.s3_backup_last_error = str(e)
        settings.save(update_fields=[
            "s3_backup_last_run_at",
            "s3_backup_last_status",
            "s3_backup_last_error",
        ])

        logger.exception("Scheduled backup task failed")
        return {"success": False, "error": str(e)}
