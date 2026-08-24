"""
Service for managing django-q2 schedules.
"""

import calendar
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Optional
from zoneinfo import ZoneInfo

from django.utils import timezone
from django_q.models import Schedule as QSchedule

logger = logging.getLogger(__name__)

# PyRunner's own infrastructure q-schedules. They share the "pyrunner-" name
# prefix with script schedules, so pause/resume must explicitly exclude them:
# deleting them silently kills worker-liveness reporting, update checks,
# scheduled backups, and auto-cleanup until the next container restart.
# Register any new infra schedule here.
HEARTBEAT_SCHEDULE_NAME = "pyrunner-worker-heartbeat"
UPDATE_SCHEDULE_NAME = "pyrunner-update-check"
BACKUP_SCHEDULE_NAME = "pyrunner-scheduled-backup"
CLEANUP_SCHEDULE_NAME = "pyrunner-auto-cleanup"
RESYNC_SCHEDULE_NAME = "pyrunner-schedule-resync"
INFRA_SCHEDULE_NAMES = (
    HEARTBEAT_SCHEDULE_NAME,
    UPDATE_SCHEDULE_NAME,
    BACKUP_SCHEDULE_NAME,
    CLEANUP_SCHEDULE_NAME,
    RESYNC_SCHEDULE_NAME,
)


class ScheduleService:
    """
    Manages the creation, update, and deletion of django-q2 Schedule objects
    based on ScriptSchedule configuration.
    """

    TASK_FUNC = "core.tasks.execute_scheduled_run"
    YEARLY_TASK_FUNC = "core.tasks.execute_yearly_scheduled_run"

    @staticmethod
    def _schedule_tz(script_schedule) -> ZoneInfo:
        """The schedule's IANA timezone, falling back to UTC on a bad value."""
        from core.tz import safe_zoneinfo

        return safe_zoneinfo(
            script_schedule.timezone,
            context=f"schedule for script {script_schedule.script_id}",
        )

    @staticmethod
    def _local_time_to_utc(tz: ZoneInfo, hour: int, minute: int) -> tuple[int, int, int]:
        """Convert a wall-clock time in ``tz`` to UTC cron fields.

        Returns (utc_hour, utc_minute, day_shift), where day_shift (-1/0/+1)
        says how the UTC calendar date relates to the local one — needed to
        move weekly/monthly day fields across the midnight boundary.

        Uses TODAY's UTC offset: a cron string cannot encode DST rules, so
        around a DST transition the fire time is off by the offset delta until
        the daily resync task (``resync_schedules_task``) rebuilds the cron.
        """
        local_date = datetime.now(tz).date()
        local_dt = datetime(
            local_date.year, local_date.month, local_date.day, hour, minute, tzinfo=tz
        )
        utc_dt = local_dt.astimezone(dt_timezone.utc)
        return utc_dt.hour, utc_dt.minute, (utc_dt.date() - local_date).days

    @staticmethod
    def _shift_month_days(days: list, day_shift: int) -> list[str]:
        """Shift day-of-month values across the UTC midnight boundary.

        Exact for days 2-28 either way. Two corners cron can't express exactly:
        - day 1 shifting backward -> 'l' (croniter's last-day-of-month), exact;
        - a trailing day shifting forward past 31 -> '1' (the 1st of the next
          month). Trailing days (29-31) that shift forward can also land on a
          day the month doesn't have and skip that month — the local day
          didn't exist there either for 31, but 29/30 may underfire in short
          months. Accepted: the alternative (no day shift) fires on the wrong
          local day for EVERY month.
        """
        shifted = set()
        for d in days:
            s = d + day_shift
            if s < 1:
                shifted.add("l")
            elif s > 31:
                shifted.add("1")
            else:
                shifted.add(str(s))
        nums = sorted((v for v in shifted if v != "l"), key=int)
        return nums + (["l"] if "l" in shifted else [])

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
        elif script_schedule.run_mode == ScriptSchedule.RunMode.YEARLY:
            q_schedule_ids = cls._create_yearly_schedule(script_schedule)

        # Update the ScriptSchedule with new IDs and next_run
        script_schedule.q_schedule_ids = q_schedule_ids
        script_schedule.next_run = cls.calculate_next_run(script_schedule)
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
        tz = cls._schedule_tz(script_schedule)

        for time_str in script_schedule.daily_times:
            hour, minute = map(int, time_str.split(":"))

            # Convert the schedule-local time to UTC (django-q evaluates cron
            # in TIME_ZONE=UTC). A date shift is irrelevant for daily crons.
            utc_hour, utc_minute, _ = cls._local_time_to_utc(tz, hour, minute)
            cron_expr = f"{utc_minute} {utc_hour} * * *"

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

        tz = cls._schedule_tz(script_schedule)

        for time_str in script_schedule.weekly_times:
            hour, minute = map(int, time_str.split(":"))

            # Convert the schedule-local time to UTC; when it crosses UTC
            # midnight the weekday moves with it (Mon 00:30 Tokyo = Sun UTC).
            utc_hour, utc_minute, day_shift = cls._local_time_to_utc(tz, hour, minute)

            # Model weekdays are 0=Monday..6=Sunday; standard cron (django-q2)
            # is 0=Sunday..6=Saturday. Shift for the date boundary, then map.
            cron_days = sorted(
                ((d + day_shift) % 7 + 1) % 7 for d in script_schedule.weekly_days
            )
            days_str = ",".join(str(d) for d in cron_days)

            # Cron format: minute hour * * day_of_week
            cron_expr = f"{utc_minute} {utc_hour} * * {days_str}"

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

        tz = cls._schedule_tz(script_schedule)

        for time_str in script_schedule.monthly_times:
            hour, minute = map(int, time_str.split(":"))

            # Convert the schedule-local time to UTC; day-of-month values move
            # with a date shift (see _shift_month_days for the corner cases).
            utc_hour, utc_minute, day_shift = cls._local_time_to_utc(tz, hour, minute)
            days_str = ",".join(
                cls._shift_month_days(script_schedule.monthly_days, day_shift)
            )

            # Cron format: minute hour day_of_month * *
            cron_expr = f"{utc_minute} {utc_hour} {days_str} * *"

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
    def _create_yearly_schedule(cls, script_schedule) -> list[int]:
        """Create one UTC cron for a yearly local calendar date."""
        local_dt = cls._next_yearly_occurrence(script_schedule)
        if local_dt is None:
            return []
        month, day = script_schedule.yearly_month, script_schedule.yearly_day
        hour, minute = map(int, script_schedule.yearly_time.split(":"))
        utc_dt = local_dt.astimezone(dt_timezone.utc)
        cron_expr = f"{utc_dt.minute} {utc_dt.hour} {utc_dt.day} {utc_dt.month} *"
        q_schedule = QSchedule.objects.create(
            name=f"pyrunner-{script_schedule.script.id}-yearly-{month:02d}{day:02d}{hour:02d}{minute:02d}",
            func=cls.YEARLY_TASK_FUNC,
            args=f"'{script_schedule.id}'",
            schedule_type=QSchedule.CRON,
            cron=cron_expr,
            repeats=-1,
            next_run=utc_dt,
        )
        return [q_schedule.id]

    @classmethod
    def _next_yearly_occurrence(cls, script_schedule, now=None) -> Optional[datetime]:
        """Return the next valid yearly occurrence in the schedule's timezone."""
        month, day, time_str = (
            script_schedule.yearly_month,
            script_schedule.yearly_day,
            script_schedule.yearly_time,
        )
        if not (month and day and time_str):
            return None

        tz = cls._schedule_tz(script_schedule)
        local_now = (now or timezone.now()).astimezone(tz)
        hour, minute = map(int, time_str.split(":"))
        for year_offset in range(8):
            year = local_now.year + year_offset
            if day > calendar.monthrange(year, month)[1]:
                continue
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
            if candidate > local_now:
                return candidate
        return None

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
    def calculate_next_run(cls, script_schedule) -> Optional[datetime]:
        """Calculate the next scheduled run time based on schedule configuration.

        Daily/weekly/monthly arithmetic happens in the schedule's timezone
        (mirroring how the crons fire), then converts to UTC for storage.
        """
        from core.models import ScriptSchedule

        if not script_schedule.is_active:
            return None

        if script_schedule.run_mode == ScriptSchedule.RunMode.INTERVAL:
            # Next run is interval_minutes from now (timezone-independent)
            return timezone.now() + timedelta(minutes=script_schedule.interval_minutes)

        # Wall-clock "now" in the schedule's timezone; candidates are built in
        # local time and converted back to UTC on return.
        now = timezone.now().astimezone(cls._schedule_tz(script_schedule))

        if script_schedule.run_mode == ScriptSchedule.RunMode.DAILY:
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

            return min(candidates).astimezone(dt_timezone.utc) if candidates else None

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

            return min(candidates).astimezone(dt_timezone.utc) if candidates else None

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

            return min(candidates).astimezone(dt_timezone.utc) if candidates else None

        elif script_schedule.run_mode == ScriptSchedule.RunMode.YEARLY:
            candidate = cls._next_yearly_occurrence(script_schedule, now=now)
            return candidate.astimezone(dt_timezone.utc) if candidate else None

        return None

    @classmethod
    def pause_all_schedules(cls, user=None) -> int:
        """
        Pause all script schedules globally by deleting their django-q2
        Schedule objects. The infrastructure schedules (heartbeat, update
        check, backup, cleanup) are untouched — pausing is about user scripts,
        not about turning off worker-liveness reporting or backups.
        Returns count of deleted schedules.
        """
        from core.models import ScriptSchedule, GlobalSettings

        # Update global settings
        settings = GlobalSettings.get_settings()
        settings.schedules_paused = True
        settings.schedules_paused_at = timezone.now()
        settings.schedules_paused_by = user
        settings.save()

        # Delete all script schedules (by prefix, so orphaned q-schedules
        # whose ids were lost from a failed sync are swept up too) — but
        # never the infra schedules that share the prefix.
        count = (
            QSchedule.objects.filter(name__startswith="pyrunner-")
            .exclude(name__in=INFRA_SCHEDULE_NAMES)
            .delete()[0]
        )

        # Clear all q_schedule_ids
        ScriptSchedule.objects.update(q_schedule_ids=[], next_run=None)

        logger.info(f"Globally paused all schedules - deleted {count} django-q2 schedules")
        return count

    @classmethod
    def resume_all_schedules(cls) -> int:
        """
        Resume all script schedules by recreating their django-q2 Schedule
        objects, and re-ensure the infrastructure schedules (heartbeat, update
        check, backup, cleanup) in case they were lost.
        Returns count of created script schedules.
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
                ScriptSchedule.RunMode.YEARLY,
            ],
        ).select_related("script"):
            ids = cls.sync_schedule(schedule)
            count += len(ids)

        # Belt-and-braces: re-ensure the infrastructure schedules. A pause on
        # an older PyRunner deleted them (shared "pyrunner-" prefix) and
        # nothing recreated them until the next container restart — resume is
        # the natural heal point. All of these are idempotent, and backup/
        # cleanup stay off unless their settings enable them.
        from core.services.backup_schedule_service import BackupScheduleService
        from core.services.retention_service import RetentionService

        cls.ensure_heartbeat_schedule()
        cls.ensure_update_check_schedule()
        cls.ensure_resync_schedule()
        BackupScheduleService.sync_schedule()
        if settings.auto_cleanup_enabled:
            RetentionService.enable_auto_cleanup()

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

    @classmethod
    def ensure_update_check_schedule(cls) -> bool:
        """
        Ensure the daily update-check schedule exists.
        Creates the schedule if it doesn't exist.

        Returns:
            bool: True if schedule was created, False if it already exists
        """
        UPDATE_TASK_FUNC = "core.tasks.check_for_updates_task"

        if QSchedule.objects.filter(name=UPDATE_SCHEDULE_NAME).exists():
            return False

        QSchedule.objects.create(
            name=UPDATE_SCHEDULE_NAME,
            func=UPDATE_TASK_FUNC,
            schedule_type=QSchedule.DAILY,
            repeats=-1,  # Run forever
            next_run=timezone.now(),  # Runs on first worker tick, then daily
        )
        logger.info("Created update check schedule")
        return True

    @classmethod
    def ensure_resync_schedule(cls) -> bool:
        """
        Ensure the daily schedule-resync schedule exists.

        Crons for timezone-aware schedules are converted to UTC with the
        offset valid at sync time; the resync task rebuilds them daily so a
        DST transition drifts fire times for at most a day.

        Returns:
            bool: True if schedule was created, False if it already exists
        """
        RESYNC_TASK_FUNC = "core.tasks.resync_schedules_task"

        if QSchedule.objects.filter(name=RESYNC_SCHEDULE_NAME).exists():
            return False

        QSchedule.objects.create(
            name=RESYNC_SCHEDULE_NAME,
            func=RESYNC_TASK_FUNC,
            schedule_type=QSchedule.DAILY,
            repeats=-1,  # Run forever
            next_run=timezone.now(),  # Runs on first worker tick, then daily
        )
        logger.info("Created schedule-resync schedule")
        return True
