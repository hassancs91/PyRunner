"""
Scheduled backup service for S3 backups.

Manages django-q2 schedules for automated S3 backups.
"""

import logging
from datetime import timedelta

from django.utils import timezone
from django_q.models import Schedule as QSchedule

from core.models import GlobalSettings

logger = logging.getLogger(__name__)

BACKUP_SCHEDULE_NAME = "pyrunner-scheduled-backup"


class BackupScheduleService:
    """
    Service for managing scheduled S3 backups.
    """

    @classmethod
    def sync_schedule(cls) -> bool:
        """
        Synchronize django-q2 schedule with current settings.
        Creates, updates, or deletes schedule as needed.

        Returns:
            bool: True if schedule is active
        """
        from core.services.s3_service import S3Service

        settings = GlobalSettings.get_settings()

        # Delete existing schedule
        QSchedule.objects.filter(name=BACKUP_SCHEDULE_NAME).delete()

        # Check if should create new schedule
        if not settings.s3_backup_enabled:
            logger.info("Scheduled backup disabled")
            return False

        if settings.s3_backup_schedule == GlobalSettings.S3BackupSchedule.DISABLED:
            logger.info("Scheduled backup set to disabled")
            return False

        if not settings.s3_enabled or not S3Service.is_configured():
            logger.warning("S3 backup enabled but S3 not properly configured")
            return False

        # Calculate next run time
        next_run = cls._calculate_next_run(settings)

        # Create schedule based on frequency
        if settings.s3_backup_schedule == GlobalSettings.S3BackupSchedule.DAILY:
            QSchedule.objects.create(
                name=BACKUP_SCHEDULE_NAME,
                func="core.tasks.scheduled_backup_task",
                schedule_type=QSchedule.DAILY,
                next_run=next_run,
                repeats=-1,
            )
            logger.info(f"Created daily backup schedule, next run at {next_run}")

        elif settings.s3_backup_schedule == GlobalSettings.S3BackupSchedule.WEEKLY:
            # Use CRON for weekly (minute hour * * day_of_week)
            # Convert Python weekday (0=Mon) to cron weekday (0=Sun)
            cron_day = (settings.s3_backup_day + 1) % 7
            cron_expr = f"{settings.s3_backup_time.minute} {settings.s3_backup_time.hour} * * {cron_day}"

            QSchedule.objects.create(
                name=BACKUP_SCHEDULE_NAME,
                func="core.tasks.scheduled_backup_task",
                schedule_type=QSchedule.CRON,
                cron=cron_expr,
                next_run=next_run,
                repeats=-1,
            )
            logger.info(f"Created weekly backup schedule (cron: {cron_expr}), next run at {next_run}")

        return True

    @classmethod
    def _calculate_next_run(cls, settings) -> timezone.datetime:
        """
        Calculate next backup run time.

        Args:
            settings: GlobalSettings instance

        Returns:
            datetime: Next scheduled run time
        """
        now = timezone.now()
        backup_time = settings.s3_backup_time

        # Create datetime for today at backup time
        next_run = now.replace(
            hour=backup_time.hour,
            minute=backup_time.minute,
            second=0,
            microsecond=0,
        )

        if settings.s3_backup_schedule == GlobalSettings.S3BackupSchedule.DAILY:
            if next_run <= now:
                next_run += timedelta(days=1)

        elif settings.s3_backup_schedule == GlobalSettings.S3BackupSchedule.WEEKLY:
            # Calculate days until target weekday
            current_weekday = now.weekday()
            target_weekday = settings.s3_backup_day
            days_ahead = target_weekday - current_weekday

            if days_ahead < 0 or (days_ahead == 0 and next_run <= now):
                days_ahead += 7

            next_run += timedelta(days=days_ahead)

        return next_run

    @classmethod
    def get_schedule_status(cls) -> dict:
        """
        Get current backup schedule status for UI.

        Returns:
            dict: Status information for display
        """
        from core.services.s3_service import S3Service

        settings = GlobalSettings.get_settings()
        schedule = QSchedule.objects.filter(name=BACKUP_SCHEDULE_NAME).first()

        return {
            "enabled": settings.s3_backup_enabled,
            "schedule": settings.s3_backup_schedule,
            "time": settings.s3_backup_time.strftime("%H:%M") if settings.s3_backup_time else "02:00",
            "day": settings.s3_backup_day,
            "day_name": cls._get_day_name(settings.s3_backup_day),
            "prefix": settings.s3_backup_prefix,
            "next_run": schedule.next_run if schedule else None,
            "last_run": settings.s3_backup_last_run_at,
            "last_status": settings.s3_backup_last_status,
            "last_error": settings.s3_backup_last_error,
            "last_size": settings.s3_backup_last_size,
            "retention_count": settings.s3_backup_retention_count,
            "include_runs": settings.s3_backup_include_runs,
            "include_datastores": settings.s3_backup_include_datastores,
            "s3_configured": S3Service.is_configured(),
            "s3_enabled": settings.s3_enabled,
        }

    @classmethod
    def _get_day_name(cls, day: int) -> str:
        """Get weekday name from number."""
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return days[day] if 0 <= day <= 6 else "Monday"

    @classmethod
    def apply_retention(cls) -> int:
        """
        Apply retention policy to S3 backups.
        Deletes old backups beyond retention count.

        Returns:
            int: Number of backups deleted
        """
        from core.services.s3_service import S3Service

        settings = GlobalSettings.get_settings()

        if settings.s3_backup_retention_count == 0:
            return 0  # Keep all

        # List existing backups
        prefix = settings.s3_backup_prefix.rstrip("/")
        files = S3Service.list_files(prefix)

        # Filter to only backup files (ending in .json.gz)
        backup_files = [f for f in files if f["key"].endswith(".json.gz")]

        # Sort by last_modified descending (newest first)
        backup_files.sort(key=lambda x: x["last_modified"], reverse=True)

        # Keep only the configured number
        files_to_delete = backup_files[settings.s3_backup_retention_count:]

        if not files_to_delete:
            return 0

        keys_to_delete = [f["key"] for f in files_to_delete]
        deleted = S3Service.delete_files(keys_to_delete)

        logger.info(f"Retention cleanup: deleted {deleted} old backups from S3")
        return deleted

    @classmethod
    def list_backups(cls) -> list[dict]:
        """
        List all backups in S3.

        Returns:
            list: List of backup files with metadata
        """
        from core.services.s3_service import S3Service

        settings = GlobalSettings.get_settings()

        if not settings.s3_enabled or not S3Service.is_configured():
            return []

        prefix = settings.s3_backup_prefix.rstrip("/")
        files = S3Service.list_files(prefix)

        # Filter to only backup files and sort by date
        backup_files = [f for f in files if f["key"].endswith(".json.gz")]
        backup_files.sort(key=lambda x: x["last_modified"], reverse=True)

        return backup_files
