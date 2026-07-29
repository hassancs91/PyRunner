"""Two bugs in the script-edit schedule panel, found together.

BUG-004 — the chosen timezone was silently discarded. ``_form_sidebar.html``
rendered ``{{ schedule_form.timezone }}`` inside each of the daily/weekly/monthly
panels, emitting three controls sharing ``name="timezone"`` (and ``id_timezone``).
Panels are hidden with CSS, and ``display:none`` does not exclude a control from
submission — only ``disabled`` does — so the browser posted all three and Django's
QueryDict kept the LAST (monthly) copy. A user picking Asia/Beirut in the daily
panel stored UTC, with no error and the selector reverting.

BUG-005 — schedule history recorded the wrong thing, or nothing. The view
snapshotted ``previous_config`` *after* ``schedule_form.is_valid()``. The form is
bound to that same instance, and validation's ``construct_instance()`` has already
written every ``Meta.fields`` value onto it, so the four model-mapped fields
(run_mode / interval_minutes / timezone / is_active) were compared against
themselves. Consequences, both locked below:

  * an edit touching only those four wrote NO history row;
  * an edit that also changed times/days DID write a row, but its
    ``previous_config`` reported the new value as the old one — a silently
    falsified audit trail, which is worse than a missing one.

The JSON list fields (daily_times, weekly_*, monthly_*) are assigned in
``ScheduleForm.save()``, i.e. after the snapshot, which is why they diffed
correctly and why the existing ``test_weekly_only_edit_writes_history_via_view``
passed straight through this bug.
"""

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Environment,
    GlobalSettings,
    ScheduleHistory,
    Script,
    ScriptSchedule,
    User,
    Workspace,
    WorkspaceMembership,
)


class ScheduleEditTestCase(TestCase):
    """A logged-in owner editing a daily schedule in the default workspace."""

    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()

        self.workspace = Workspace.get_default()
        user = User.objects.create(
            email="sched-editor@example.com", is_staff=True, is_superuser=True
        )
        WorkspaceMembership.ensure(
            user, self.workspace, role=WorkspaceMembership.ROLE_OWNER
        )
        self.client.force_login(user)

        # A default environment must exist or SetupMiddleware treats setup as
        # incomplete and 302s the POST to /setup/ before the view runs.
        self.env = Environment.objects.create(
            name="sched-env", path="schedpath", is_active=True, is_default=True
        )
        self.script = Script.objects.create(
            name="sched_script",
            code="print('x')",
            environment=self.env,
            workspace=self.workspace,
        )
        self.schedule = ScriptSchedule.objects.create(
            script=self.script,
            workspace=self.workspace,
            run_mode=ScriptSchedule.RunMode.DAILY,
            daily_times=["23:30"],
            timezone="UTC",
            is_active=True,
        )

    @property
    def url(self):
        return reverse("cpanel:script_edit", args=[self.script.pk])

    def post(self, **overrides):
        """A full valid edit POST; overrides change one thing at a time."""
        data = {
            # ScriptForm — unchanged throughout
            "name": self.script.name,
            "code": self.script.code,
            "environment": str(self.env.id),
            "timeout_seconds": self.script.timeout_seconds,
            "isolation_mode": self.script.isolation_mode,
            "injection_mode": self.script.injection_mode,
            "notify_on": self.script.notify_on,
            # ScheduleForm
            "run_mode": ScriptSchedule.RunMode.DAILY,
            "daily_times_input": "23:30",
            "timezone": "UTC",
            "is_active": "on",
        }
        data.update(overrides)
        response = self.client.post(self.url, data)
        self.schedule.refresh_from_db()
        return response


class TimezoneControlTests(ScheduleEditTestCase):
    """BUG-004: exactly one timezone control may reach the browser."""

    def test_edit_form_renders_a_single_timezone_control(self):
        html = self.client.get(self.url).content.decode()
        # Three copies made the last one win; duplicate ids also broke the label.
        self.assertEqual(html.count('name="timezone"'), 1)
        self.assertEqual(html.count('id="id_timezone"'), 1)

    def test_chosen_timezone_is_stored(self):
        self.post(timezone="Asia/Beirut")
        self.assertEqual(self.schedule.timezone, "Asia/Beirut")

    def test_timezone_survives_a_weekly_edit(self):
        """The one control serves every clock-based mode, not just daily."""
        self.post(
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days_input=["0", "2"],
            weekly_times_input="09:00",
            timezone="Asia/Beirut",
        )
        self.assertEqual(self.schedule.run_mode, ScriptSchedule.RunMode.WEEKLY)
        self.assertEqual(self.schedule.timezone, "Asia/Beirut")


class ScheduleHistoryTimingTests(ScheduleEditTestCase):
    """BUG-005: the snapshot must predate the form's write to the instance."""

    def _sole_entry(self):
        entries = ScheduleHistory.objects.filter(
            schedule=self.schedule, change_type=ScheduleHistory.ChangeType.UPDATED
        )
        self.assertEqual(entries.count(), 1)
        return entries.first()

    def test_timezone_only_edit_is_recorded(self):
        self.post(timezone="Asia/Beirut")
        entry = self._sole_entry()
        self.assertEqual(entry.previous_config["timezone"], "UTC")
        self.assertEqual(entry.new_config["timezone"], "Asia/Beirut")

    def test_run_mode_switch_is_recorded(self):
        self.post(
            run_mode=ScriptSchedule.RunMode.INTERVAL,
            interval_minutes=60,
        )
        entry = self._sole_entry()
        self.assertEqual(entry.previous_config["run_mode"], ScriptSchedule.RunMode.DAILY)
        self.assertEqual(entry.new_config["run_mode"], ScriptSchedule.RunMode.INTERVAL)
        self.assertEqual(entry.new_config["interval_minutes"], 60)

    def test_deactivating_via_the_edit_form_is_recorded(self):
        data_without_checkbox = {"is_active": ""}
        self.post(**data_without_checkbox)
        self.assertFalse(self.schedule.is_active)
        entry = self._sole_entry()
        self.assertTrue(entry.previous_config["is_active"])
        self.assertFalse(entry.new_config["is_active"])

    def test_previous_config_is_the_true_previous_on_a_mixed_edit(self):
        """The falsified-audit half: times AND timezone change together."""
        self.post(daily_times_input="07:15", timezone="Asia/Beirut")
        entry = self._sole_entry()
        self.assertEqual(entry.previous_config["timezone"], "UTC")
        self.assertEqual(entry.previous_config["daily_times"], ["23:30"])
        self.assertEqual(entry.new_config["timezone"], "Asia/Beirut")
        self.assertEqual(entry.new_config["daily_times"], ["07:15"])

    def test_unchanged_resubmit_still_records_nothing(self):
        """The earlier snapshot must not turn every save into an audit row."""
        self.post()
        self.assertEqual(ScheduleHistory.objects.filter(schedule=self.schedule).count(), 0)
