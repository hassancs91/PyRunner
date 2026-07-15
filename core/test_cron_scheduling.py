"""
Tests for raw cron scheduling (RunMode.CRON).

Covers the validation/preview helpers on ScheduleService, django-q2 schedule
creation, next-run computation, ScheduleForm validation, the plugin
ScheduleAPI.sync cron path, and the timezone -> UTC conversion (expressions are
authored in the schedule's timezone and stored as UTC, like the guided modes).
"""

from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule as QSchedule

from core.forms import ScheduleForm
from core.models import (
    Environment,
    GlobalSettings,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import ScheduleAPI
from core.services.schedule_service import ScheduleService
from core.tasks import resync_schedules_task


class CronValidationTests(TestCase):
    def test_valid_expression(self):
        ok, err = ScheduleService.validate_cron_expression("0 9 * * 1-5")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_empty_expression(self):
        ok, err = ScheduleService.validate_cron_expression("")
        self.assertFalse(ok)
        self.assertIn("required", err)

    def test_wrong_field_count(self):
        ok, err = ScheduleService.validate_cron_expression("0 9 * *")
        self.assertFalse(ok)
        self.assertIn("5 fields", err)

    def test_named_shortcut_rejected(self):
        ok, err = ScheduleService.validate_cron_expression("@daily")
        self.assertFalse(ok)
        self.assertIn("shortcut", err.lower())

    def test_garbage_rejected(self):
        ok, err = ScheduleService.validate_cron_expression("99 99 * * *")
        self.assertFalse(ok)

    def test_preview_returns_three_future_runs(self):
        runs = ScheduleService.preview_cron_runs("*/5 * * * *", count=3)
        self.assertEqual(len(runs), 3)
        now = timezone.localtime(timezone.now())
        self.assertTrue(all(r > now for r in runs))
        # Strictly increasing
        self.assertTrue(runs[0] < runs[1] < runs[2])

    def test_preview_invalid_returns_empty(self):
        self.assertEqual(ScheduleService.preview_cron_runs("nope"), [])


class CronScheduleServiceTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="cronenv", path="cronenv")
        self.script = Script.objects.create(
            name="cron-script", code="print(1)", environment=self.env, workspace=self.ws
        )

    def _make(self, expr, is_active=True):
        return ScriptSchedule.objects.create(
            script=self.script,
            workspace=self.ws,
            run_mode=ScriptSchedule.RunMode.CRON,
            cron_expression=expr,
            is_active=is_active,
        )

    def test_sync_creates_cron_qschedule(self):
        sched = self._make("0 9 * * 1-5")
        ids = ScheduleService.sync_schedule(sched)
        self.assertEqual(len(ids), 1)
        q = QSchedule.objects.get(id=ids[0])
        self.assertEqual(q.schedule_type, QSchedule.CRON)
        self.assertEqual(q.cron, "0 9 * * 1-5")
        self.assertEqual(q.func, ScheduleService.TASK_FUNC)

        sched.refresh_from_db()
        self.assertEqual(sched.q_schedule_ids, ids)
        self.assertIsNotNone(sched.next_run)

    def test_sync_inactive_creates_nothing(self):
        sched = self._make("0 9 * * *", is_active=False)
        ids = ScheduleService.sync_schedule(sched)
        self.assertEqual(ids, [])
        self.assertFalse(QSchedule.objects.filter(name__contains=str(self.script.id)).exists())

    def test_empty_expression_creates_nothing(self):
        sched = self._make("")
        ids = ScheduleService.sync_schedule(sched)
        self.assertEqual(ids, [])

    def test_next_run_matches_croniter(self):
        from croniter import croniter
        from datetime import datetime

        sched = self._make("30 4 * * *")
        nxt = ScheduleService.calculate_next_run(sched)
        self.assertIsNotNone(nxt)
        expected = croniter("30 4 * * *", timezone.now()).get_next(datetime)
        # Allow tiny drift between the two now() reads.
        self.assertLess(abs((nxt - expected).total_seconds()), 5)

    def test_schedule_display(self):
        sched = self._make("0 0 * * 0")
        self.assertEqual(sched.schedule_display, "Cron: 0 0 * * 0")


class CronFormTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="formenv", path="formenv")
        self.script = Script.objects.create(
            name="form-script", code="print(1)", environment=self.env, workspace=self.ws
        )
        self.schedule = ScriptSchedule.objects.create(script=self.script, workspace=self.ws)

    def _post(self, expr):
        return {
            "run_mode": "cron",
            "cron_expression": expr,
            "timezone": "UTC",
            "is_active": "on",
        }

    def test_valid_cron_form_saves(self):
        form = ScheduleForm(self._post("15 2 * * *"), instance=self.schedule)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.run_mode, "cron")
        self.assertEqual(saved.cron_expression, "15 2 * * *")

    def test_invalid_cron_form_errors(self):
        form = ScheduleForm(self._post("not a cron"), instance=self.schedule)
        self.assertFalse(form.is_valid())
        self.assertIn("cron_expression", form.errors)

    def test_missing_cron_form_errors(self):
        form = ScheduleForm(self._post(""), instance=self.schedule)
        self.assertFalse(form.is_valid())
        self.assertIn("cron_expression", form.errors)


class CronPluginAPITests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="pluginenv", path="pluginenv")

    def test_sync_cron_mode(self):
        from core.plugins.api import ScriptAPI

        script = ScriptAPI("myplugin").upsert(key="c", code="x", environment=self.env)
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            sched = ScheduleAPI("myplugin").sync(
                script, mode=ScriptSchedule.RunMode.CRON, cron="0 * * * *"
            )
        self.assertEqual(sched.cron_expression, "0 * * * *")
        self.assertTrue(sched.is_active)

    def test_sync_cron_invalid_raises(self):
        from core.plugins.api import ScriptAPI

        script = ScriptAPI("myplugin").upsert(key="c2", code="x", environment=self.env)
        with self.assertRaises(ValueError):
            ScheduleAPI("myplugin").sync(
                script, mode=ScriptSchedule.RunMode.CRON, cron="bad"
            )


class CronTimezoneTests(TestCase):
    """Cron expressions are written in the schedule's timezone and stored as UTC.

    Zones without DST (Tokyo +09:00, Sao Paulo -03:00, Kolkata +05:30) keep the
    expected strings date-independent.
    """

    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="tzenv", path="tzenv")
        self.script = Script.objects.create(
            name="tz-script", code="print(1)", environment=self.env, workspace=self.ws
        )

    @staticmethod
    def _convert(expr, tz_name):
        return ScheduleService.cron_to_utc_expressions(expr, ZoneInfo(tz_name))

    def _make(self, expr, tz):
        return ScriptSchedule.objects.create(
            script=self.script,
            workspace=self.ws,
            run_mode=ScriptSchedule.RunMode.CRON,
            cron_expression=expr,
            timezone=tz,
        )

    # -- pure conversion -------------------------------------------------

    def test_utc_passes_through_untouched(self):
        self.assertEqual(self._convert("0 9 * * mon-fri", "UTC"), ["0 9 * * mon-fri"])

    def test_tokyo_weekday_morning_shifts_hour_only(self):
        # 09:00 Tokyo = 00:00 UTC, same calendar day: day fields stay verbatim.
        self.assertEqual(self._convert("0 9 * * 1-5", "Asia/Tokyo"), ["0 0 * * 1-5"])

    def test_crossing_utc_midnight_shifts_weekdays_back(self):
        # 00:30 Mon-Fri Tokyo = 15:30 Sun-Thu UTC.
        self.assertEqual(self._convert("30 0 * * 1-5", "Asia/Tokyo"), ["30 15 * * 0,1,2,3,4"])

    def test_crossing_utc_midnight_forward_shifts_weekday_ahead(self):
        # 22:00 Monday in Sao Paulo = 01:00 Tuesday UTC.
        self.assertEqual(self._convert("0 22 * * 1", "America/Sao_Paulo"), ["0 1 * * 2"])

    def test_month_days_shift_with_the_date(self):
        # 00:30 on the 1st and 15th in Tokyo = 15:30 UTC on the last day and the 14th.
        self.assertEqual(self._convert("30 0 1,15 * *", "Asia/Tokyo"), ["30 15 14,l * *"])

    def test_hours_on_different_utc_days_become_two_schedules(self):
        # 05:00 Mon Tokyo = 20:00 Sun UTC; 12:00 Mon Tokyo = 03:00 Mon UTC.
        self.assertEqual(
            sorted(self._convert("0 5,12 * * 1", "Asia/Tokyo")), ["0 20 * * 0", "0 3 * * 1"]
        )

    def test_every_quarter_hour_is_offset_independent(self):
        self.assertEqual(self._convert("*/15 * * * *", "Asia/Tokyo"), ["*/15 * * * *"])

    def test_hourly_on_weekdays_splits_at_utc_midnight(self):
        # Local 00-08 on Mon-Fri = 15-23 UTC on Sun-Thu; local 09-23 = 00-14 UTC same day.
        self.assertEqual(
            sorted(self._convert("0 * * * 1-5", "Asia/Tokyo")),
            sorted(
                [
                    "0 15,16,17,18,19,20,21,22,23 * * 0,1,2,3,4",
                    "0 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14 * * 1-5",
                ]
            ),
        )

    def test_fractional_offset_shifts_minutes(self):
        # Kolkata is +05:30: on the hour local = half past every hour UTC.
        self.assertEqual(self._convert("0 * * * *", "Asia/Kolkata"), ["30 * * * *"])

    def test_fractional_offset_rejects_wildcard_minute(self):
        with self.assertRaises(ValueError):
            self._convert("* 9 * * *", "Asia/Kolkata")
        ok, err = ScheduleService.validate_cron_expression("* 9 * * *", "Asia/Kolkata")
        self.assertFalse(ok)
        self.assertIn("fractional", err)

    def test_nth_weekday_rejected_only_when_crossing_midnight(self):
        self.assertEqual(self._convert("0 9 * * 1#2", "Asia/Tokyo"), ["0 0 * * 1#2"])
        with self.assertRaises(ValueError):
            self._convert("30 0 * * 1#2", "Asia/Tokyo")

    # -- through the service ----------------------------------------------

    def test_sync_stores_utc_expression_and_local_next_run(self):
        sched = self._make("0 9 * * *", "Asia/Tokyo")
        ids = ScheduleService.sync_schedule(sched)
        self.assertEqual([QSchedule.objects.get(id=i).cron for i in ids], ["0 0 * * *"])
        sched.refresh_from_db()
        self.assertEqual(sched.next_run.astimezone(dt_timezone.utc).hour, 0)

    def test_sync_creates_one_row_per_utc_expression(self):
        sched = self._make("0 5,12 * * 1", "Asia/Tokyo")
        ids = ScheduleService.sync_schedule(sched)
        self.assertEqual(len(ids), 2)
        self.assertEqual(
            sorted(QSchedule.objects.get(id=i).cron for i in ids), ["0 20 * * 0", "0 3 * * 1"]
        )

    def test_unconvertible_expression_creates_nothing(self):
        sched = self._make("* 9 * * *", "Asia/Kolkata")
        self.assertEqual(ScheduleService.sync_schedule(sched), [])

    def test_resync_rebuilds_cron_mode_like_the_guided_modes(self):
        sched = self._make("0 9 * * *", "Asia/Tokyo")
        ids = ScheduleService.sync_schedule(sched)
        QSchedule.objects.filter(id__in=ids).update(cron="0 1 * * *")  # simulate DST drift

        result = resync_schedules_task()

        self.assertEqual(result["resynced"], 1)
        sched.refresh_from_db()
        self.assertEqual(QSchedule.objects.get(id=sched.q_schedule_ids[0]).cron, "0 0 * * *")

    def test_preview_runs_in_selected_timezone(self):
        runs = ScheduleService.preview_cron_runs("0 9 * * *", count=2, timezone_name="Asia/Tokyo")
        self.assertEqual([r.hour for r in runs], [9, 9])
        self.assertEqual([r.utcoffset() for r in runs], [timedelta(hours=9)] * 2)


class CronFormTimezoneTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="formtzenv", path="formtzenv")
        self.script = Script.objects.create(
            name="form-tz-script", code="print(1)", environment=self.env, workspace=self.ws
        )
        self.schedule = ScriptSchedule.objects.create(script=self.script, workspace=self.ws)

    def _post(self, expr, tz):
        return {"run_mode": "cron", "cron_expression": expr, "timezone": tz, "is_active": "on"}

    def test_form_saves_timezone_with_cron(self):
        form = ScheduleForm(self._post("0 9 * * 1-5", "Asia/Tokyo"), instance=self.schedule)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual((saved.cron_expression, saved.timezone), ("0 9 * * 1-5", "Asia/Tokyo"))

    def test_form_rejects_expression_unconvertible_in_timezone(self):
        form = ScheduleForm(self._post("* 9 * * *", "Asia/Kolkata"), instance=self.schedule)
        self.assertFalse(form.is_valid())
        self.assertIn("fractional", form.errors["cron_expression"][0])


class CronPreviewViewTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        self.workspace = Workspace.get_default()
        user = get_user_model().objects.create(
            email="cron-preview@example.com", is_staff=True, is_superuser=True
        )
        WorkspaceMembership.ensure(user, self.workspace, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)
        # SetupMiddleware treats a missing default environment as "setup not
        # done" and 302s to /setup/ before the view runs.
        Environment.objects.create(name="Default Environment", path="default", is_default=True)

    def test_preview_reports_runs_in_the_requested_timezone(self):
        response = self.client.get(
            reverse("cpanel:cron_preview"), {"expression": "0 9 * * *", "timezone": "Asia/Tokyo"}
        )
        self.assertEqual(response.status_code, 200, (response.get("Location"), response.content[:400]))
        data = response.json()
        self.assertTrue(data["valid"], data)
        self.assertEqual(data["timezone"], "Asia/Tokyo")
        self.assertEqual(len(data["runs"]), 3)
        self.assertTrue(all(run.endswith("09:00") for run in data["runs"]), data["runs"])

    def test_preview_reports_timezone_specific_errors(self):
        response = self.client.get(
            reverse("cpanel:cron_preview"), {"expression": "* 9 * * *", "timezone": "Asia/Kolkata"}
        )
        data = response.json()
        self.assertFalse(data["valid"])
        self.assertIn("fractional", data["error"])

    def test_unknown_timezone_falls_back_to_utc(self):
        response = self.client.get(
            reverse("cpanel:cron_preview"), {"expression": "0 9 * * *", "timezone": "Mars/Olympus"}
        )
        self.assertEqual(response.json()["timezone"], "UTC")
