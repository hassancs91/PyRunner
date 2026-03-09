"""
Service for managing django-q2 schedules.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from django.utils import timezone
from django_q.models import Schedule as QSchedule

logger = logging.getLogger(__name__)


class ScheduleService:
    """
    Manages the creation, update, and deletion of django-q2 Schedule objects
    based on ScriptSchedule configuration.
    """

    TASK_FUNC = "core.tasks.execute_scheduled_run"

    @classmethod
    def sync_schedule(cls, script_schedule) -> list[int]:
        """
        Synchronize django-q2 schedules with ScriptSchedule configuration.
        Deletes old schedules and creates new ones based on current config.

        Returns list of created django-q2 Schedule IDs.
        """
        from core.models import ScriptSchedule, GlobalSettings

        # Delete existing django-q2 schedules
        cls.delete_q_schedules(script_schedule)

        # If not active or manual mode, don't create new schedules
        if (
            not script_schedule.is_active
            or script_schedule.run_mode == ScriptSchedule.RunMode.MANUAL
        ):
            script_schedule.q_schedule_ids = []
            script_schedule.next_run = None
            script_schedule.save(update_fields=["q_schedule_ids", "next_run"])
            return []

        # Check global pause
        settings = GlobalSettings.get_settings()
        if settings.schedules_paused:
            logger.info(
                f"Schedules globally paused - not creating schedule for {script_schedule.script.name}"
            )
            script_schedule.q_schedule_ids = []
            script_schedule.next_run = None
            script_schedule.save(update_fields=["q_schedule_ids", "next_run"])
            return []

        q_schedule_ids = []

        if script_schedule.run_mode == ScriptSchedule.RunMode.INTERVAL:
            q_schedule_ids = cls._create_interval_schedule(script_schedule)
        elif script_schedule.run_mode == ScriptSchedule.RunMode.DAILY:
            q_schedule_ids = cls._create_daily_schedules(script_schedule)
        elif script_schedule.run_mode == ScriptSchedule.RunMode.WEEKLY:
            q_schedule_ids = cls._create_weekly_schedules(script_schedule)
        elif script_schedule.run_mode == ScriptSchedule.RunMode.MONTHLY:
            q_schedule_ids = cls._create_monthly_schedules(script_schedule)

        # Update the ScriptSchedule with new IDs and next_run
        script_schedule.q_schedule_ids = q_schedule_ids
        script_schedule.next_run = cls._calculate_next_run(script_schedule)
        script_schedule.save(update_fields=["q_schedule_ids", "next_run"])

        return q_schedule_ids

    @classmethod
    def _create_interval_schedule(cls, script_schedule) -> list[int]:
        """Create a MINUTES type django-q2 schedule."""
        q_schedule = QSchedule.objects.create(
            name=f"pyrunner-{script_schedule.script.id}",
            func=cls.TASK_FUNC,
            args=f"'{script_schedule.script.id}'",
            schedule_type=QSchedule.MINUTES,
            minutes=script_schedule.interval_minutes,
            repeats=-1,  # Run forever
            next_run=timezone.now(),
        )
        logger.info(
            f"Created interval schedule {q_schedule.id} for script {script_schedule.script.name}"
        )
        return [q_schedule.id]

    @classmethod
    def _create_daily_schedules(cls, script_schedule) -> list[int]:
        """
        Create CRON type django-q2 schedules for each daily time.
        Returns list of created schedule IDs.
        """
        q_schedule_ids = []

        for time_str in script_schedule.daily_times:
            hour, minute = map(int, time_str.split(":"))

            # Create cron expression: minute hour * * *
            cron_expr = f"{minute} {hour} * * *"

            q_schedule = QSchedule.objects.create(
                name=f"pyrunner-{script_schedule.script.id}-{time_str.replace(':', '')}",
                func=cls.TASK_FUNC,
                args=f"'{script_schedule.script.id}'",
                schedule_type=QSchedule.CRON,
                cron=cron_expr,
                repeats=-1,
                next_run=timezone.now(),
            )
            q_schedule_ids.append(q_schedule.id)
            logger.info(
                f"Created daily schedule {q_schedule.id} for script {script_schedule.script.name} at {time_str}"
            )

        return q_schedule_ids

    @classmethod
    def _create_weekly_schedules(cls, script_schedule) -> list[int]:
        """
        Create CRON type django-q2 schedules for weekly execution.
        Creates one schedule per time, with days combined in cron expression.
        """
        q_schedule_ids = []

        if not script_schedule.weekly_days or not script_schedule.weekly_times:
            return q_schedule_ids

        # Convert Python weekdays (0=Mon) to cron weekdays (0=Sun in standard cron, but django-q uses 1=Mon)
        # django-q2 uses standard cron: 0=Sunday, 1=Monday, ..., 6=Saturday
        # Our model uses: 0=Monday, ..., 6=Sunday
        # Convert: add 1 and mod 7 (Mon=0 -> 1, Sun=6 -> 0)
        cron_days = [(d + 1) % 7 for d in script_schedule.weekly_days]
        days_str = ",".join(str(d) for d in sorted(cron_days))

        for time_str in script_schedule.weekly_times:
            hour, minute = map(int, time_str.split(":"))

            # Cron format: minute hour * * day_of_week
            cron_expr = f"{minute} {hour} * * {days_str}"

            q_schedule = QSchedule.objects.create(
                name=f"pyrunner-{script_schedule.script.id}-weekly-{time_str.replace(':', '')}",
                func=cls.TASK_FUNC,
                args=f"'{script_schedule.script.id}'",
                schedule_type=QSchedule.CRON,
                cron=cron_expr,
                repeats=-1,
                next_run=timezone.now(),
            )
            q_schedule_ids.append(q_schedule.id)
            logger.info(
                f"Created weekly schedule {q_schedule.id} for script {script_schedule.script.name} "
                f"on days {days_str} at {time_str}"
            )

        return q_schedule_ids

    @classmethod
    def _create_monthly_schedules(cls, script_schedule) -> list[int]:
        """
        Create CRON type django-q2 schedules for monthly execution.
        Creates one schedule per time, with days combined in cron expression.
        """
        q_schedule_ids = []

        if not script_schedule.monthly_days or not script_schedule.monthly_times:
            return q_schedule_ids

        days_str = ",".join(str(d) for d in sorted(script_schedule.monthly_days))

        for time_str in script_schedule.monthly_times:
            hour, minute = map(int, time_str.split(":"))

            # Cron format: minute hour day_of_month * *
            cron_expr = f"{minute} {hour} {days_str} * *"

            q_schedule = QSchedule.objects.create(
                name=f"pyrunner-{script_schedule.script.id}-monthly-{time_str.replace(':', '')}",
                func=cls.TASK_FUNC,
                args=f"'{script_schedule.script.id}'",
                schedule_type=QSchedule.CRON,
                cron=cron_expr,
                repeats=-1,
                next_run=timezone.now(),
            )
            q_schedule_ids.append(q_schedule.id)
            logger.info(
                f"Created monthly schedule {q_schedule.id} for script {script_schedule.script.name} "
                f"on days {days_str} at {time_str}"
            )

        return q_schedule_ids

    @classmethod
    def delete_q_schedules(cls, script_schedule) -> int:
        """Delete all django-q2 schedules associated with a ScriptSchedule."""
        if not script_schedule.q_schedule_ids:
            return 0

        count = QSchedule.objects.filter(id__in=script_schedule.q_schedule_ids).delete()[
            0
        ]
        logger.info(
            f"Deleted {count} django-q2 schedules for script {script_schedule.script.name}"
        )
        return count

    @classmethod
    def _calculate_next_run(cls, script_schedule) -> Optional[datetime]:
        """Calculate the next scheduled run time based on schedule configuration."""
        from core.models import ScriptSchedule

        if not script_schedule.is_active:
            return None

        now = timezone.now()

        if script_schedule.run_mode == ScriptSchedule.RunMode.INTERVAL:
            # Next run is interval_minutes from now
            return now + timedelta(minutes=script_schedule.interval_minutes)

        elif script_schedule.run_mode == ScriptSchedule.RunMode.DAILY:
            # Calculate next occurrence from daily_times
            if not script_schedule.daily_times:
                return None

            candidates = []
            for time_str in script_schedule.daily_times:
                hour, minute = map(int, time_str.split(":"))
                # Today's occurrence
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    # Already passed today, use tomorrow
                    candidate += timedelta(days=1)
                candidates.append(candidate)

            return min(candidates) if candidates else None

        elif script_schedule.run_mode == ScriptSchedule.RunMode.WEEKLY:
            # Calculate next occurrence from weekly_days and weekly_times
            if not script_schedule.weekly_days or not script_schedule.weekly_times:
                return None

            candidates = []
            current_weekday = now.weekday()  # 0=Monday, 6=Sunday

            for day in script_schedule.weekly_days:
                for time_str in script_schedule.weekly_times:
                    hour, minute = map(int, time_str.split(":"))

                    # Calculate days until this weekday
                    days_ahead = day - current_weekday
                    if days_ahead < 0:
                        days_ahead += 7

                    candidate = now.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    ) + timedelta(days=days_ahead)

                    # If it's today but already passed, add a week
                    if candidate <= now:
                        candidate += timedelta(days=7)

                    candidates.append(candidate)

            return min(candidates) if candidates else None

        elif script_schedule.run_mode == ScriptSchedule.RunMode.MONTHLY:
            # Calculate next occurrence from monthly_days and monthly_times
            if not script_schedule.monthly_days or not script_schedule.monthly_times:
                return None

            candidates = []

            for day in script_schedule.monthly_days:
                for time_str in script_schedule.monthly_times:
                    hour, minute = map(int, time_str.split(":"))

                    # Try this month first
                    try:
                        candidate = now.replace(
                            day=day, hour=hour, minute=minute, second=0, microsecond=0
                        )
                        if candidate <= now:
                            # Already passed this month, try next month
                            if now.month == 12:
                                candidate = candidate.replace(year=now.year + 1, month=1)
                            else:
                                candidate = candidate.replace(month=now.month + 1)
                    except ValueError:
                        # Day doesn't exist in this month (e.g., 31st in Feb)
                        # Try next month
                        try:
                            if now.month == 12:
                                candidate = now.replace(
                                    year=now.year + 1,
                                    month=1,
                                    day=day,
                                    hour=hour,
                                    minute=minute,
                                    second=0,
                                    microsecond=0,
                                )
                            else:
                                candidate = now.replace(
                                    month=now.month + 1,
                                    day=day,
                                    hour=hour,
                                    minute=minute,
                                    second=0,
                                    microsecond=0,
                                )
                        except ValueError:
                            # Day doesn't exist in next month either, skip
                            continue

                    candidates.append(candidate)

            return min(candidates) if candidates else None

        return None

    @classmethod
    def pause_all_schedules(cls, user=None) -> int:
        """
        Pause all schedules globally by deleting all django-q2 Schedule objects.
        Returns count of deleted schedules.
        """
        from core.models import ScriptSchedule, GlobalSettings

        # Update global settings
        settings = GlobalSettings.get_settings()
        settings.schedules_paused = True
        settings.schedules_paused_at = timezone.now()
        settings.schedules_paused_by = user
        settings.save()

        # Delete all PyRunner-related schedules
        count = QSchedule.objects.filter(name__startswith="pyrunner-").delete()[0]

        # Clear all q_schedule_ids
        ScriptSchedule.objects.update(q_schedule_ids=[], next_run=None)

        logger.info(f"Globally paused all schedules - deleted {count} django-q2 schedules")
        return count

    @classmethod
    def resume_all_schedules(cls) -> int:
        """
        Resume all schedules by recreating django-q2 Schedule objects.
        Returns count of created schedules.
        """
        from core.models import ScriptSchedule, GlobalSettings

        settings = GlobalSettings.get_settings()
        settings.schedules_paused = False
        settings.schedules_paused_at = None
        settings.schedules_paused_by = None
        settings.save()

        count = 0
        for schedule in ScriptSchedule.objects.filter(
            is_active=True,
            run_mode__in=[
                ScriptSchedule.RunMode.INTERVAL,
                ScriptSchedule.RunMode.DAILY,
                ScriptSchedule.RunMode.WEEKLY,
                ScriptSchedule.RunMode.MONTHLY,
            ],
        ).select_related("script"):
            ids = cls.sync_schedule(schedule)
            count += len(ids)

        logger.info(f"Resumed all schedules - created {count} django-q2 schedules")
        return count

    @classmethod
    def ensure_heartbeat_schedule(cls) -> bool:
        """
        Ensure the worker heartbeat schedule exists.
        Creates the schedule if it doesn't exist.

        Returns:
            bool: True if schedule was created, False if it already exists
        """
        HEARTBEAT_SCHEDULE_NAME = "pyrunner-worker-heartbeat"
        HEARTBEAT_TASK_FUNC = "core.tasks.worker_heartbeat_task"

        if QSchedule.objects.filter(name=HEARTBEAT_SCHEDULE_NAME).exists():
            return False

        QSchedule.objects.create(
            name=HEARTBEAT_SCHEDULE_NAME,
            func=HEARTBEAT_TASK_FUNC,
            schedule_type=QSchedule.MINUTES,
            minutes=1,  # Run every minute
            repeats=-1,  # Run forever
            next_run=timezone.now(),
        )
        logger.info("Created worker heartbeat schedule")
        return True
