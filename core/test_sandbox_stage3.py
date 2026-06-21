"""
Sandbox Stage 3 — DB-driven policy hierarchy + fail-closed + RBAC-gated UI.

resolve_isolation(run) resolves whether a run is sandboxed from instance default
→ workspace policy (tighten-only) → per-script toggle, at run time. execute_run
selects the backend from that decision (an env value is break-glass), and a
'required' policy on a host that can't sandbox either degrades (default) or fails
the run (opt-in fail-closed). The workspace policy is Owner/Admin-gated.
"""

import sys
import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.executor import (
    IsolationDecision,
    _select_backend_for_run,
    execute_run,
    resolve_isolation,
)
from core.executor_backends import LocalSubprocessBackend, SandboxedSubprocessBackend
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)


def _gs(default="off", fail_closed=False):
    gs = GlobalSettings.get_settings()
    gs.sandbox_default = default
    gs.sandbox_fail_closed = fail_closed
    gs.save()
    return gs


def _make_run(instance_default="off", ws_policy=None, script_mode="inherit"):
    gs = _gs(instance_default)
    ws = Workspace.objects.create(name=f"w{uuid.uuid4().hex[:6]}", sandbox_policy=ws_policy)
    env = Environment.objects.create(name="e", path=f"env{uuid.uuid4().hex[:8]}")
    script = Script.objects.create(
        name="s", code="print('x')", environment=env, workspace=ws,
        isolation_mode=script_mode, timeout_seconds=3600,
    )
    run = Run.objects.create(script=script, workspace=ws, status=Run.Status.PENDING)
    return run, gs


class ResolveIsolationMatrixTests(TestCase):
    def test_default_off_is_plain(self):
        run, gs = _make_run("off", None, "inherit")
        d = resolve_isolation(run, gs)
        self.assertFalse(d.sandbox)
        self.assertFalse(d.mandatory)

    def test_optional_inherit_script_is_plain(self):
        run, gs = _make_run("optional", None, "inherit")
        self.assertFalse(resolve_isolation(run, gs).sandbox)

    def test_optional_sandboxed_script_opts_in(self):
        run, gs = _make_run("optional", None, "sandboxed")
        d = resolve_isolation(run, gs)
        self.assertTrue(d.sandbox)
        self.assertFalse(d.mandatory)  # optional => not fail-closed

    def test_optional_plain_script_stays_plain(self):
        run, gs = _make_run("optional", None, "plain")
        self.assertFalse(resolve_isolation(run, gs).sandbox)

    def test_required_instance_forces_sandbox_ignoring_script(self):
        run, gs = _make_run("required", None, "plain")  # script says plain…
        d = resolve_isolation(run, gs)
        self.assertTrue(d.sandbox)        # …but required wins
        self.assertTrue(d.mandatory)

    def test_workspace_tightens_off_instance_to_required(self):
        run, gs = _make_run("off", "required", "inherit")
        d = resolve_isolation(run, gs)
        self.assertTrue(d.sandbox)
        self.assertTrue(d.mandatory)

    def test_workspace_cannot_weaken_required_instance(self):
        # instance required + workspace 'off' => effective stays required.
        run, gs = _make_run("required", "off", "inherit")
        d = resolve_isolation(run, gs)
        self.assertTrue(d.sandbox)
        self.assertTrue(d.mandatory)

    def test_workspace_optional_with_sandboxed_script(self):
        run, gs = _make_run("off", "optional", "sandboxed")
        self.assertTrue(resolve_isolation(run, gs).sandbox)

    def test_workspace_optional_inherit_script_is_plain(self):
        run, gs = _make_run("off", "optional", "inherit")
        self.assertFalse(resolve_isolation(run, gs).sandbox)

    def test_falls_back_to_script_workspace_when_run_unstamped(self):
        run, gs = _make_run("off", "required", "inherit")
        run.workspace = None  # unstamped run; policy must come from script.workspace
        run.save(update_fields=["workspace"])
        self.assertTrue(resolve_isolation(run, gs).sandbox)


class SelectBackendTests(TestCase):
    def test_plain_policy_selects_local(self):
        run, gs = _make_run("off")
        backend, decision = _select_backend_for_run(run, gs)
        self.assertIsInstance(backend, LocalSubprocessBackend)
        self.assertFalse(decision.sandbox)

    def test_sandbox_policy_selects_sandboxed(self):
        run, gs = _make_run("required")
        backend, decision = _select_backend_for_run(run, gs)
        self.assertIsInstance(backend, SandboxedSubprocessBackend)
        self.assertTrue(decision.mandatory)

    def test_env_local_override_forces_plain(self):
        run, gs = _make_run("required")  # policy wants sandbox…
        with mock.patch.dict("os.environ", {"PYRUNNER_RUN_BACKEND": "local"}):
            backend, decision = _select_backend_for_run(run, gs)
        self.assertIsInstance(backend, LocalSubprocessBackend)  # …override wins

    def test_env_sandbox_override_forces_sandbox(self):
        run, gs = _make_run("off")  # policy says plain…
        with mock.patch.dict("os.environ", {"PYRUNNER_RUN_BACKEND": "sandbox"}):
            backend, decision = _select_backend_for_run(run, gs)
        self.assertIsInstance(backend, SandboxedSubprocessBackend)  # …override wins


class FailClosedTests(TestCase):
    """A required sandbox on a host that can't deliver it: degrade (default) vs
    fail the run (opt-in). On this dev host the runtime tier is 'none'."""

    def setUp(self):
        from core.executor_backends.sandboxed import reset_runtime_tier
        reset_runtime_tier()
        self.addCleanup(reset_runtime_tier)

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_required_degrades_and_runs_when_not_fail_closed(self, _val):
        run, gs = _make_run("required")  # fail_closed defaults False
        run.script.code = "print('ran anyway')"
        run.script.save(update_fields=["code"])
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)  # degraded, still ran
        self.assertIn("ran anyway", run.stdout)

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_required_fails_closed_blocks_run(self, _val):
        run, gs = _make_run("required")
        gs.sandbox_fail_closed = True
        gs.save(update_fields=["sandbox_fail_closed"])
        run.script.code = "print('should NOT run')"
        run.script.save(update_fields=["code"])
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertNotIn("should NOT run", run.stdout or "")  # never executed
        self.assertIn("fail-closed", run.stderr.lower())


class WorkspacePolicyGatingTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.ws = Workspace.objects.create(name="Acme")
        self.owner = User.objects.create(email="owner@example.com")
        self.admin = User.objects.create(email="admin@example.com")
        self.plain = User.objects.create(email="member@example.com")
        self.outsider = User.objects.create(email="out@example.com")
        self.super = User.objects.create(email="root@example.com", is_superuser=True)
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.ws, role="owner")
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.ws, role="admin")
        WorkspaceMembership.objects.create(user=self.plain, workspace=self.ws, role="member")
        self.url = reverse("cpanel:workspace_sandbox_policy", args=[self.ws.id])

    def _post(self, user, policy):
        self.client.force_login(user)
        return self.client.post(self.url, {"sandbox_policy": policy})

    def test_admin_can_set_policy(self):
        resp = self._post(self.admin, "required")
        self.assertEqual(resp.status_code, 302)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.sandbox_policy, "required")

    def test_owner_can_set_policy(self):
        self._post(self.owner, "optional")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.sandbox_policy, "optional")

    def test_blank_resets_to_inherit(self):
        self.ws.sandbox_policy = "required"
        self.ws.save(update_fields=["sandbox_policy"])
        self._post(self.admin, "")
        self.ws.refresh_from_db()
        self.assertIsNone(self.ws.sandbox_policy)

    def test_member_cannot_set_policy(self):
        resp = self._post(self.plain, "required")
        self.assertEqual(resp.status_code, 403)
        self.ws.refresh_from_db()
        self.assertIsNone(self.ws.sandbox_policy)

    def test_outsider_gets_404(self):
        resp = self._post(self.outsider, "required")
        self.assertEqual(resp.status_code, 404)
        self.ws.refresh_from_db()
        self.assertIsNone(self.ws.sandbox_policy)

    def test_superuser_can_set_policy(self):
        self._post(self.super, "required")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.sandbox_policy, "required")

    def test_invalid_policy_rejected(self):
        resp = self._post(self.admin, "bogus")
        self.assertEqual(resp.status_code, 302)
        self.ws.refresh_from_db()
        self.assertIsNone(self.ws.sandbox_policy)  # unchanged


class ScriptFormIsolationTests(TestCase):
    def test_form_exposes_isolation_mode(self):
        from core.forms import ScriptForm

        env = Environment.objects.create(name="e", path="envfm", is_active=True)
        form = ScriptForm(data={
            "name": "s", "code": "print('x')", "environment": str(env.id),
            "timeout_seconds": 60, "isolation_mode": "sandboxed", "notify_on": "never",
        })
        self.assertTrue(form.is_valid(), form.errors)
        script = form.save(commit=False)
        self.assertEqual(script.isolation_mode, "sandboxed")
