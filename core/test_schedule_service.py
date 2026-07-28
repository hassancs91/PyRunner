"""
Schedule service tests.

1) Global pause/resume vs the infrastructure schedules — regression for review
   4a.2: pause_all_schedules deleted EVERY q-schedule with the "pyrunner-" name
   prefix, which silently included PyRunner's own infrastructure schedules
   (worker heartbeat, update check, scheduled backup, auto-cleanup) — and
   resume recreated only script schedules. One pause/resume cycle killed
   worker-liveness reporting (dashboard "workers down" + inbound webhooks
   fast-failing), scheduled backups, and auto-cleanup until the next container
   restart.

2) Schedule timezones — regression for review 4a.1: the timezone picker was
   stored and displayed but no scheduling code ever read it; every cron was
   built from the raw HH:MM and fired in UTC. Crons must now be built from the
   schedule's local time converted to UTC (weekday/month-day shifting with it
   across the UTC midnight boundary), and the daily resync task heals DST
   drift. Deterministic zones are used on purpose: Asia/Tokyo (UTC+9, no DST)
   and Etc/GMT+5 (fixed UTC-5).
"""

import uuid
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone
from django_q.models import Schedule as QSchedule

from core.models import Environment, GlobalSettings, Script, ScriptSchedule
from core.services.schedule_service import (
    BACKUP_SCHEDULE_NAME,
    CLEANUP_SCHEDULE_NAME,
    HEARTBEAT_SCHEDULE_NAME,
    INFRA_SCHEDULE_NAMES,
    UPDATE_SCHEDULE_NAME,
    ScheduleService,
)
from core.tasks import resync_schedules_task


def _make_script_schedule(interval_minutes=30, **kwargs):
    env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:10]}")
    script = Script.objects.create(
        name=f"s-{uuid.uuid4().hex[:6]}", code="print('x')", environment=env
    )
    defaults = {
        "run_mode": ScriptSchedule.RunMode.INTERVAL,
        "interval_minutes": interval_minutes,
        "is_active": True,
    }
    defaults.update(kwargs)
    return ScriptSchedule.objects.create(script=script, **defaults)


def _synced_cron(schedule) -> str:
    """Sync the schedule and return its (single) q-schedule's cron string."""
    ids = ScheduleService.sync_schedule(schedule)
    assert len(ids) == 1, f"expected one q-schedule, got {ids}"
    return QSchedule.objects.get(id=ids[0]).cron


def _seed_infra_schedules():
    """Create all infra q-schedules as a running instance would have.

    Heartbeat/update-check/resync via their real ensure functions;
    backup/cleanup directly (their services gate creation on S3/settings
    config, which is irrelevant here — the point is that the ROWS survive a
    pause).
    """
    ScheduleService.ensure_heartbeat_schedule()
    ScheduleService.ensure_update_check_schedule()
    ScheduleService.ensure_resync_schedule()
    QSchedule.objects.create(
        name=BACKUP_SCHEDULE_NAME,
        func="core.tasks.scheduled_backup_task",
        schedule_type=QSchedule.DAILY,
        repeats=-1,
        next_run=timezone.now(),
    )
    QSchedule.objects.create(
        name=CLEANUP_SCHEDULE_NAME,
        func="core.tasks.cleanup_old_runs_task",
        schedule_type=QSchedule.DAILY,
        repeats=-1,
        next_run=timezone.now(),
    )


class PauseSparesInfraTests(TestCase):
    def test_pause_deletes_script_schedules_but_spares_infra(self):
        sched = _make_script_schedule()
        ScheduleService.sync_schedule(sched)
        _seed_infra_schedules()

        count = ScheduleService.pause_all_schedules()

        self.assertEqual(count, 1)  # only the script schedule
        remaining = set(QSchedule.objects.values_list("name", flat=True))
        self.assertEqual(remaining, set(INFRA_SCHEDULE_NAMES))
        sched.refresh_from_db()
        self.assertEqual(sched.q_schedule_ids, [])
        self.assertIsNone(sched.next_run)
        self.assertTrue(GlobalSettings.get_settings().schedules_paused)

    def test_pause_sweeps_orphaned_script_schedules(self):
        # A q-schedule whose id was lost from ScriptSchedule.q_schedule_ids
        # (failed sync) must still be deleted — pause filters by prefix, not
        # by the stored ids.
        QSchedule.objects.create(
            name=f"pyrunner-{uuid.uuid4()}",
            func=ScheduleService.TASK_FUNC,
            schedule_type=QSchedule.MINUTES,
            minutes=5,
            repeats=-1,
            next_run=timezone.now(),
        )
        _seed_infra_schedules()

        count = ScheduleService.pause_all_schedules()

        self.assertEqual(count, 1)
        remaining = set(QSchedule.objects.values_list("name", flat=True))
        self.assertEqual(remaining, set(INFRA_SCHEDULE_NAMES))


class ResumeHealsInfraTests(TestCase):
    def test_resume_recreates_script_schedules_and_missing_infra(self):
        sched = _make_script_schedule()
        ScheduleService.sync_schedule(sched)
        ScheduleService.pause_all_schedules()
        # Simulate an install damaged by the old pause behavior: no infra
        # schedules exist at all.
        QSchedule.objects.all().delete()
        gs = GlobalSettings.get_settings()
        gs.auto_cleanup_enabled = True
        gs.save()

        count = ScheduleService.resume_all_schedules()

        self.assertEqual(count, 1)
        names = set(QSchedule.objects.values_list("name", flat=True))
        self.assertIn(f"pyrunner-{sched.script.id}", names)
        self.assertIn(HEARTBEAT_SCHEDULE_NAME, names)
        self.assertIn(UPDATE_SCHEDULE_NAME, names)
        self.assertIn(CLEANUP_SCHEDULE_NAME, names)  # auto_cleanup_enabled
        # Backup stays settings-gated: S3 backups are off in this instance.
        self.assertNotIn(BACKUP_SCHEDULE_NAME, names)
        sched.refresh_from_db()
        self.assertEqual(len(sched.q_schedule_ids), 1)
        self.assertIsNotNone(sched.next_run)
        self.assertFalse(GlobalSettings.get_settings().schedules_paused)

    def test_resume_respects_auto_cleanup_toggle(self):
        gs = GlobalSettings.get_settings()
        gs.auto_cleanup_enabled = False
        gs.save()

        ScheduleService.resume_all_schedules()

        names = set(QSchedule.objects.values_list("name", flat=True))
        self.assertNotIn(CLEANUP_SCHEDULE_NAME, names)

    def test_pause_resume_round_trip_is_lossless_for_scripts(self):
        sched = _make_script_schedule()
        before = set(ScheduleService.sync_schedule(sched))
        self.assertEqual(len(before), 1)

        ScheduleService.pause_all_schedules()
        self.assertFalse(
            QSchedule.objects.filter(name=f"pyrunner-{sched.script.id}").exists()
        )
        ScheduleService.resume_all_schedules()

        q = QSchedule.objects.get(name=f"pyrunner-{sched.script.id}")
        self.assertEqual(q.minutes, 30)
        sched.refresh_from_db()
        self.assertEqual(sched.q_schedule_ids, [q.id])


class TimezoneCronTests(TestCase):
    """Crons must fire at the schedule's LOCAL wall-clock time (review 4a.1)."""

    def test_daily_utc_schedule_is_unchanged(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="UTC",
        )
        self.assertEqual(_synced_cron(sched), "0 9 * * *")

    def test_daily_tokyo_converts_to_utc(self):
        # 09:00 Tokyo (UTC+9, no DST) = 00:00 UTC
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="Asia/Tokyo",
        )
        self.assertEqual(_synced_cron(sched), "0 0 * * *")

    def test_weekly_day_shifts_across_utc_midnight(self):
        # Monday 00:30 Tokyo = Sunday 15:30 UTC (model 0=Mon; cron 0=Sun)
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            interval_minutes=None,
            weekly_days=[0],
            weekly_times=["00:30"],
            timezone="Asia/Tokyo",
        )
        self.assertEqual(_synced_cron(sched), "30 15 * * 0")

    def test_weekly_utc_schedule_is_unchanged(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            interval_minutes=None,
            weekly_days=[0, 4],  # Mon, Fri
            weekly_times=["09:00"],
            timezone="UTC",
        )
        self.assertEqual(_synced_cron(sched), "0 9 * * 1,5")

    def test_monthly_interior_day_shifts(self):
        # Day 15 at 00:30 Tokyo = day 14 at 15:30 UTC
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.MONTHLY,
            interval_minutes=None,
            monthly_days=[15],
            monthly_times=["00:30"],
            timezone="Asia/Tokyo",
        )
        self.assertEqual(_synced_cron(sched), "30 15 14 * *")

    def test_monthly_day_one_becomes_last_of_month(self):
        # Day 1 at 00:30 Tokyo = LAST day of the previous month 15:30 UTC —
        # croniter's 'l' (verified supported by the pinned croniter).
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.MONTHLY,
            interval_minutes=None,
            monthly_days=[1],
            monthly_times=["00:30"],
            timezone="Asia/Tokyo",
        )
        self.assertEqual(_synced_cron(sched), "30 15 l * *")

    def test_monthly_day_31_wraps_forward_to_first(self):
        # Day 31 at 23:30 in fixed UTC-5 = day 1 of the next month 04:30 UTC
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.MONTHLY,
            interval_minutes=None,
            monthly_days=[31],
            monthly_times=["23:30"],
            timezone="Etc/GMT+5",  # POSIX sign: GMT+5 means UTC-5
        )
        self.assertEqual(_synced_cron(sched), "30 4 1 * *")

    def test_unknown_timezone_falls_back_to_utc(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="Not/AZone",
        )
        self.assertEqual(_synced_cron(sched), "0 9 * * *")

    def test_next_run_is_local_wall_clock(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="Asia/Tokyo",
        )
        ScheduleService.sync_schedule(sched)
        sched.refresh_from_db()
        local = sched.next_run.astimezone(ZoneInfo("Asia/Tokyo"))
        self.assertEqual((local.hour, local.minute), (9, 0))


class ResyncTaskTests(TestCase):
    """The daily resync task rebuilds tz-aware crons (DST heal, review 4a.1)."""

    def test_resync_rebuilds_drifted_cron(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="Asia/Tokyo",
        )
        ids = ScheduleService.sync_schedule(sched)
        # Simulate DST drift: the stored cron no longer matches today's offset.
        QSchedule.objects.filter(id__in=ids).update(cron="0 1 * * *")

        result = resync_schedules_task()

        self.assertTrue(result["success"])
        self.assertEqual(result["resynced"], 1)
        sched.refresh_from_db()
        self.assertEqual(
            QSchedule.objects.get(id=sched.q_schedule_ids[0]).cron, "0 0 * * *"
        )

    def test_resync_skips_utc_schedules(self):
        sched = _make_script_schedule(
            run_mode=ScriptSchedule.RunMode.DAILY,
            interval_minutes=None,
            daily_times=["09:00"],
            timezone="UTC",
        )
        ScheduleService.sync_schedule(sched)

        result = resync_schedules_task()

        self.assertTrue(result["success"])
        self.assertEqual(result["resynced"], 0)

    def test_resync_noop_while_paused(self):
        ScheduleService.pause_all_schedules()
        result = resync_schedules_task()
        self.assertEqual(result, {"success": True, "resynced": 0, "note": "schedules paused"})


class BackupTimezoneTests(TestCase):
    """Scheduled backup times are interpreted in the INSTANCE timezone —
    regression for review 4a.1's backup half (help text said 'in instance
    timezone' but the raw hour/minute ran in UTC)."""

    def _configure_backup(self, schedule_mode, tz="Asia/Tokyo"):
        from datetime import time

        from core.models import StorageConnection

        # Backups resolve the `is_default` connection; the old `s3_enabled` flag
        # now lives on the row as `enabled`.
        StorageConnection.objects.create(
            name="Test backups",
            bucket="test-bucket",
            is_default=True,
            enabled=True,
        )

        gs = GlobalSettings.get_settings()
        gs.timezone = tz
        gs.s3_backup_enabled = True
        gs.s3_backup_schedule = schedule_mode
        gs.s3_backup_time = time(9, 0)
        gs.s3_backup_day = 0  # Monday
        gs.save()
        return gs

    @mock.patch("core.services.s3_service.S3Service.is_configured", return_value=True)
    def test_daily_backup_fires_at_local_time(self, _cfg):
        from core.services.backup_schedule_service import BackupScheduleService

        self._configure_backup(GlobalSettings.S3BackupSchedule.DAILY)
        self.assertTrue(BackupScheduleService.sync_schedule())

        q = QSchedule.objects.get(name=BACKUP_SCHEDULE_NAME)
        # 09:00 Tokyo == 00:00 UTC
        self.assertEqual((q.next_run.hour, q.next_run.minute), (0, 0))

    @mock.patch("core.services.s3_service.S3Service.is_configured", return_value=True)
    def test_weekly_backup_cron_shifts_day(self, _cfg):
        from core.services.backup_schedule_service import BackupScheduleService

        gs = self._configure_backup(GlobalSettings.S3BackupSchedule.WEEKLY)
        from datetime import time

        gs.s3_backup_time = time(0, 30)  # Monday 00:30 Tokyo = Sunday 15:30 UTC
        gs.save()
        self.assertTrue(BackupScheduleService.sync_schedule())

        q = QSchedule.objects.get(name=BACKUP_SCHEDULE_NAME)
        self.assertEqual(q.cron, "30 15 * * 0")
