"""
Tests for free-form interval schedules (roadmap 1.7).

``interval_minutes`` used to be a fixed choice list (5/10/15/30/60/120/360/720);
it now accepts any whole number from 1 to 32767 (django-q2's small-int ceiling
on ``Schedule.minutes``). Covers the model bounds, the human-readable label,
the ScheduleForm number input + quick-picks, and the plugin ScheduleAPI path,
which previously accepted any integer without validating it.
"""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.forms import ScheduleForm
from core.models import (
    Environment,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import ScheduleAPI, ScriptAPI

User = get_user_model()


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class IntervalModelTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="ienv", path=f"ienv{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(
            name="interval-script", code="print(1)", environment=self.env, workspace=self.ws
        )

    def _schedule(self, minutes):
        return ScriptSchedule(
            script=self.script,
            workspace=self.ws,
            run_mode=ScriptSchedule.RunMode.INTERVAL,
            interval_minutes=minutes,
        )

    def test_any_value_in_range_is_valid(self):
        for minutes in (1, 7, 90, 1440, 32767):
            with self.subTest(minutes=minutes):
                self._schedule(minutes).full_clean()

    def test_out_of_range_rejected(self):
        for minutes in (0, 32768):
            with self.subTest(minutes=minutes):
                with self.assertRaises(ValidationError) as ctx:
                    self._schedule(minutes).full_clean()
                self.assertIn("interval_minutes", ctx.exception.message_dict)

    def test_describe_interval_labels(self):
        cases = {
            1: "Every minute",
            7: "Every 7 minutes",
            60: "Every hour",
            90: "Every 90 minutes",
            120: "Every 2 hours",
            1440: "Every day",
            2880: "Every 2 days",
            None: "Every ? minutes",
        }
        for minutes, label in cases.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(ScriptSchedule.describe_interval(minutes), label)

    def test_schedule_display_uses_label(self):
        self.assertEqual(self._schedule(7).schedule_display, "Every 7 minutes")

    def test_presets_are_within_bounds(self):
        for minutes, _label in ScriptSchedule.INTERVAL_PRESETS:
            self.assertTrue(
                ScriptSchedule.INTERVAL_MIN_MINUTES <= minutes <= ScriptSchedule.INTERVAL_MAX_MINUTES
            )


class IntervalFormTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="fenv", path=f"fenv{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(
            name="form-script", code="print(1)", environment=self.env, workspace=self.ws
        )
        self.schedule = ScriptSchedule.objects.create(script=self.script, workspace=self.ws)

    def _post(self, minutes):
        data = {"run_mode": "interval", "timezone": "UTC", "is_active": "on"}
        if minutes is not None:
            data["interval_minutes"] = minutes
        return data

    def test_non_preset_value_saves(self):
        form = ScheduleForm(self._post("7"), instance=self.schedule)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.interval_minutes, 7)
        self.assertEqual(saved.schedule_display, "Every 7 minutes")

    def test_ceiling_saves(self):
        form = ScheduleForm(self._post("32767"), instance=self.schedule)
        self.assertTrue(form.is_valid(), form.errors)

    def test_zero_rejected_with_one_error(self):
        form = ScheduleForm(self._post("0"), instance=self.schedule)
        self.assertFalse(form.is_valid())
        # The range validator reports it; clean() must not pile a second
        # "required" message on top.
        self.assertEqual(len(form.errors["interval_minutes"]), 1)

    def test_above_ceiling_rejected(self):
        form = ScheduleForm(self._post("40000"), instance=self.schedule)
        self.assertFalse(form.is_valid())
        self.assertIn("interval_minutes", form.errors)

    def test_missing_rejected(self):
        form = ScheduleForm(self._post(None), instance=self.schedule)
        self.assertFalse(form.is_valid())
        self.assertIn("required", form.errors["interval_minutes"][0].lower())

    def test_widget_is_a_bounded_number_input(self):
        html = str(ScheduleForm(instance=self.schedule)["interval_minutes"])
        self.assertIn('type="number"', html)
        self.assertIn('min="1"', html)
        self.assertIn('max="32767"', html)


class IntervalFormPageTests(TestCase):
    """The edit page renders the number input and the quick-pick buttons."""

    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="interval@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.env = Environment.objects.create(name="penv", path=f"penv{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(
            name="page-script", code="pass", environment=self.env, workspace=self.ws
        )
        ScriptSchedule.objects.create(
            script=self.script, workspace=self.ws,
            run_mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=7,
        )
        self.client.force_login(self.user)

    def test_edit_page_shows_quick_picks_and_current_value(self):
        resp = self.client.get(reverse("cpanel:script_edit", args=[self.script.pk]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('data-interval-preset="5"', body)
        self.assertIn('data-interval-preset="1440"', body)
        self.assertIn('name="interval_minutes"', body)
        self.assertIn('value="7"', body)


class IntervalPluginAPITests(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(name="aenv", path=f"aenv{uuid.uuid4().hex[:6]}")
        self.script = ScriptAPI("myplugin").upsert(key="i", code="x", environment=self.env)

    def _sync(self, minutes):
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            return ScheduleAPI("myplugin").sync(
                self.script, mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=minutes
            )

    def test_non_preset_value_accepted(self):
        self.assertEqual(self._sync(7).interval_minutes, 7)

    def test_string_digits_coerced(self):
        self.assertEqual(self._sync("45").interval_minutes, 45)

    def test_out_of_range_raises(self):
        for minutes in (0, 32768, -5):
            with self.subTest(minutes=minutes):
                with self.assertRaises(ValueError):
                    self._sync(minutes)

    def test_non_integer_raises(self):
        with self.assertRaises(ValueError):
            self._sync("every five")
