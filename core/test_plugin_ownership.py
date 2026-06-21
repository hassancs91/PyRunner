"""
Plugin Platform v2 — Stage 2a (Ownership + Scoped Secrets, WS3) tests.

Covers the data-model + executor half of WS3:
  * Secret uniqueness re-scoped to (workspace, owner_plugin) — user secrets keep
    their exact per-workspace rule; owned secrets get their own per-owner rule.
  * The executor's injection-mode resolution: 'all' (default) = user secrets only
    (byte-identical to pre-v2); 'selected' = global + same-owner + granted, by
    clean name, with global < same-owner < grant precedence.
  * DataStore name stays per-workspace unique regardless of owner.
  * PYRUNNER_OWNER_PLUGIN exposed to an owned script's run env.
"""

from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment, resolve_secrets_for_run
from core.models import (
    DataStore,
    Environment,
    Run,
    Script,
    Secret,
    SecretGrant,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.plugin_service import PluginService


def _secret(key, value, *, workspace=None, owner_plugin=None, owner_key=None):
    s = Secret(
        key=key, workspace=workspace, owner_plugin=owner_plugin, owner_key=owner_key
    )
    s.set_value(value)
    s.save()
    return s


class SecretUniquenessTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.ws2 = Workspace.objects.create(name="Other")

    def test_two_user_secrets_same_key_same_workspace_rejected(self):
        _secret("API_KEY", "a", workspace=self.ws)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _secret("API_KEY", "b", workspace=self.ws)

    def test_user_secrets_same_key_different_workspace_ok(self):
        _secret("API_KEY", "a", workspace=self.ws)
        _secret("API_KEY", "b", workspace=self.ws2)  # different workspace → ok

    def test_two_user_secrets_same_key_null_workspace_rejected(self):
        _secret("GLOBAL", "a", workspace=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _secret("GLOBAL", "b", workspace=None)

    def test_two_plugins_same_key_same_workspace_ok(self):
        _secret("R2_BUCKET", "a", workspace=self.ws, owner_plugin="plugin_a")
        # A different owner may reuse the clean key in the same workspace.
        _secret("R2_BUCKET", "b", workspace=self.ws, owner_plugin="plugin_b")

    def test_same_plugin_same_key_same_workspace_rejected(self):
        _secret("R2_BUCKET", "a", workspace=self.ws, owner_plugin="plugin_a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            _secret("R2_BUCKET", "b", workspace=self.ws, owner_plugin="plugin_a")

    def test_user_and_owned_same_key_same_workspace_ok(self):
        _secret("API_KEY", "user", workspace=self.ws)
        # An owned secret never collides with the user secret of the same key.
        _secret("API_KEY", "owned", workspace=self.ws, owner_plugin="plugin_a")


class InjectionModeTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="ownenv")

        # Secrets: a global user secret, two owned secrets (different owners), and
        # a SHARED key existing both globally and under plugin_a (clash test).
        _secret("GLOBAL_KEY", "g", workspace=self.ws)
        _secret("OWNED_KEY", "owned-a", workspace=self.ws, owner_plugin="plugin_a")
        _secret("OTHER_KEY", "owned-b", workspace=self.ws, owner_plugin="plugin_b")
        _secret("SHARED", "global-val", workspace=self.ws)
        _secret("SHARED", "plugin-a-val", workspace=self.ws, owner_plugin="plugin_a")

        self.user_script = Script.objects.create(
            name="user", code="x", environment=self.env, workspace=self.ws
        )
        self.owned_script = Script.objects.create(
            name="owned", code="x", environment=self.env, workspace=self.ws,
            owner_plugin="plugin_a", owner_key="backup",
            injection_mode=Script.InjectionMode.SELECTED,
        )
        self.user_run = Run.objects.create(script=self.user_script, workspace=self.ws)
        self.owned_run = Run.objects.create(script=self.owned_script, workspace=self.ws)

    def test_all_mode_injects_only_user_secrets(self):
        out = resolve_secrets_for_run(self.user_run)
        self.assertEqual(out.get("GLOBAL_KEY"), "g")
        self.assertEqual(out.get("SHARED"), "global-val")  # global wins for user script
        self.assertNotIn("OWNED_KEY", out)   # plugin secret never leaks into a user script
        self.assertNotIn("OTHER_KEY", out)

    def test_selected_includes_global_and_same_owner_only(self):
        out = resolve_secrets_for_run(self.owned_run)
        self.assertEqual(out.get("GLOBAL_KEY"), "g")     # explicitly-global injected
        self.assertEqual(out.get("OWNED_KEY"), "owned-a")  # same-owner injected
        self.assertNotIn("OTHER_KEY", out)               # other plugin's secret excluded

    def test_selected_same_owner_overrides_global_on_clean_name_clash(self):
        out = resolve_secrets_for_run(self.owned_run)
        self.assertEqual(out.get("SHARED"), "plugin-a-val")  # same-owner wins

    def test_selected_injects_active_grant_and_skips_inactive(self):
        # Grant plugin_b's secret to plugin_a's script (it would not inject otherwise).
        other = Secret.objects.get(key="OTHER_KEY", owner_plugin="plugin_b")
        grant = SecretGrant.objects.create(script=self.owned_script, secret=other)

        out = resolve_secrets_for_run(self.owned_run)
        self.assertEqual(out.get("OTHER_KEY"), "owned-b")  # granted → injected

        grant.active = False
        grant.save(update_fields=["active"])
        out = resolve_secrets_for_run(self.owned_run)
        self.assertNotIn("OTHER_KEY", out)  # inactive grant → not injected

    def test_masking_set_matches_injection_set(self):
        # The env builder injects exactly the resolved clean names (shared resolver
        # → masking can't drift). No other workspace's owned value bleeds in.
        env = _build_script_environment(run=self.owned_run)
        resolved = resolve_secrets_for_run(self.owned_run)
        for name, value in resolved.items():
            self.assertEqual(env.get(name), value)
        self.assertNotIn("OTHER_KEY", env)

    def test_owner_plugin_env_set_for_owned_script_only(self):
        self.assertEqual(
            _build_script_environment(run=self.owned_run).get("PYRUNNER_OWNER_PLUGIN"),
            "plugin_a",
        )
        self.assertNotIn(
            "PYRUNNER_OWNER_PLUGIN", _build_script_environment(run=self.user_run)
        )


class DataStoreOwnershipTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()

    def test_name_unique_per_workspace_regardless_of_owner(self):
        DataStore.objects.create(name="state", workspace=self.ws, owner_plugin="plugin_a")
        # owner is grouping metadata only — it is NOT part of the uniqueness key,
        # so a second 'state' in the same workspace is rejected even under a
        # different owner (the SDK avoids this by auto-naming "<slug>:<key>").
        with self.assertRaises(IntegrityError), transaction.atomic():
            DataStore.objects.create(
                name="state", workspace=self.ws, owner_plugin="plugin_b"
            )

    def test_owner_fields_default_null(self):
        ds = DataStore.objects.create(name="plain", workspace=self.ws)
        self.assertIsNone(ds.owner_plugin)
        self.assertIsNone(ds.owner_key)


class BackupRoundTripTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="bkenv")

    def test_owner_fields_and_grants_round_trip(self):
        owned_script = Script.objects.create(
            name="o", code="x", environment=self.env, workspace=self.ws,
            owner_plugin="pluginx", owner_key="backup",
            injection_mode=Script.InjectionMode.SELECTED,
        )
        _secret("OWNED", "v", workspace=self.ws, owner_plugin="pluginx", owner_key="api")
        user_secret = _secret("USERK", "u", workspace=self.ws)
        DataStore.objects.create(
            name="pluginx:state", workspace=self.ws,
            owner_plugin="pluginx", owner_key="state",
        )
        SecretGrant.objects.create(script=owned_script, secret=user_secret)

        backup = BackupService.create_backup(include_runs=False)
        result = BackupService.restore_backup(backup, restore_runs=False)
        self.assertTrue(result["success"], result.get("errors"))

        s2 = Script.objects.get(owner_plugin="pluginx")
        self.assertEqual(s2.owner_key, "backup")
        self.assertEqual(s2.injection_mode, "selected")
        self.assertEqual(Secret.objects.get(key="OWNED").owner_plugin, "pluginx")
        self.assertEqual(DataStore.objects.get(name="pluginx:state").owner_plugin, "pluginx")
        self.assertTrue(
            SecretGrant.objects.filter(script=s2, secret__key="USERK", active=True).exists()
        )

    def test_pre_v2_backup_imports_as_null_owner_all_mode(self):
        Script.objects.create(name="s", code="x", environment=self.env, workspace=self.ws)
        _secret("K", "v", workspace=self.ws)
        DataStore.objects.create(name="d", workspace=self.ws)

        backup = BackupService.create_backup(include_runs=False)
        # Mimic a pre-1.3.0 backup: strip every v2 field.
        backup["backup_metadata"]["version"] = "1.2.0"
        for s in backup["scripts"]:
            for k in ("owner_plugin", "owner_key", "injection_mode", "secret_grants"):
                s.pop(k, None)
        for s in backup["secrets"]:
            s.pop("owner_plugin", None)
            s.pop("owner_key", None)
        for d in backup["datastores"]:
            d.pop("owner_plugin", None)
            d.pop("owner_key", None)

        result = BackupService.restore_backup(backup, restore_runs=False)
        self.assertTrue(result["success"], result.get("errors"))
        s2 = Script.objects.get(name="s")
        self.assertIsNone(s2.owner_plugin)
        self.assertEqual(s2.injection_mode, "all")  # legacy default
        self.assertIsNone(Secret.objects.get(key="K").owner_plugin)
        self.assertIsNone(DataStore.objects.get(name="d").owner_plugin)


class OwnedCleanupTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="clenv")

    def test_cleanup_removes_only_owned_rows(self):
        Script.objects.create(
            name="o", code="x", environment=self.env, workspace=self.ws, owner_plugin="pp"
        )
        Script.objects.create(name="u", code="x", environment=self.env, workspace=self.ws)
        _secret("OWNED", "v", workspace=self.ws, owner_plugin="pp")
        _secret("USERK", "u", workspace=self.ws)
        DataStore.objects.create(name="pp:state", workspace=self.ws, owner_plugin="pp")
        DataStore.objects.create(name="userds", workspace=self.ws)

        counts = PluginService._cleanup_owned_resources("pp")

        self.assertGreaterEqual(counts.get("scripts", 0), 1)
        # Owned rows gone, user rows untouched.
        self.assertFalse(Script.objects.filter(owner_plugin="pp").exists())
        self.assertTrue(Script.objects.filter(name="u").exists())
        self.assertFalse(Secret.objects.filter(owner_plugin="pp").exists())
        self.assertTrue(Secret.objects.filter(key="USERK").exists())
        self.assertFalse(DataStore.objects.filter(owner_plugin="pp").exists())
        self.assertTrue(DataStore.objects.filter(name="userds").exists())


class DeleteGuardTests(TestCase):
    """Owned resources are delete-guarded on the generic Secrets page; a superuser
    force=1 is the escape hatch. (Same guard wraps the script + datastore deletes.)"""

    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.ws = Workspace.get_default()
        self.member = User.objects.create(email="m@example.com")
        self.root = User.objects.create(email="r@example.com", is_superuser=True)
        WorkspaceMembership.ensure(self.member, self.ws, WorkspaceMembership.ROLE_MEMBER)
        WorkspaceMembership.ensure(self.root, self.ws, WorkspaceMembership.ROLE_OWNER)

        self.owned = _secret("OWNED", "v", workspace=self.ws, owner_plugin="pp", owner_key="api")
        self.free = _secret("FREE", "v", workspace=self.ws)

    def _delete(self, secret, data=None):
        return self.client.post(
            reverse("cpanel:secret_delete", args=[secret.pk]), data or {}
        )

    def test_member_cannot_delete_owned(self):
        self.client.force_login(self.member)
        self._delete(self.owned)
        self.assertTrue(Secret.objects.filter(pk=self.owned.pk).exists())

    def test_superuser_blocked_without_force(self):
        self.client.force_login(self.root)
        self._delete(self.owned)
        self.assertTrue(Secret.objects.filter(pk=self.owned.pk).exists())

    def test_superuser_force_deletes_owned(self):
        self.client.force_login(self.root)
        self._delete(self.owned, {"force": "1"})
        self.assertFalse(Secret.objects.filter(pk=self.owned.pk).exists())

    def test_unowned_secret_deletes_normally(self):
        self.client.force_login(self.member)
        self._delete(self.free)
        self.assertFalse(Secret.objects.filter(pk=self.free.pk).exists())
