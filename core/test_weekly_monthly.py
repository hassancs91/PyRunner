"""
The weekly/monthly schedule blind-spot sweep — regression for review 4a.4 +
4b.2 + the Part 5 history-snapshot cross-cutting note.

WEEKLY and MONTHLY run modes were added after several consumers existed and
never swept through them:

  * 4a.4 — the dashboard "upcoming scheduled runs" widget filtered
    ``run_mode__in=[INTERVAL, DAILY]`` and silently hid weekly/monthly
    schedules. The redundant filter is gone; a non-null future ``next_run``
    (which ``sync_schedule`` clears to NULL for manual) is the real signal.
  * 4b.2 — the backup format never exported the weekly/monthly schedule fields
    (a weekly/monthly schedule restored as an empty shell) nor a script's
    tags / archive state / isolation_mode.
  * history — ``ScheduleHistory`` snapshots omitted weekly/monthly fields, so a
    weekly- or monthly-only edit compared equal and wrote no audit entry.

Each is locked below.
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    ScheduleHistory,
    Script,
    ScriptSchedule,
    Tag,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.dashboard_service import DashboardService
from core.services.schedule_service import ScheduleService


def _make_script(name=None, **kwargs):
    env = Environment.objects.create(name=f"e-{uuid.uuid4().hex[:8]}", path=f"p{uuid.uuid4().hex[:8]}")
    return Script.objects.create(
        name=name or f"s-{uuid.uuid4().hex[:6]}",
        code="print('x')",
        environment=env,
        **kwargs,
    )


class DashboardUpcomingRunsTests(TestCase):
    """Review 4a.4: every recurring mode with a future next_run must surface."""

    def _upcoming_ids(self):
        return set(
            str(s.id) for s in DashboardService.get_upcoming_scheduled_runs(limit=20)
        )

    def test_weekly_and_monthly_schedules_appear(self):
        future = timezone.now() + timedelta(hours=1)
        weekly = ScriptSchedule.objects.create(
            script=_make_script(),
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days=[0],
            weekly_times=["09:00"],
            is_active=True,
            next_run=future,
        )
        monthly = ScriptSchedule.objects.create(
            script=_make_script(),
            run_mode=ScriptSchedule.RunMode.MONTHLY,
            monthly_days=[1],
            monthly_times=["09:00"],
            is_active=True,
            next_run=future,
        )
        ids = self._upcoming_ids()
        self.assertIn(str(weekly.id), ids)
        self.assertIn(str(monthly.id), ids)

    def test_manual_schedule_still_excluded(self):
        # sync clears next_run to NULL for manual mode — the invariant the
        # removed run_mode filter relied on. Guard it so the deletion is safe.
        manual = ScriptSchedule.objects.create(
            script=_make_script(),
            run_mode=ScriptSchedule.RunMode.MANUAL,
            is_active=True,
        )
        ScheduleService.sync_schedule(manual)
        manual.refresh_from_db()
        self.assertIsNone(manual.next_run)
        self.assertNotIn(str(manual.id), self._upcoming_ids())


class BackupFidelityTests(TestCase):
    """Review 4b.2: fields added after the format was designed must round-trip."""

    def _round_trip(self, current_user=None):
        backup = BackupService.create_backup(include_datastores=False)
        result = BackupService.restore_backup(backup, current_user=current_user)
        self.assertTrue(result["success"], result.get("errors"))
        return backup

    def test_weekly_monthly_schedule_fields_round_trip(self):
        weekly = ScriptSchedule.objects.create(
            script=_make_script("weekly_one"),
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days=[0, 2, 4],
            weekly_times=["09:00", "18:00"],
            timezone="Asia/Tokyo",
        )
        monthly = ScriptSchedule.objects.create(
            script=_make_script("monthly_one"),
            run_mode=ScriptSchedule.RunMode.MONTHLY,
            monthly_days=[1, 15],
            monthly_times=["06:30"],
            timezone="UTC",
        )

        self._round_trip()

        weekly_r = ScriptSchedule.objects.get(id=weekly.id)
        self.assertEqual(weekly_r.run_mode, ScriptSchedule.RunMode.WEEKLY)
        self.assertEqual(weekly_r.weekly_days, [0, 2, 4])
        self.assertEqual(weekly_r.weekly_times, ["09:00", "18:00"])
        self.assertEqual(weekly_r.timezone, "Asia/Tokyo")

        monthly_r = ScriptSchedule.objects.get(id=monthly.id)
        self.assertEqual(monthly_r.run_mode, ScriptSchedule.RunMode.MONTHLY)
        self.assertEqual(monthly_r.monthly_days, [1, 15])
        self.assertEqual(monthly_r.monthly_times, ["06:30"])

    def test_script_tags_archive_isolation_round_trip(self):
        user = User.objects.create(email="owner@example.com")
        tag = Tag.objects.create(name="production", color=Tag.Color.BLUE)
        archived_at = timezone.now()
        script = _make_script(
            "archived_tagged",
            isolation_mode=Script.IsolationMode.SANDBOXED,
            archived_at=archived_at,
            archived_by=user,
        )
        script.tags.add(tag)

        # Back up WHILE the tag exists, then drop the Tag rows so the import must
        # recreate them (proves get_or_create-by-name reconstructs, not re-links).
        backup = BackupService.create_backup(include_datastores=False)
        Tag.objects.all().delete()
        result = BackupService.restore_backup(backup, current_user=user)
        self.assertTrue(result["success"], result.get("errors"))

        restored = Script.objects.get(id=script.id)
        self.assertEqual(restored.isolation_mode, Script.IsolationMode.SANDBOXED)
        self.assertIsNotNone(restored.archived_at)
        self.assertTrue(restored.is_archived)
        self.assertEqual(restored.archived_by, user)
        restored_tags = {(t.name, t.color) for t in restored.tags.all()}
        self.assertEqual(restored_tags, {("production", Tag.Color.BLUE)})

    def test_backup_version_is_current(self):
        # 1.7.0 = Script Libraries joined the format: a ``libraries`` array
        # (head revision inlined), attachment ids on scripts, and
        # ``library_versions`` on runs (1.6.0 added External Secret Providers,
        # 1.5.0 the Databases array).
        #
        # This literal is the ONE deliberate tripwire for a format change:
        # changing the backup format is a compatibility event, so it should cost
        # a conscious edit here. Other suites assert their own feature's floor
        # instead, so an additive bump doesn't break them too.
        backup = BackupService.create_backup(include_datastores=False)
        self.assertEqual(backup["backup_metadata"]["version"], "1.7.0")


class ScheduleHistorySnapshotTests(TestCase):
    """Part 5 note: a weekly/monthly-only edit must still record history."""

    def test_config_snapshot_covers_every_mode(self):
        schedule = ScriptSchedule.objects.create(
            script=_make_script(),
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days=[0],
            weekly_times=["09:00"],
        )
        snapshot = schedule.config_snapshot()
        for key in (
            "run_mode",
            "interval_minutes",
            "daily_times",
            "weekly_days",
            "weekly_times",
            "monthly_days",
            "monthly_times",
            "timezone",
            "is_active",
        ):
            self.assertIn(key, snapshot)

    def test_weekly_only_change_alters_snapshot(self):
        schedule = ScriptSchedule.objects.create(
            script=_make_script(),
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days=[0],
            weekly_times=["09:00"],
        )
        before = schedule.config_snapshot()
        schedule.weekly_days = [0, 2]
        after = schedule.config_snapshot()
        # The bug: previous_config == new_config because weekly_days was omitted.
        self.assertNotEqual(before, after)

    def test_weekly_only_edit_writes_history_via_view(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()

        default_ws = Workspace.get_default()
        user = User.objects.create(email="editor@example.com", is_staff=True, is_superuser=True)
        WorkspaceMembership.ensure(user, default_ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)

        # A default environment must exist or SetupMiddleware treats setup as
        # incomplete and 302s the POST to /setup/ before the view runs.
        env = Environment.objects.create(
            name="edit-env", path="editpath", is_active=True, is_default=True
        )
        script = Script.objects.create(
            name="weekly_edit", code="print('x')", environment=env, workspace=default_ws
        )
        schedule = ScriptSchedule.objects.create(
            script=script,
            workspace=default_ws,
            run_mode=ScriptSchedule.RunMode.WEEKLY,
            weekly_days=[0],
            weekly_times=["09:00"],
            timezone="UTC",
            is_active=True,
        )

        resp = self.client.post(
            reverse("cpanel:script_edit", args=[script.pk]),
            {
                # ScriptForm — unchanged
                "name": script.name,
                "code": script.code,
                "environment": str(env.id),
                "timeout_seconds": script.timeout_seconds,
                "isolation_mode": script.isolation_mode,
                "injection_mode": script.injection_mode,
                "notify_on": script.notify_on,
                # ScheduleForm — weekly_days is the ONLY change
                "run_mode": ScriptSchedule.RunMode.WEEKLY,
                "timezone": "UTC",
                "weekly_days_input": ["0", "2"],
                "weekly_times_input": "09:00",
                "is_active": "on",
            },
        )

        self.assertEqual(resp.status_code, 302)
        entry = ScheduleHistory.objects.filter(
            schedule=schedule, change_type=ScheduleHistory.ChangeType.UPDATED
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.previous_config["weekly_days"], [0])
        self.assertEqual(entry.new_config["weekly_days"], [0, 2])
