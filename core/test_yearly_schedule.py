import uuid
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django_q.models import Schedule as QSchedule

from core.forms import ScheduleForm
from core.models import Environment, Script, ScriptSchedule
from core.plugins.api import ScheduleAPI
from core.services.backup_service import BackupService
from core.services.schedule_service import ScheduleService
from core.tasks import execute_yearly_scheduled_run


def make_schedule(**kwargs):
    env = Environment.objects.create(
        name=f"e-{uuid.uuid4().hex[:8]}", path=f"p-{uuid.uuid4().hex[:8]}"
    )
    script = Script.objects.create(
        name=f"s-{uuid.uuid4().hex[:8]}", code="print('x')", environment=env
    )
    defaults = {
        "run_mode": ScriptSchedule.RunMode.YEARLY,
        "yearly_month": 6,
        "yearly_day": 15,
        "yearly_time": "09:00",
        "timezone": "UTC",
        "is_active": True,
    }
    defaults.update(kwargs)
    return ScriptSchedule.objects.create(script=script, **defaults)


class YearlyScheduleServiceTests(TestCase):
    def test_creates_annual_cron(self):
        schedule = make_schedule()
        ids = ScheduleService.sync_schedule(schedule)
        self.assertEqual(len(ids), 1)
        self.assertEqual(QSchedule.objects.get(id=ids[0]).cron, "0 9 15 6 *")

    def test_timezone_can_cross_into_previous_year(self):
        schedule = make_schedule(
            yearly_month=1, yearly_day=1, yearly_time="00:30", timezone="Asia/Tokyo"
        )
        ids = ScheduleService.sync_schedule(schedule)
        self.assertEqual(QSchedule.objects.get(id=ids[0]).cron, "30 15 31 12 *")

    def test_uses_target_year_dst_offset(self):
        schedule = make_schedule(
            yearly_month=11,
            yearly_day=2,
            yearly_time="03:00",
            timezone="America/New_York",
        )
        now = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        with mock.patch("core.services.schedule_service.timezone.now", return_value=now):
            ids = ScheduleService.sync_schedule(schedule)
        q_schedule = QSchedule.objects.get(id=ids[0])
        self.assertEqual(q_schedule.cron, "0 8 2 11 *")
        self.assertEqual(q_schedule.next_run, datetime(2025, 11, 2, 8, tzinfo=ZoneInfo("UTC")))

    def test_february_29_skips_non_leap_years(self):
        schedule = make_schedule(yearly_month=2, yearly_day=29, yearly_time="12:00")
        now = datetime(2025, 3, 1, 0, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("core.services.schedule_service.timezone.now", return_value=now):
            next_run = ScheduleService.calculate_next_run(schedule)
        self.assertEqual(next_run.year, 2028)
        self.assertEqual((next_run.month, next_run.day), (2, 29))

    def test_february_29_skips_non_leap_century(self):
        schedule = make_schedule(yearly_month=2, yearly_day=29, yearly_time="12:00")
        now = datetime(2096, 3, 1, 0, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("core.services.schedule_service.timezone.now", return_value=now):
            next_run = ScheduleService.calculate_next_run(schedule)
        self.assertEqual(next_run, datetime(2104, 2, 29, 12, 0, tzinfo=ZoneInfo("UTC")))

    def test_february_29_cron_uses_local_date_guard(self):
        schedule = make_schedule(
            yearly_month=2,
            yearly_day=29,
            yearly_time="00:30",
            timezone="Asia/Tokyo",
        )
        now = datetime(2025, 1, 1, tzinfo=ZoneInfo("UTC"))
        with mock.patch("core.services.schedule_service.timezone.now", return_value=now):
            ids = ScheduleService.sync_schedule(schedule)
        q_schedule = QSchedule.objects.get(id=ids[0])
        self.assertEqual(q_schedule.cron, "30 15 28 2 *")
        self.assertEqual(q_schedule.func, ScheduleService.YEARLY_TASK_FUNC)

        non_leap_fire = datetime(2025, 2, 28, 15, 30, tzinfo=ZoneInfo("UTC"))
        with (
            mock.patch("core.tasks.timezone.now", return_value=non_leap_fire),
            mock.patch("core.tasks.execute_scheduled_run") as execute,
        ):
            result = execute_yearly_scheduled_run(str(schedule.id))
        self.assertFalse(result["success"])
        execute.assert_not_called()

        leap_fire = datetime(2028, 2, 28, 15, 30, tzinfo=ZoneInfo("UTC"))
        with (
            mock.patch("core.tasks.timezone.now", return_value=leap_fire),
            mock.patch(
                "core.tasks.execute_scheduled_run", return_value={"success": True}
            ) as execute,
        ):
            result = execute_yearly_scheduled_run(str(schedule.id))
        self.assertTrue(result["success"])
        execute.assert_called_once_with(str(schedule.script_id))

    def test_next_run_before_annual_date_stays_in_current_year(self):
        schedule = make_schedule(yearly_month=12, yearly_day=31, yearly_time="23:00")
        now = datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("core.services.schedule_service.timezone.now", return_value=now):
            next_run = ScheduleService.calculate_next_run(schedule)
        self.assertEqual(next_run.year, 2026)


class YearlyScheduleFormTests(TestCase):
    def test_requires_valid_yearly_fields(self):
        form = ScheduleForm(
            data={
                "run_mode": "yearly",
                "yearly_month": "2",
                "yearly_day": "29",
                "yearly_time": "09:00",
                "timezone": "UTC",
                "is_active": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_rejects_day_that_month_does_not_have(self):
        form = ScheduleForm(
            data={
                "run_mode": "yearly",
                "yearly_month": "4",
                "yearly_day": "31",
                "yearly_time": "09:00",
                "timezone": "UTC",
                "is_active": "on",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("yearly_day", form.errors)


class YearlyScheduleAPITests(TestCase):
    def test_sync_accepts_yearly_date(self):
        schedule = make_schedule(
            run_mode=ScriptSchedule.RunMode.MANUAL,
            yearly_month=None,
            yearly_day=None,
            yearly_time="",
        )
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            result = ScheduleAPI().sync(
                schedule.script, mode="yearly", month=2, day=29, time_str="09:05"
            )
        self.assertEqual(
            (result.yearly_month, result.yearly_day, result.yearly_time), (2, 29, "09:05")
        )

    def test_sync_rejects_invalid_yearly_date(self):
        schedule = make_schedule()
        with self.assertRaises(ValueError):
            ScheduleAPI().sync(schedule.script, mode="yearly", month=4, day=31, time_str="09:00")


class YearlyScheduleBackupTests(TestCase):
    def test_yearly_fields_round_trip(self):
        schedule = make_schedule(yearly_month=2, yearly_day=29, yearly_time="07:30")
        backup = BackupService.create_backup(include_datastores=False)
        ScriptSchedule.objects.all().delete()
        result = BackupService.restore_backup(backup)
        self.assertTrue(result["success"], result.get("errors"))
        restored = ScriptSchedule.objects.get(id=schedule.id)
        self.assertEqual(
            (restored.yearly_month, restored.yearly_day, restored.yearly_time),
            (2, 29, "07:30"),
        )
