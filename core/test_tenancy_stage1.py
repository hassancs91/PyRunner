"""
Tenancy Stage 1 — run-stamping + the shared workspace-scoped secret resolver.

Closes the worst leak: secret injection AND output masking go through ONE
resolver (so they can never drift), scoped to the run's workspace; and every run
is stamped with its script's workspace at creation (manual, scheduled, webhook —
the request and no-request paths). A transitional rule keeps a single-workspace
instance byte-for-byte: an un-scoped (NULL) run injects everything, and
still-unassigned (NULL-workspace) secrets keep injecting until the Stage 3 sweep.
"""

import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment, resolve_secrets_for_run
from core.models import (
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Secret,
    User,
    Workspace,
)


def _secret(key, value, workspace):
    s = Secret(key=key, workspace=workspace)
    s.set_value(value)
    s.save()
    return s


class ResolveSecretsForRunTests(TestCase):
    def setUp(self):
        self.default = Workspace.get_default()
        self.ws_b = Workspace.objects.create(name="B")
        self.env = Environment.objects.create(name="e", path="s1env")
        self.script_a = Script.objects.create(
            name="a", code="x", environment=self.env, workspace=self.default
        )
        self.run_a = Run.objects.create(script=self.script_a, workspace=self.default)
        _secret("A_KEY", "aval", self.default)
        _secret("B_KEY", "bval", self.ws_b)
        _secret("NULL_KEY", "nval", None)

    def test_scoped_to_workspace_plus_null(self):
        out = resolve_secrets_for_run(self.run_a)
        self.assertEqual(out.get("A_KEY"), "aval")
        self.assertEqual(out.get("NULL_KEY"), "nval")  # unassigned still injects
        self.assertNotIn("B_KEY", out)  # WS-B secret never reaches a WS-A run

    def test_null_workspace_run_gets_all(self):
        run_null = Run.objects.create(script=self.script_a, workspace=None)
        out = resolve_secrets_for_run(run_null)
        self.assertEqual(set(out), {"A_KEY", "B_KEY", "NULL_KEY"})

    def test_none_run_gets_all(self):
        self.assertEqual(set(resolve_secrets_for_run(None)), {"A_KEY", "B_KEY", "NULL_KEY"})

    def test_injection_env_is_scoped(self):
        env = _build_script_environment(run=self.run_a)
        self.assertEqual(env.get("A_KEY"), "aval")
        self.assertEqual(env.get("NULL_KEY"), "nval")
        self.assertNotIn("B_KEY", env)  # not injected into the subprocess env

    def test_masking_set_has_no_cross_workspace_value(self):
        # Masking uses the same resolved set as injection; WS-B's value is absent,
        # so it can be neither injected nor (incorrectly) masked into WS-A output.
        injected = resolve_secrets_for_run(self.run_a)
        self.assertNotIn("bval", injected.values())


class RunStampingTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.ws_a = Workspace.objects.create(name="A")
        self.env = Environment.objects.create(name="e", path="s1stamp")
        self.script = Script.objects.create(
            name="s",
            code="print(1)",
            environment=self.env,
            workspace=self.ws_a,
            is_enabled=True,
        )
        self.user = User.objects.create(email="u@example.com", is_superuser=True)

    @mock.patch("core.views.scripts.queue_script_run")
    def test_manual_run_stamped(self, _q):
        self.client.force_login(self.user)
        # Stage 3 scopes the manual-run view by the active workspace, so the
        # ws_a script must be reached under the ws_a URL prefix (the bare URL
        # resolves to the superuser's default workspace and would 404 here).
        self.client.post(
            reverse("cpanel_ws:script_run", args=[self.ws_a.id, self.script.id])
        )
        run = Run.objects.latest("created_at")
        self.assertEqual(run.workspace_id, self.ws_a.id)

    @mock.patch("core.tasks.queue_script_run")
    @mock.patch(
        "core.services.schedule_service.ScheduleService._calculate_next_run",
        return_value=None,
    )
    def test_scheduled_run_stamped(self, _calc, _q):
        from core.tasks import execute_scheduled_run

        ScriptSchedule.objects.create(script=self.script, is_active=True)
        execute_scheduled_run(str(self.script.id))
        run = Run.objects.latest("created_at")
        self.assertEqual(run.workspace_id, self.ws_a.id)
        self.assertEqual(run.trigger_type, Run.TriggerType.SCHEDULED)

    @mock.patch("core.views.webhooks.queue_script_run")
    def test_webhook_run_stamped(self, _q):
        self.script.webhook_token = "tok_" + uuid.uuid4().hex
        self.script.save(update_fields=["webhook_token"])
        self.client.post(
            reverse("webhook_trigger", args=[self.script.webhook_token]),
            data="{}",
            content_type="application/json",
        )
        run = Run.objects.latest("created_at")
        self.assertEqual(run.workspace_id, self.ws_a.id)
        self.assertEqual(run.trigger_type, Run.TriggerType.API)
