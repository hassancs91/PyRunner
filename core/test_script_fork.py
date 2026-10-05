"""
Tests for script forking (roadmap 3.5).

A fork is a same-workspace, user-owned duplicate: code, settings, tags,
notification channels, secret grants and library attachments come along; the
webhook token, plugin ownership and archive state do not; the schedule is
cloned paused. The coverage tests at the bottom pin the copy lists against
``Script._meta`` / ``ScriptSchedule._meta`` so a future column can't be
silently dropped from forks.
"""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import (
    Channel,
    Environment,
    Library,
    Script,
    ScriptSchedule,
    Secret,
    SecretGrant,
    Tag,
    Workspace,
    WorkspaceMembership,
)
from core.services import script_service
from core.services.script_service import fork_name, fork_script

User = get_user_model()


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class ForkServiceTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.author = User.objects.create(email="author@example.com")
        self.forker = User.objects.create(email="forker@example.com")
        self.env = Environment.objects.create(name="fenv", path=f"fenv{uuid.uuid4().hex[:6]}")
        self.tag = Tag.objects.create(name="billing", color="blue")
        self.channel = Channel.objects.create(
            workspace=self.ws, provider=Channel.Provider.TELEGRAM, name="ops"
        )
        self.secret = Secret.objects.create(workspace=self.ws, key="API_KEY", encrypted_value="x")
        self.library = Library.objects.create(key="helpers", name="Helpers", workspace=self.ws)
        self.library.save_revision({"util.py": "def f():\n    return 1\n"})

        self.source = Script.objects.create(
            name="Nightly sync",
            description="Pulls orders",
            code="print('sync')",
            environment=self.env,
            workspace=self.ws,
            created_by=self.author,
            timeout_seconds=120,
            is_enabled=False,
            injection_mode=Script.InjectionMode.SELECTED,
            isolation_mode=Script.IsolationMode.SANDBOXED,
            notify_on=Script.NotifyOn.FAILURE,
            notify_email="ops@example.com",
            notify_webhook_url="https://hooks.example.com/x",
            notify_webhook_enabled=True,
            retention_days_override=7,
            retention_count_override=50,
            owner_plugin="some_plugin",
            owner_key="nightly",
        )
        self.source.create_webhook_token()
        self.source.tags.add(self.tag)
        self.source.notify_channels.add(self.channel)
        SecretGrant.objects.create(script=self.source, secret=self.secret, active=True)
        self.source.libraries.add(self.library)
        self.schedule = ScriptSchedule.objects.create(
            script=self.source,
            workspace=self.ws,
            run_mode=ScriptSchedule.RunMode.DAILY,
            daily_times=["09:00", "18:00"],
            timezone="Asia/Beirut",
            is_active=True,
            q_schedule_ids=[41, 42],
        )

    def test_copies_settings_and_relations(self):
        fork = fork_script(self.source, user=self.forker)

        self.assertNotEqual(fork.pk, self.source.pk)
        self.assertEqual(fork.name, "Copy of Nightly sync")
        self.assertEqual(fork.workspace_id, self.ws.pk)
        self.assertEqual(fork.created_by, self.forker)
        for field in script_service.FORK_COPIED_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(fork, field), getattr(self.source, field))

        self.assertEqual(list(fork.tags.all()), [self.tag])
        self.assertEqual(list(fork.notify_channels.all()), [self.channel])
        self.assertEqual(
            list(fork.secret_grants.values_list("secret_id", "active")),
            [(self.secret.pk, True)],
        )
        self.assertEqual(list(fork.libraries.all()), [self.library])

    def test_resets_identity_webhook_and_ownership(self):
        fork = fork_script(self.source, user=self.forker)
        self.assertIsNone(fork.webhook_token)
        self.assertIsNone(fork.owner_plugin)
        self.assertIsNone(fork.owner_key)
        self.assertIsNone(fork.archived_at)
        self.assertIsNone(fork.archived_by)
        # The original is untouched.
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.webhook_token)
        self.assertEqual(self.source.owner_plugin, "some_plugin")

    def test_schedule_cloned_paused(self):
        fork = fork_script(self.source, user=self.forker)
        sched = fork.schedule
        self.assertEqual(sched.run_mode, "daily")
        self.assertEqual(sched.daily_times, ["09:00", "18:00"])
        self.assertEqual(sched.timezone, "Asia/Beirut")
        self.assertFalse(sched.is_active)
        self.assertEqual(sched.q_schedule_ids, [])
        self.assertIsNone(sched.next_run)
        self.assertEqual(sched.created_by, self.forker)
        # Source schedule still live.
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.is_active)

    def test_no_schedule_means_no_schedule(self):
        bare = Script.objects.create(
            name="bare", code="pass", environment=self.env, workspace=self.ws
        )
        fork = fork_script(bare, user=self.forker)
        self.assertFalse(ScriptSchedule.objects.filter(script=fork).exists())

    def test_runs_are_not_copied(self):
        from core.models import Run

        Run.objects.create(script=self.source, workspace=self.ws, code_snapshot="x")
        fork = fork_script(self.source, user=self.forker)
        self.assertEqual(fork.runs.count(), 0)

    def test_archived_source_forks_live(self):
        from django.utils import timezone

        self.source.archived_at = timezone.now()
        self.source.archived_by = self.author
        self.source.save()
        fork = fork_script(self.source, user=self.forker)
        self.assertFalse(fork.is_archived)

    def test_explicit_name(self):
        fork = fork_script(self.source, user=self.forker, name="Weekly sync")
        self.assertEqual(fork.name, "Weekly sync")

    def test_fork_name_respects_column_width(self):
        long = "x" * 300
        self.assertLessEqual(len(fork_name(long)), Script._meta.get_field("name").max_length)
        self.assertTrue(fork_name(long).startswith("Copy of "))


class ForkViewTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="fork-view@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.env = Environment.objects.create(name="venv", path=f"venv{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(
            name="Source", code="pass", environment=self.env, workspace=self.ws
        )
        self.client.force_login(self.user)

    def test_post_forks_and_redirects_to_edit(self):
        resp = self.client.post(reverse("cpanel:script_fork", args=[self.script.pk]))
        fork = Script.objects.exclude(pk=self.script.pk).get(name="Copy of Source")
        self.assertRedirects(
            resp, reverse("cpanel:script_edit", args=[fork.pk]), fetch_redirect_response=False
        )
        self.assertEqual(fork.created_by, self.user)

    def test_get_not_allowed(self):
        resp = self.client.get(reverse("cpanel:script_fork", args=[self.script.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_other_workspace_script_is_404(self):
        other = Workspace.objects.create(name="Other")
        foreign = Script.objects.create(
            name="Foreign", code="pass", environment=self.env, workspace=other
        )
        resp = self.client.post(reverse("cpanel:script_fork", args=[foreign.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Script.objects.filter(name__startswith="Copy of").count(), 0)

    def test_detail_page_shows_fork_button(self):
        resp = self.client.get(reverse("cpanel:script_detail", args=[self.script.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(reverse("cpanel:script_fork", args=[self.script.pk]), resp.content.decode())


class ForkFieldCoverageTests(TestCase):
    """Adding a column to Script/ScriptSchedule must be classified here."""

    @staticmethod
    def _concrete_fields(model):
        return {
            f.name
            for f in model._meta.get_fields()
            if getattr(f, "concrete", False) and not f.many_to_many
        }

    @staticmethod
    def _m2m_fields(model):
        return {f.name for f in model._meta.get_fields() if f.many_to_many and not f.auto_created}

    def test_every_script_field_is_copied_or_reset(self):
        listed = set(script_service.FORK_COPIED_FIELDS) | set(script_service.FORK_RESET_FIELDS)
        self.assertEqual(
            listed,
            self._concrete_fields(Script),
            "New Script field: add it to FORK_COPIED_FIELDS or FORK_RESET_FIELDS",
        )
        self.assertFalse(
            set(script_service.FORK_COPIED_FIELDS) & set(script_service.FORK_RESET_FIELDS)
        )

    def test_every_script_relation_is_copied(self):
        self.assertEqual(set(script_service.FORK_COPIED_RELATIONS), self._m2m_fields(Script))

    def test_every_schedule_field_is_copied_or_reset(self):
        listed = set(script_service.FORK_SCHEDULE_COPIED_FIELDS) | set(
            script_service.FORK_SCHEDULE_RESET_FIELDS
        )
        self.assertEqual(
            listed,
            self._concrete_fields(ScriptSchedule),
            "New ScriptSchedule field: add it to FORK_SCHEDULE_COPIED_FIELDS or "
            "FORK_SCHEDULE_RESET_FIELDS",
        )
