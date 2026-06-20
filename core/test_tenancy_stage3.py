"""
Tenancy Stage 3 — the view/service scoping sweep (leak-focused verification).

Covers the rows of the plan's leak matrix that Stage 3 closes: list/dashboard
scoping (only the active workspace's rows appear), object-level IDOR (a
cross-workspace pk 404s — never 403, never the object), the control-plane guard
(a tenant cannot force-stop/cancel another tenant's run), per-workspace secret
keys, the workspace-scoped public REST datastore list, and the shared-Environment
"scripts using this env" listing.

Fixture: WS-A and WS-B each own a script, secret, run and datastore. User U is a
member of WS-A ONLY, so a bare (unprefixed) request resolves to WS-A and a
WS-B object/URL must be invisible (404).
"""

from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.forms import SecretCreateForm
from core.models import (
    DataStore,
    DataStoreAPIToken,
    Environment,
    Run,
    Script,
    Secret,
    Workspace,
    WorkspaceMembership,
)
from core.services.task_service import TaskService


def _mock_setup(test):
    """Stop the setup-wizard middleware from 302-ing client requests."""
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class _Fixture(TestCase):
    """Two workspaces with one of each scoped resource, and a WS-A-only user."""

    def setUp(self):
        _mock_setup(self)
        from core.models import User

        self.default = Workspace.get_default()
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.env = Environment.objects.create(name="shared", path="s3env")

        self.script_a = Script.objects.create(
            name="alpha_script", code="x", environment=self.env,
            workspace=self.ws_a, is_enabled=True,
        )
        self.script_b = Script.objects.create(
            name="beta_script", code="x", environment=self.env,
            workspace=self.ws_b, is_enabled=True,
        )
        self.secret_a = Secret.objects.create(
            key="ALPHA_SECRET", encrypted_value="x", workspace=self.ws_a
        )
        self.secret_b = Secret.objects.create(
            key="BETA_SECRET", encrypted_value="x", workspace=self.ws_b
        )
        self.run_a = Run.objects.create(script=self.script_a, workspace=self.ws_a)
        self.run_b = Run.objects.create(script=self.script_b, workspace=self.ws_b)
        self.store_a = DataStore.objects.create(name="alpha_store", workspace=self.ws_a)
        self.store_b = DataStore.objects.create(name="beta_store", workspace=self.ws_b)

        # U is a member of WS-A ONLY (drop the auto default-workspace membership
        # the new-user signal creates), so bare URLs resolve to WS-A.
        self.user = User.objects.create(email="u@example.com")
        WorkspaceMembership.objects.filter(user=self.user).delete()
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws_a, role=WorkspaceMembership.ROLE_MEMBER
        )
        self.client.force_login(self.user)


class ListScopingTests(_Fixture):
    """Leak matrix rows 1 & 3 — lists/dashboard show only the active workspace."""

    def test_script_list_scoped(self):
        body = self.client.get(reverse("cpanel:script_list")).content.decode()
        self.assertIn("alpha_script", body)
        self.assertNotIn("beta_script", body)

    def test_secret_list_scoped(self):
        body = self.client.get(reverse("cpanel:secret_list")).content.decode()
        self.assertIn("ALPHA_SECRET", body)
        self.assertNotIn("BETA_SECRET", body)

    def test_run_list_scoped(self):
        body = self.client.get(reverse("cpanel:run_list")).content.decode()
        self.assertIn("alpha_script", body)
        self.assertNotIn("beta_script", body)

    def test_datastore_list_scoped(self):
        body = self.client.get(reverse("cpanel:datastore_list")).content.decode()
        self.assertIn("alpha_store", body)
        self.assertNotIn("beta_store", body)

    def test_dashboard_scoped(self):
        body = self.client.get(reverse("cpanel:dashboard")).content.decode()
        self.assertIn("alpha_script", body)
        self.assertNotIn("beta_script", body)


class IdorTests(_Fixture):
    """Leak matrix row 2 — every detail/mutate of a WS-B pk 404s for a WS-A user."""

    def test_script_detail_cross_ws_404(self):
        resp = self.client.get(
            reverse("cpanel:script_detail", args=[self.script_b.id])
        )
        self.assertEqual(resp.status_code, 404)

    def test_script_run_cross_ws_404_and_no_run_created(self):
        before = Run.objects.filter(script=self.script_b).count()
        resp = self.client.post(
            reverse("cpanel:script_run", args=[self.script_b.id])
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Run.objects.filter(script=self.script_b).count(), before)

    def test_script_delete_cross_ws_404(self):
        resp = self.client.post(
            reverse("cpanel:script_delete", args=[self.script_b.id])
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Script.objects.filter(pk=self.script_b.id).exists())

    def test_secret_edit_cross_ws_404(self):
        resp = self.client.get(
            reverse("cpanel:secret_edit", args=[self.secret_b.id])
        )
        self.assertEqual(resp.status_code, 404)

    def test_secret_delete_cross_ws_404(self):
        resp = self.client.post(
            reverse("cpanel:secret_delete", args=[self.secret_b.id])
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Secret.objects.filter(pk=self.secret_b.id).exists())

    def test_run_detail_cross_ws_404(self):
        resp = self.client.get(reverse("cpanel:run_detail", args=[self.run_b.id]))
        self.assertEqual(resp.status_code, 404)

    def test_own_workspace_objects_accessible(self):
        # Guard does not over-block: WS-A's own objects resolve normally.
        self.assertEqual(
            self.client.get(
                reverse("cpanel:script_detail", args=[self.script_a.id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("cpanel:run_detail", args=[self.run_a.id])
            ).status_code,
            200,
        )


class PrefixedUrlIdorTests(_Fixture):
    """Leak matrix rows 21 & 22 — the URL prefix is never trusted on its own."""

    def test_non_member_prefix_404(self):
        # Row 21: U (not a member of WS-B) hitting /w/<WS-B>/scripts/ 404s at the
        # middleware — membership is validated from the URL, never trusted.
        resp = self.client.get(
            reverse("cpanel_ws:script_list", args=[self.ws_b.id])
        )
        self.assertEqual(resp.status_code, 404)

    def test_object_under_valid_prefix_still_scoped(self):
        # Row 22: a user who IS a member of both workspaces still cannot open a
        # WS-B object under the WS-A prefix — object-level scoping applies inside
        # a valid prefix; the prefix alone does not grant the object.
        from core.models import User

        u2 = User.objects.create(email="both@example.com")
        WorkspaceMembership.objects.create(
            user=u2, workspace=self.ws_a, role=WorkspaceMembership.ROLE_MEMBER
        )
        WorkspaceMembership.objects.create(
            user=u2, workspace=self.ws_b, role=WorkspaceMembership.ROLE_MEMBER
        )
        self.client.force_login(u2)
        resp = self.client.get(
            reverse("cpanel_ws:script_detail", args=[self.ws_a.id, self.script_b.id])
        )
        self.assertEqual(resp.status_code, 404)


class ControlPlaneGuardTests(_Fixture):
    """Leak matrix row 10 — a tenant cannot force-stop/cancel another's job."""

    def test_force_stop_cross_workspace_denied_service(self):
        self.run_b.status = Run.Status.RUNNING
        self.run_b.task_id = "task-b"
        self.run_b.save(update_fields=["status", "task_id"])

        ok, _msg = TaskService.force_stop_task("task-b", workspace=self.ws_a)
        self.assertFalse(ok)
        self.run_b.refresh_from_db()
        self.assertEqual(self.run_b.status, Run.Status.RUNNING)  # untouched

    def test_cancel_cross_workspace_denied_service(self):
        self.run_b.status = Run.Status.PENDING
        self.run_b.task_id = "task-bp"
        self.run_b.save(update_fields=["status", "task_id"])

        ok, _msg = TaskService.cancel_queued_task("task-bp", workspace=self.ws_a)
        self.assertFalse(ok)
        self.run_b.refresh_from_db()
        self.assertEqual(self.run_b.status, Run.Status.PENDING)  # untouched

    def test_force_stop_own_workspace_allowed_service(self):
        # Guard does not over-block: a WS-A run is stoppable from WS-A.
        self.run_a.status = Run.Status.RUNNING
        self.run_a.task_id = "task-a"
        self.run_a.pid = None
        self.run_a.save(update_fields=["status", "task_id", "pid"])

        ok, _msg = TaskService.force_stop_task("task-a", workspace=self.ws_a)
        self.assertTrue(ok)
        self.run_a.refresh_from_db()
        self.assertEqual(self.run_a.status, Run.Status.CANCELLED)

    def test_force_stop_cross_workspace_denied_view(self):
        self.run_b.status = Run.Status.RUNNING
        self.run_b.task_id = "task-bv"
        self.run_b.save(update_fields=["status", "task_id"])

        resp = self.client.post(
            reverse("cpanel:task_force_stop", args=["task-bv"])
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])
        self.run_b.refresh_from_db()
        self.assertEqual(self.run_b.status, Run.Status.RUNNING)

    def test_task_detail_cross_workspace_404(self):
        self.run_b.task_id = "task-bd"
        self.run_b.save(update_fields=["task_id"])
        resp = self.client.get(
            reverse("cpanel:task_detail", args=["task-bd"])
        )
        self.assertEqual(resp.status_code, 404)


class SecretKeyUniquenessTests(TestCase):
    """Leak matrix row 13 — secret keys are unique PER workspace, not globally."""

    def setUp(self):
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")

    def test_same_key_across_workspaces_allowed(self):
        Secret.objects.create(key="API_KEY", encrypted_value="x", workspace=self.ws_a)
        Secret.objects.create(key="API_KEY", encrypted_value="y", workspace=self.ws_b)
        self.assertEqual(Secret.objects.filter(key="API_KEY").count(), 2)

    def test_duplicate_key_within_workspace_rejected(self):
        Secret.objects.create(key="API_KEY", encrypted_value="x", workspace=self.ws_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Secret.objects.create(
                    key="API_KEY", encrypted_value="z", workspace=self.ws_a
                )

    def test_null_workspace_key_globally_unique(self):
        Secret.objects.create(key="LEGACY", encrypted_value="x", workspace=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Secret.objects.create(key="LEGACY", encrypted_value="z", workspace=None)

    def test_form_clean_key_scoped_to_workspace(self):
        Secret.objects.create(key="SHARED", encrypted_value="x", workspace=self.ws_a)
        # Same key in a DIFFERENT workspace is allowed by the form.
        ok_form = SecretCreateForm(
            {"key": "SHARED", "value": "v"}, workspace=self.ws_b
        )
        self.assertTrue(ok_form.is_valid(), ok_form.errors)
        # Same key in the SAME workspace is rejected.
        dup_form = SecretCreateForm(
            {"key": "SHARED", "value": "v"}, workspace=self.ws_a
        )
        self.assertFalse(dup_form.is_valid())
        self.assertIn("key", dup_form.errors)


class RestDatastoreListScopingTests(TestCase):
    """Leak matrix row 11 — a workspace-bound token lists only its datastores."""

    def setUp(self):
        _mock_setup(self)
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.store_a = DataStore.objects.create(name="alpha_store", workspace=self.ws_a)
        self.store_b = DataStore.objects.create(name="beta_store", workspace=self.ws_b)
        # Global (no-datastore) token bound to WS-A.
        self.token_a = DataStoreAPIToken.objects.create(
            name="ta",
            token=DataStoreAPIToken.generate_token(),
            workspace=self.ws_a,
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token_a.token}"}

    def test_list_only_token_workspace_stores(self):
        resp = self.client.get("/api/v1/datastores/", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {d["name"] for d in resp.json()["datastores"]}
        self.assertIn("alpha_store", names)
        self.assertNotIn("beta_store", names)

    def test_cross_workspace_name_404(self):
        resp = self.client.get("/api/v1/datastores/beta_store/", **self._auth())
        self.assertEqual(resp.status_code, 404)


class EnvironmentDetailScopingTests(_Fixture):
    """Leak matrix row 19 — a shared env's script listing is workspace-scoped."""

    def test_scripts_listing_scoped(self):
        # The shared env is used by alpha_script (WS-A) and beta_script (WS-B);
        # U sees only WS-A's script even though the env itself is global.
        resp = self.client.get(
            reverse("cpanel:environment_detail", args=[self.env.id])
        )
        self.assertEqual(resp.status_code, 200)  # env is shared → reachable
        body = resp.content.decode()
        self.assertIn("alpha_script", body)
        self.assertNotIn("beta_script", body)
