"""
Tests for raw cron scheduling (RunMode.CRON).

Covers the validation/preview helpers on ScheduleService, django-q2 schedule
creation, next-run computation, ScheduleForm validation, and the plugin
ScheduleAPI.sync cron path.
"""

from unittest import mock

from django.test import TestCase
from django.utils import timezone
from django_q.models import Schedule as QSchedule

from core.forms import ScheduleForm
from core.models import Environment, Script, ScriptSchedule, Workspace
from core.plugins.api import ScheduleAPI
from core.services.schedule_service import ScheduleService


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
        nxt = ScheduleService._calculate_next_run(sched)
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
