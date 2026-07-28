"""
Tests for External Secret Providers — Stage 1 (backend seam + Vault).

All HTTP is mocked (Windows dev has no Vault; live verification is Stage 4). The
two load-bearing regression suites are at the bottom:
  (a) LocalPathUnchangedTests — existing local secrets resolve byte-for-byte on
      today's path (log-and-skip on decrypt error, never fail-closed).
  (b) ExternalMaskingRunTests — external values flow through the SAME shared
      resolver dict, so they get injected AND masked in run output for free.
"""

import json
import sys
import uuid
from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.executor import execute_run, resolve_secrets_for_run
from core.forms import SecretCreateForm, SecretEditForm, SecretProviderForm
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    Secret,
    SecretProvider,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.encryption_service import EncryptionService
from core.services.secret_backends import (
    SecretBackend,
    SecretResolutionError,
    clear_cache,
    get_backend,
    list_backends,
    register,
    resolve_secret_ref,
)
from core.services.secret_backends import base as sb_base
from core.services.secret_backends import cache as sb_cache
from core.services.secret_backends.vault import VaultBackend

_TEST_KEY = Fernet.generate_key().decode()

_VAULT_GET = "core.services.secret_backends.vault.requests.get"


def _vault_response(status=200, data=None, text=""):
    """A mock ``requests`` response shaped like Vault KV v2 (``data.data``)."""
    resp = mock.Mock()
    resp.status_code = status
    resp.text = text
    if data is None:
        resp.json.side_effect = ValueError("no json body")
    else:
        resp.json.return_value = {"data": {"data": data, "metadata": {}}}
    return resp


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _Base(TestCase):
    """Shared setup: clears the in-process cache so call-count assertions are
    deterministic and no value leaks across tests."""

    def setUp(self):
        super().setUp()
        clear_cache()
        self.addCleanup(clear_cache)

    def _provider(
        self,
        name=None,
        cache_ttl=300,
        on_error="fail",
        token="hvs.t0ken",
        base_url="https://vault.example.com:8200",
        mount="secret",
        namespace="",
    ):
        p = SecretProvider(
            provider_type="vault",
            name=name or f"vault-{uuid.uuid4().hex[:6]}",
            config={"base_url": base_url, "mount": mount, "namespace": namespace},
            cache_ttl=cache_ttl,
            on_error=on_error,
        )
        p.set_credentials({"token": token})
        p.save()
        return p


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class RegistryTests(_Base):
    def test_get_backend_returns_vault(self):
        self.assertIsInstance(get_backend("vault"), VaultBackend)

    def test_unknown_backend_raises_resolution_error(self):
        with self.assertRaises(SecretResolutionError):
            get_backend("does-not-exist")

    def test_list_backends_includes_vault(self):
        keys = [b.provider_key for b in list_backends()]
        self.assertIn("vault", keys)

    def test_register_then_get(self):
        class DummyBackend(SecretBackend):
            provider_key = "dummy-test"

        try:
            register(DummyBackend())
            self.assertIsInstance(get_backend("dummy-test"), DummyBackend)
        finally:
            sb_base._REGISTRY.pop("dummy-test", None)


# --------------------------------------------------------------------------- #
# Reference parsing
# --------------------------------------------------------------------------- #
class RefParsingTests(_Base):
    def setUp(self):
        super().setUp()
        self.backend = VaultBackend()

    def test_path_and_key(self):
        self.assertEqual(self.backend.split_ref("kv/myapp#API_KEY"), ("kv/myapp", "API_KEY"))

    def test_hashless_ref_has_empty_key(self):
        self.assertEqual(self.backend.split_ref("just/a/path"), ("just/a/path", ""))

    def test_splits_on_last_hash(self):
        # A '#' inside the path survives; only the final segment is the key.
        self.assertEqual(self.backend.split_ref("weird#path#KEY"), ("weird#path", "KEY"))

    def test_whitespace_trimmed(self):
        self.assertEqual(self.backend.split_ref("  kv/app # KEY "), ("kv/app", "KEY"))


# --------------------------------------------------------------------------- #
# Vault adapter — fetch + test_connection (mocked HTTP)
# --------------------------------------------------------------------------- #
class VaultFetchTests(_Base):
    def test_fetch_returns_stringified_dict(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"API_KEY": "v", "PORT": 5432})) as g:
            values = get_backend("vault").fetch(p, "kv/app")
        self.assertEqual(values, {"API_KEY": "v", "PORT": "5432"})
        # URL is {base}/v1/{mount}/data/{path}, token + namespace headers present.
        called_url = g.call_args.args[0]
        self.assertEqual(called_url, "https://vault.example.com:8200/v1/secret/data/kv/app")
        self.assertEqual(g.call_args.kwargs["headers"]["X-Vault-Token"], "hvs.t0ken")

    def test_namespace_header_sent_when_configured(self):
        p = self._provider(namespace="admin/team-a")
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})) as g:
            get_backend("vault").fetch(p, "kv/app")
        self.assertEqual(g.call_args.kwargs["headers"]["X-Vault-Namespace"], "admin/team-a")

    def test_custom_mount_in_url(self):
        p = self._provider(mount="kv2")
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})) as g:
            get_backend("vault").fetch(p, "app")
        self.assertEqual(g.call_args.args[0], "https://vault.example.com:8200/v1/kv2/data/app")

    def test_404_raises_not_found(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=404)):
            with self.assertRaisesRegex(SecretResolutionError, "not found"):
                get_backend("vault").fetch(p, "missing")

    def test_403_raises_denied(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=403)):
            with self.assertRaisesRegex(SecretResolutionError, "denied"):
                get_backend("vault").fetch(p, "kv/app")

    def test_network_error_wrapped(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("boom")):
            with self.assertRaisesRegex(SecretResolutionError, "request failed"):
                get_backend("vault").fetch(p, "kv/app")

    def test_missing_base_url_raises(self):
        p = self._provider(base_url="")
        with self.assertRaisesRegex(SecretResolutionError, "base_url"):
            get_backend("vault").fetch(p, "kv/app")

    def test_missing_token_raises(self):
        p = self._provider(token="")
        with self.assertRaisesRegex(SecretResolutionError, "token"):
            get_backend("vault").fetch(p, "kv/app")

    def test_bad_response_shape_raises(self):
        p = self._provider()
        bad = mock.Mock(status_code=200, text="")
        bad.json.return_value = {"unexpected": True}
        with mock.patch(_VAULT_GET, return_value=bad):
            with self.assertRaisesRegex(SecretResolutionError, "Unexpected Vault response"):
                get_backend("vault").fetch(p, "kv/app")

    def test_test_connection_ok(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=200, data={})):
            ok, msg = get_backend("vault").test_connection(p)
        self.assertTrue(ok)

    def test_test_connection_rejected_token(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=403)):
            ok, msg = get_backend("vault").test_connection(p)
        self.assertFalse(ok)

    def test_test_connection_unreachable(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("no route")):
            ok, msg = get_backend("vault").test_connection(p)
        self.assertFalse(ok)
        self.assertIn("reach", msg)


# --------------------------------------------------------------------------- #
# Key extraction from a fetched path
# --------------------------------------------------------------------------- #
class KeyExtractionTests(_Base):
    def test_extracts_named_key(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1", "B": "2"})):
            self.assertEqual(resolve_secret_ref(p, "kv/app#B"), "2")

    def test_missing_key_raises(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})):
            with self.assertRaisesRegex(SecretResolutionError, "not found"):
                resolve_secret_ref(p, "kv/app#NOPE")

    def test_hashless_ref_single_value_returns_it(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"only": "solo"})):
            self.assertEqual(resolve_secret_ref(p, "kv/app"), "solo")

    def test_hashless_ref_multiple_values_is_ambiguous(self):
        p = self._provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1", "B": "2"})):
            with self.assertRaisesRegex(SecretResolutionError, "needs a '#key'"):
                resolve_secret_ref(p, "kv/app")


# --------------------------------------------------------------------------- #
# Caching — one fetch per (profile, path) per TTL window
# --------------------------------------------------------------------------- #
class CachingTests(_Base):
    def test_two_keys_one_path_single_fetch(self):
        p = self._provider(cache_ttl=300)
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1", "B": "2"})) as g:
            self.assertEqual(resolve_secret_ref(p, "kv/app#A"), "1")
            self.assertEqual(resolve_secret_ref(p, "kv/app#B"), "2")
        self.assertEqual(g.call_count, 1)

    def test_ttl_zero_disables_cache(self):
        p = self._provider(cache_ttl=0)
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})) as g:
            resolve_secret_ref(p, "kv/app#A")
            resolve_secret_ref(p, "kv/app#A")
        self.assertEqual(g.call_count, 2)

    def test_cache_expiry_refetches(self):
        p = self._provider(cache_ttl=10)
        clock = {"t": 1000.0}
        with mock.patch.object(sb_cache.time, "monotonic", side_effect=lambda: clock["t"]):
            with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})) as g:
                resolve_secret_ref(p, "kv/app#A")  # fetch @1000
                clock["t"] = 1005.0
                resolve_secret_ref(p, "kv/app#A")  # fresh (5s < 10s)
                self.assertEqual(g.call_count, 1)
                clock["t"] = 1020.0
                resolve_secret_ref(p, "kv/app#A")  # expired (20s ≥ 10s) → refetch
                self.assertEqual(g.call_count, 2)

    def test_distinct_paths_are_separate_entries(self):
        p = self._provider(cache_ttl=300)
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "1"})) as g:
            resolve_secret_ref(p, "kv/one#A")
            resolve_secret_ref(p, "kv/two#A")
        self.assertEqual(g.call_count, 2)


# --------------------------------------------------------------------------- #
# on_error policy
# --------------------------------------------------------------------------- #
class OnErrorTests(_Base):
    def test_fail_mode_propagates(self):
        p = self._provider(on_error="fail")
        with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("down")):
            with self.assertRaises(SecretResolutionError):
                resolve_secret_ref(p, "kv/app#A")

    def test_use_stale_serves_last_good_value(self):
        p = self._provider(on_error="use_stale", cache_ttl=10)
        clock = {"t": 100.0}
        with mock.patch.object(sb_cache.time, "monotonic", side_effect=lambda: clock["t"]):
            with mock.patch(_VAULT_GET, return_value=_vault_response(data={"A": "good"})):
                self.assertEqual(resolve_secret_ref(p, "kv/app#A"), "good")
            clock["t"] = 999.0  # past TTL
            with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("down")):
                self.assertEqual(resolve_secret_ref(p, "kv/app#A"), "good")  # stale

    def test_use_stale_without_prior_value_still_fails(self):
        p = self._provider(on_error="use_stale")
        with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("down")):
            with self.assertRaises(SecretResolutionError):
                resolve_secret_ref(p, "kv/app#A")


# --------------------------------------------------------------------------- #
# Secret dispatch on source + PROTECT
# --------------------------------------------------------------------------- #
class SecretDispatchTests(_Base):
    def test_local_masked_value_unchanged(self):
        s = Secret(key="API_KEY", source=Secret.Source.LOCAL)
        s.set_value("sk-abc123xyz789")
        s.save()
        self.assertEqual(s.get_masked_value(), "sk-...789")

    def test_external_masked_value_is_reference_no_fetch(self):
        p = self._provider()
        s = Secret.objects.create(
            key="API_KEY",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#API_KEY",
        )
        with mock.patch(_VAULT_GET) as g:
            self.assertEqual(s.get_masked_value(), "vault: kv/app#API_KEY")
        g.assert_not_called()  # list pages must never hit the provider

    def test_external_decrypt_dispatches_and_fetches(self):
        p = self._provider()
        s = Secret.objects.create(
            key="API_KEY",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#API_KEY",
        )
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"API_KEY": "live-value"})):
            self.assertEqual(s.get_decrypted_value(), "live-value")

    def test_external_error_wrapped_with_secret_and_provider_name(self):
        p = self._provider(name="Prod Vault")
        s = Secret.objects.create(
            key="API_KEY",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#API_KEY",
        )
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=404)):
            with self.assertRaises(SecretResolutionError) as ctx:
                s.get_decrypted_value()
        msg = str(ctx.exception)
        self.assertIn("Secret API_KEY", msg)
        self.assertIn("Prod Vault", msg)
        self.assertIn("vault", msg)

    def test_external_without_provider_raises(self):
        s = Secret.objects.create(
            key="API_KEY", source=Secret.Source.EXTERNAL, external_ref="kv/app#API_KEY"
        )
        with self.assertRaises(SecretResolutionError):
            s.get_decrypted_value()

    def test_delete_provider_in_use_is_protected(self):
        p = self._provider()
        Secret.objects.create(
            key="API_KEY",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#API_KEY",
        )
        with self.assertRaises(ProtectedError):
            p.delete()


# --------------------------------------------------------------------------- #
# Regression (a): the local path is byte-for-byte unchanged
# --------------------------------------------------------------------------- #
@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class LocalPathUnchangedTests(_Base):
    def test_local_get_decrypted_value_is_plain_fernet(self):
        s = Secret(key="API_KEY")  # source defaults to local
        s.set_value("plain-value-123")
        s.save()
        self.assertEqual(s.source, Secret.Source.LOCAL)
        self.assertEqual(s.get_decrypted_value(), "plain-value-123")
        self.assertEqual(
            s.get_decrypted_value(), EncryptionService.decrypt(s.encrypted_value)
        )

    def test_resolver_injects_local_secret(self):
        s = Secret(key="MY_LOCAL")
        s.set_value("local-secret-value")
        s.save()
        env = resolve_secrets_for_run(None)
        self.assertEqual(env["MY_LOCAL"], "local-secret-value")

    def test_corrupt_local_secret_is_logged_and_skipped_not_fatal(self):
        # A broken local decrypt must NOT fail the run (today's behavior) — only
        # external rows are fail-closed. Prove the fork didn't touch local rows.
        good = Secret(key="GOOD")
        good.set_value("good-value")
        good.save()
        Secret.objects.create(key="BROKEN", encrypted_value="not-a-fernet-token")
        env = resolve_secrets_for_run(None)  # must not raise
        self.assertEqual(env["GOOD"], "good-value")
        self.assertNotIn("BROKEN", env)


# --------------------------------------------------------------------------- #
# Regression (b): external values are masked in run output via the shared resolver
# --------------------------------------------------------------------------- #
@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ExternalMaskingRunTests(_Base):
    def _script(self, code):
        env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:10]}")
        return Script.objects.create(
            name="s", code=code, environment=env, timeout_seconds=60
        )

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_external_value_injected_and_masked(self, _val):
        p = self._provider()
        Secret.objects.create(
            key="MY_EXTERNAL",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#MY_EXTERNAL",
        )
        script = self._script("import os; print(os.environ['MY_EXTERNAL'])")
        run = Run.objects.create(script=script, status=Run.Status.PENDING)
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"MY_EXTERNAL": "super-secret-value"})):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertNotIn("super-secret-value", run.stdout)
        self.assertIn("[MY_EXTERNAL:MASKED]", run.stdout)

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_unresolvable_external_secret_fails_run_pre_exec(self, _val):
        p = self._provider(name="Prod Vault")
        Secret.objects.create(
            key="MY_EXTERNAL",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#MY_EXTERNAL",
        )
        script = self._script("print('SHOULD NOT RUN')")
        run = Run.objects.create(script=script, status=Run.Status.PENDING)
        with mock.patch(_VAULT_GET, side_effect=requests.ConnectionError("vault down")):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertNotIn("SHOULD NOT RUN", run.stdout or "")
        self.assertIn("Secret MY_EXTERNAL", run.stderr)
        self.assertIn("Prod Vault", run.stderr)


# =========================================================================== #
# Stage 2 — UI: forms + views
# =========================================================================== #
def _provider_post(**overrides):
    """A valid SecretProviderForm POST dict (Vault) with ``f_<name>`` adapter keys."""
    data = {
        "provider_type": "vault",
        "name": "Prod Vault",
        "cache_ttl": "300",
        "on_error": "fail",
        "f_base_url": "https://vault.example.com:8200",
        "f_mount": "secret",
        "f_namespace": "",
        "f_token": "hvs.t0ken",
    }
    data.update(overrides)
    return data


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class SecretProviderFormTests(TestCase):
    def test_valid_create_splits_config_and_credentials(self):
        form = SecretProviderForm(_provider_post())
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.provider_type, "vault")
        self.assertEqual(
            p.config,
            {"base_url": "https://vault.example.com:8200", "mount": "secret", "namespace": ""},
        )
        self.assertEqual(p.get_credentials(), {"token": "hvs.t0ken"})
        # Credentials are stored encrypted, never as plaintext JSON.
        self.assertTrue(p.credentials_encrypted)
        self.assertNotIn("hvs.t0ken", p.credentials_encrypted)

    def test_missing_required_config_invalid(self):
        form = SecretProviderForm(_provider_post(f_base_url=""))
        self.assertFalse(form.is_valid())

    def test_missing_required_credential_invalid_on_create(self):
        form = SecretProviderForm(_provider_post(f_token=""))
        self.assertFalse(form.is_valid())

    def test_credential_preserved_on_edit_when_blank(self):
        form = SecretProviderForm(_provider_post())
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        # Edit with a blank token but changed ttl → token kept, ttl updated.
        edit = SecretProviderForm(
            _provider_post(cache_ttl="600", on_error="use_stale", f_token=""),
            instance=p,
        )
        self.assertTrue(edit.is_valid(), edit.errors)
        p2 = edit.save()
        self.assertEqual(p2.cache_ttl, 600)
        self.assertEqual(p2.on_error, "use_stale")
        self.assertEqual(p2.get_credentials()["token"], "hvs.t0ken")

    def test_duplicate_name_invalid(self):
        first = SecretProviderForm(_provider_post(name="Dup"))
        self.assertTrue(first.is_valid(), first.errors)
        first.save()
        form = SecretProviderForm(_provider_post(name="dup"))  # case-insensitive
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_unknown_provider_type_invalid(self):
        form = SecretProviderForm(_provider_post(provider_type="nope"))
        self.assertFalse(form.is_valid())


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class SecretValueSourceFormTests(TestCase):
    def _provider(self):
        form = SecretProviderForm(_provider_post())
        assert form.is_valid(), form.errors
        return form.save()

    # --- create ---
    def test_local_requires_value(self):
        form = SecretCreateForm(
            {"key": "API_KEY", "source": "local", "value": ""}, workspace=None
        )
        self.assertFalse(form.is_valid())
        self.assertIn("value", form.errors)

    def test_local_valid(self):
        form = SecretCreateForm(
            {"key": "API_KEY", "source": "local", "value": "v"}, workspace=None
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_external_requires_provider_and_ref(self):
        form = SecretCreateForm(
            {"key": "API_KEY", "source": "external", "provider": "", "external_ref": ""},
            workspace=None,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("provider", form.errors)
        self.assertIn("external_ref", form.errors)

    def test_external_valid(self):
        p = self._provider()
        form = SecretCreateForm(
            {
                "key": "API_KEY",
                "source": "external",
                "provider": str(p.id),
                "external_ref": "kv/app#API_KEY",
            },
            workspace=None,
        )
        self.assertTrue(form.is_valid(), form.errors)

    # --- edit ---
    def test_edit_local_blank_value_kept_when_stored(self):
        s = Secret(key="K", source=Secret.Source.LOCAL)
        s.set_value("stored")
        s.save()
        form = SecretEditForm(
            {"source": "local", "value": "", "description": ""}, instance=s
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_edit_external_to_local_requires_value(self):
        p = self._provider()
        s = Secret.objects.create(
            key="E", source=Secret.Source.EXTERNAL, provider=p, external_ref="kv/app#K"
        )
        form = SecretEditForm(
            {"source": "local", "value": "", "description": ""}, instance=s
        )
        self.assertFalse(form.is_valid())
        self.assertIn("value", form.errors)

    def test_edit_switch_local_to_external(self):
        p = self._provider()
        s = Secret(key="SW", source=Secret.Source.LOCAL)
        s.set_value("v")
        s.save()
        form = SecretEditForm(
            {
                "source": "external",
                "provider": str(p.id),
                "external_ref": "kv/app#SW",
                "description": "",
            },
            instance=s,
        )
        self.assertTrue(form.is_valid(), form.errors)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _ViewBase(TestCase):
    def setUp(self):
        super().setUp()
        clear_cache()
        self.addCleanup(clear_cache)
        s = GlobalSettings.get_settings()
        s.setup_completed = True
        s.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create(
            email="admin@example.com", is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(
            self.admin, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER
        )
        self.client.force_login(self.admin)
        self.ws = Workspace.get_default()

    def _make_provider(self, name="Prod Vault", **cfg):
        p = SecretProvider(
            provider_type="vault",
            name=name,
            config={"base_url": "https://vault.example.com:8200", "mount": "secret", "namespace": ""},
            cache_ttl=300,
            on_error="fail",
        )
        p.set_credentials({"token": "hvs.t0ken"})
        p.save()
        return p


class SecretProviderViewTests(_ViewBase):
    def test_services_page_renders_card(self):
        resp = self.client.get(reverse("cpanel:services"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Secret Providers", resp.content)

    def test_save_creates_provider(self):
        resp = self.client.post(reverse("cpanel:secret_provider_save"), _provider_post())
        self.assertEqual(resp.status_code, 302)
        p = SecretProvider.objects.get(name="Prod Vault")
        self.assertEqual(p.get_credentials()["token"], "hvs.t0ken")

    def test_save_edits_provider_preserving_credential(self):
        p = self._make_provider()
        resp = self.client.post(
            reverse("cpanel:secret_provider_save"),
            _provider_post(
                provider_id=str(p.id), cache_ttl="600", on_error="use_stale", f_token=""
            ),
        )
        self.assertEqual(resp.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.cache_ttl, 600)
        self.assertEqual(p.on_error, "use_stale")
        self.assertEqual(p.get_credentials()["token"], "hvs.t0ken")

    def test_delete_unreferenced_provider(self):
        p = self._make_provider(name="Trash")
        resp = self.client.post(
            reverse("cpanel:secret_provider_delete", kwargs={"provider_id": p.id})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SecretProvider.objects.filter(pk=p.id).exists())

    def test_delete_referenced_provider_blocked(self):
        p = self._make_provider()
        Secret.objects.create(
            key="K",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#K",
            workspace=self.ws,
        )
        resp = self.client.post(
            reverse("cpanel:secret_provider_delete", kwargs={"provider_id": p.id})
        )
        self.assertEqual(resp.status_code, 302)
        # PROTECT surfaced as a friendly block, not a 500 — provider still there.
        self.assertTrue(SecretProvider.objects.filter(pk=p.id).exists())

    def test_test_connection_success(self):
        with mock.patch(_VAULT_GET, return_value=_vault_response(status=200, data={})):
            resp = self.client.post(
                reverse("cpanel:secret_provider_test"),
                data=json.dumps(
                    {
                        "provider_type": "vault",
                        "config": {"base_url": "https://v", "mount": "secret"},
                        "credentials": {"token": "t"},
                    }
                ),
                content_type="application/json",
            )
        self.assertTrue(resp.json()["success"])

    def test_test_connection_unknown_type(self):
        resp = self.client.post(
            reverse("cpanel:secret_provider_test"),
            data=json.dumps({"provider_type": "nope"}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["success"])


class SecretExternalViewTests(_ViewBase):
    def test_create_external_secret(self):
        p = self._make_provider()
        resp = self.client.post(
            reverse("cpanel:secret_create"),
            {
                "key": "MY_EXT",
                "source": "external",
                "provider": str(p.id),
                "external_ref": "kv/app#MY_EXT",
                "value": "",
                "description": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        s = Secret.objects.get(key="MY_EXT")
        self.assertEqual(s.source, Secret.Source.EXTERNAL)
        self.assertEqual(s.provider_id, p.id)
        self.assertEqual(s.external_ref, "kv/app#MY_EXT")
        self.assertEqual(s.encrypted_value, "")

    def test_create_page_renders_value_source(self):
        resp = self.client.get(reverse("cpanel:secret_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Value source", resp.content)

    def test_edit_switch_local_to_external_clears_value(self):
        p = self._make_provider()
        s = Secret(key="SW", source=Secret.Source.LOCAL, workspace=self.ws)
        s.set_value("v")
        s.save()
        resp = self.client.post(
            reverse("cpanel:secret_edit", kwargs={"pk": s.pk}),
            {
                "source": "external",
                "provider": str(p.id),
                "external_ref": "kv/app#SW",
                "value": "",
                "description": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        s.refresh_from_db()
        self.assertEqual(s.source, Secret.Source.EXTERNAL)
        self.assertEqual(s.provider_id, p.id)
        self.assertEqual(s.encrypted_value, "")

    def test_edit_switch_external_to_local_stores_value(self):
        p = self._make_provider()
        s = Secret.objects.create(
            key="BACK",
            source=Secret.Source.EXTERNAL,
            provider=p,
            external_ref="kv/app#BACK",
            workspace=self.ws,
        )
        resp = self.client.post(
            reverse("cpanel:secret_edit", kwargs={"pk": s.pk}),
            {"source": "local", "value": "now-local", "description": ""},
        )
        self.assertEqual(resp.status_code, 302)
        s.refresh_from_db()
        self.assertEqual(s.source, Secret.Source.LOCAL)
        self.assertIsNone(s.provider_id)
        self.assertEqual(s.get_decrypted_value(), "now-local")

    def test_test_resolve_returns_masked_value(self):
        p = self._make_provider()
        with mock.patch(_VAULT_GET, return_value=_vault_response(data={"MY_EXT": "super-secret-value"})):
            resp = self.client.post(
                reverse("cpanel:secret_test_resolve"),
                data=json.dumps({"provider_id": str(p.id), "external_ref": "kv/app#MY_EXT"}),
                content_type="application/json",
            )
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertNotIn("super-secret-value", data["message"])
        self.assertIn("sup...lue", data["message"])

    def test_test_resolve_without_provider_errors(self):
        resp = self.client.post(
            reverse("cpanel:secret_test_resolve"),
            data=json.dumps({"provider_id": "", "external_ref": "kv/app#X"}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["success"])


# =========================================================================== #
# Stage 3 — adapter fleet (aws_sm / infisical / doppler / custom), mocked HTTP
# =========================================================================== #
_INFIS_POST = "core.services.secret_backends.infisical.requests.post"
_INFIS_GET = "core.services.secret_backends.infisical.requests.get"
_DOPPLER_GET = "core.services.secret_backends.doppler.requests.get"
_CUSTOM_GET = "core.services.secret_backends.custom.requests.get"


def _http_json(status=200, body=None, text=""):
    """A generic mock ``requests`` response with a JSON body."""
    resp = mock.Mock(status_code=status, text=text)
    if body is None:
        resp.json.side_effect = ValueError("no json body")
    else:
        resp.json.return_value = body
    return resp


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _AdapterBase(TestCase):
    def setUp(self):
        super().setUp()
        clear_cache()
        self.addCleanup(clear_cache)

    def _provider(self, ptype, config, creds, on_error="fail", cache_ttl=300):
        p = SecretProvider(
            provider_type=ptype,
            name=f"{ptype}-{uuid.uuid4().hex[:6]}",
            config=config,
            cache_ttl=cache_ttl,
            on_error=on_error,
        )
        p.set_credentials(creds)
        p.save()
        return p


class AWSSecretsManagerTests(_AdapterBase):
    def _aws(self, **creds):
        return self._provider("aws_sm", {"region": "us-east-1"}, creds)

    def _client(self, get_return=None, get_side=None, list_side=None):
        client = mock.Mock()
        if get_side is not None:
            client.get_secret_value.side_effect = get_side
        else:
            client.get_secret_value.return_value = get_return
        if list_side is not None:
            client.list_secrets.side_effect = list_side
        else:
            client.list_secrets.return_value = {"SecretList": []}
        return client

    def test_fetch_json_secret_caches_per_path(self):
        p = self._aws(access_key_id="AKIA", secret_access_key="s")
        client = self._client(
            get_return={"SecretString": json.dumps({"API_KEY": "v", "PORT": 5432})}
        )
        with mock.patch("boto3.client", return_value=client):
            self.assertEqual(resolve_secret_ref(p, "prod/app#API_KEY"), "v")
            self.assertEqual(resolve_secret_ref(p, "prod/app#PORT"), "5432")
        # Two keys, one path → a single AWS call.
        self.assertEqual(client.get_secret_value.call_count, 1)

    def test_fetch_plaintext_secret(self):
        p = self._aws()  # ambient IAM (no creds)
        client = self._client(get_return={"SecretString": "plain-token-value"})
        with mock.patch("boto3.client", return_value=client):
            self.assertEqual(resolve_secret_ref(p, "prod/token"), "plain-token-value")

    def test_ambient_iam_omits_keys(self):
        p = self._aws()
        client = self._client(get_return={"SecretString": "x"})
        with mock.patch("boto3.client", return_value=client) as mk:
            resolve_secret_ref(p, "n")
        kwargs = mk.call_args.kwargs
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertEqual(kwargs["region_name"], "us-east-1")

    def test_partial_credentials_error(self):
        p = self._aws(access_key_id="AKIA")  # secret key missing
        with self.assertRaisesRegex(SecretResolutionError, "both"):
            resolve_secret_ref(p, "n")

    def test_not_found_raises(self):
        from botocore.exceptions import ClientError

        p = self._aws()
        err = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
            "GetSecretValue",
        )
        client = self._client(get_side=err)
        with mock.patch("boto3.client", return_value=client):
            with self.assertRaisesRegex(SecretResolutionError, "not found"):
                resolve_secret_ref(p, "missing")

    def test_binary_secret_unsupported(self):
        p = self._aws()
        client = self._client(get_return={"SecretBinary": b"\x00\x01"})
        with mock.patch("boto3.client", return_value=client):
            with self.assertRaisesRegex(SecretResolutionError, "binary"):
                resolve_secret_ref(p, "bin")

    def test_test_connection_ok(self):
        p = self._aws()
        with mock.patch("boto3.client", return_value=self._client()):
            ok, _ = get_backend("aws_sm").test_connection(p)
        self.assertTrue(ok)

    def test_test_connection_access_denied_is_valid(self):
        from botocore.exceptions import ClientError

        p = self._aws()
        client = self._client(
            list_side=ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListSecrets")
        )
        with mock.patch("boto3.client", return_value=client):
            ok, _ = get_backend("aws_sm").test_connection(p)
        self.assertTrue(ok)  # signing succeeded → creds are valid


class InfisicalTests(_AdapterBase):
    def _infis(self):
        return self._provider(
            "infisical",
            {"base_url": "", "project_id": "proj-1", "environment": "prod"},
            {"client_id": "cid", "client_secret": "csec"},
        )

    def test_fetch(self):
        p = self._infis()
        login = _http_json(200, {"accessToken": "tok"})
        secrets = _http_json(200, {"secrets": [{"secretKey": "API_KEY", "secretValue": "v"}]})
        with mock.patch(_INFIS_POST, return_value=login), mock.patch(_INFIS_GET, return_value=secrets):
            self.assertEqual(resolve_secret_ref(p, "/app#API_KEY"), "v")

    def test_root_folder_ref_uses_slash(self):
        p = self._infis()
        login = _http_json(200, {"accessToken": "tok"})
        secrets = _http_json(200, {"secrets": [{"secretKey": "ROOT_KEY", "secretValue": "r"}]})
        with mock.patch(_INFIS_POST, return_value=login), mock.patch(_INFIS_GET, return_value=secrets) as g:
            self.assertEqual(resolve_secret_ref(p, "ROOT_KEY"), "r")
        self.assertEqual(g.call_args.kwargs["params"]["secretPath"], "/")

    def test_login_rejected(self):
        p = self._infis()
        with mock.patch(_INFIS_POST, return_value=_http_json(401)):
            with self.assertRaisesRegex(SecretResolutionError, "rejected"):
                resolve_secret_ref(p, "/app#K")

    def test_test_connection_ok(self):
        p = self._infis()
        with mock.patch(_INFIS_POST, return_value=_http_json(200, {"accessToken": "t"})):
            ok, _ = get_backend("infisical").test_connection(p)
        self.assertTrue(ok)

    def test_test_connection_missing_project(self):
        p = self._provider(
            "infisical",
            {"base_url": "", "project_id": "", "environment": "prod"},
            {"client_id": "c", "client_secret": "s"},
        )
        ok, msg = get_backend("infisical").test_connection(p)
        self.assertFalse(ok)


class DopplerTests(_AdapterBase):
    def _dop(self, token="dp.st.xxx"):
        return self._provider("doppler", {}, {"service_token": token} if token else {})

    def test_fetch(self):
        p = self._dop()
        with mock.patch(_DOPPLER_GET, return_value=_http_json(200, {"API_KEY": "v", "DB": "x"})):
            self.assertEqual(resolve_secret_ref(p, "API_KEY"), "v")

    def test_two_names_one_download(self):
        p = self._dop()
        with mock.patch(_DOPPLER_GET, return_value=_http_json(200, {"A": "1", "B": "2"})) as g:
            self.assertEqual(resolve_secret_ref(p, "A"), "1")
            self.assertEqual(resolve_secret_ref(p, "B"), "2")
        self.assertEqual(g.call_count, 1)  # single config = single download

    def test_missing_token(self):
        p = self._dop(token="")
        with self.assertRaisesRegex(SecretResolutionError, "token"):
            resolve_secret_ref(p, "API_KEY")

    def test_test_connection_ok(self):
        p = self._dop()
        with mock.patch(_DOPPLER_GET, return_value=_http_json(200, {"A": "1"})):
            ok, _ = get_backend("doppler").test_connection(p)
        self.assertTrue(ok)

    def test_test_connection_rejected(self):
        p = self._dop()
        with mock.patch(_DOPPLER_GET, return_value=_http_json(403)):
            ok, _ = get_backend("doppler").test_connection(p)
        self.assertFalse(ok)


class CustomHTTPTests(_AdapterBase):
    def _custom(self, token=""):
        return self._provider(
            "custom", {"url": "https://secrets.internal/v"}, {"token": token} if token else {}
        )

    def test_fetch(self):
        p = self._custom()
        with mock.patch(_CUSTOM_GET, return_value=_http_json(200, {"API_KEY": "v"})):
            self.assertEqual(resolve_secret_ref(p, "API_KEY"), "v")

    def test_bearer_header_sent(self):
        p = self._custom(token="abc")
        with mock.patch(_CUSTOM_GET, return_value=_http_json(200, {"K": "v"})) as g:
            resolve_secret_ref(p, "K")
        self.assertEqual(g.call_args.kwargs["headers"]["Authorization"], "Bearer abc")

    def test_no_token_no_header(self):
        p = self._custom()
        with mock.patch(_CUSTOM_GET, return_value=_http_json(200, {"K": "v"})) as g:
            resolve_secret_ref(p, "K")
        self.assertEqual(g.call_args.kwargs["headers"], {})

    def test_non_object_json_errors(self):
        p = self._custom()
        with mock.patch(_CUSTOM_GET, return_value=_http_json(200, ["not", "an", "object"])):
            with self.assertRaisesRegex(SecretResolutionError, "not an object"):
                resolve_secret_ref(p, "K")

    def test_missing_url(self):
        p = self._provider("custom", {"url": ""}, {})
        with self.assertRaisesRegex(SecretResolutionError, "URL"):
            resolve_secret_ref(p, "K")

    def test_test_connection_ok(self):
        p = self._custom()
        with mock.patch(_CUSTOM_GET, return_value=_http_json(200, {"K": "v"})):
            ok, _ = get_backend("custom").test_connection(p)
        self.assertTrue(ok)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class Stage3FormIntegrationTests(TestCase):
    """The registry-driven form validates every adapter's fields with no per-adapter
    form code — proof the extensibility contract holds through the UI layer."""

    def test_all_adapters_in_type_dropdown(self):
        form = SecretProviderForm()
        keys = [c[0] for c in form.fields["provider_type"].choices]
        self.assertEqual(set(keys), {"vault", "aws_sm", "infisical", "doppler", "custom"})

    def test_doppler_form_valid(self):
        form = SecretProviderForm(
            {
                "provider_type": "doppler",
                "name": "Dop",
                "cache_ttl": "300",
                "on_error": "fail",
                "f_service_token": "dp.st.xyz",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.get_credentials(), {"service_token": "dp.st.xyz"})

    def test_doppler_missing_token_invalid(self):
        form = SecretProviderForm(
            {
                "provider_type": "doppler",
                "name": "Dop",
                "cache_ttl": "300",
                "on_error": "fail",
                "f_service_token": "",
            }
        )
        self.assertFalse(form.is_valid())

    def test_aws_ambient_form_valid_without_credentials(self):
        form = SecretProviderForm(
            {
                "provider_type": "aws_sm",
                "name": "AWS",
                "cache_ttl": "300",
                "on_error": "fail",
                "f_region": "us-east-1",
                "f_access_key_id": "",
                "f_secret_access_key": "",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.config["region"], "us-east-1")
        self.assertEqual(p.get_credentials(), {})


# =========================================================================== #
# Stage 4 — Backup / restore round-trip (format 1.6.0)
# =========================================================================== #
@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class BackupRoundTripTests(_Base):
    """A whole-instance backup carries external secret-provider profiles (creds
    included) and the new Secret fields, and a restore onto a fresh instance
    re-creates the providers and relinks external secrets to them BY NAME (so it
    works even though the provider gets a new UUID on the target). Older archives
    (no ``secret_providers`` key, no ``source`` on secrets) import unchanged."""

    def setUp(self):
        super().setUp()
        from core.services.backup_service import BackupService

        self.backup = BackupService
        self.user = User.objects.create(email="root@example.com", is_superuser=True)
        self.provider = self._provider(name="Prod Vault", token="hvs.t0ken")

        self.local = Secret(key="LOCAL_KEY", source=Secret.Source.LOCAL)
        self.local.set_value("local-plaintext")
        self.local.save()

        self.external = Secret.objects.create(
            key="EXT_KEY",
            source=Secret.Source.EXTERNAL,
            provider=self.provider,
            external_ref="kv/app#EXT_KEY",
        )

    def test_export_carries_providers_and_secret_fields(self):
        data = self.backup.create_backup(include_runs=False)
        # Secret providers joined the format at 1.6.0 — assert that FLOOR, not the
        # exact current version: this test's subject is the provider payload, and
        # a later additive bump shouldn't break it.
        version = tuple(int(p) for p in data["backup_metadata"]["version"].split("."))
        self.assertGreaterEqual(version, (1, 6, 0))

        self.assertEqual(len(data["secret_providers"]), 1)
        prov = data["secret_providers"][0]
        self.assertEqual(prov["name"], "Prod Vault")
        self.assertEqual(prov["provider_type"], "vault")
        # Credentials are carried verbatim (the encrypted blob, not plaintext).
        self.assertEqual(prov["credentials_encrypted"], self.provider.credentials_encrypted)
        self.assertNotIn("hvs.t0ken", json.dumps(prov))

        by_key = {s["key"]: s for s in data["secrets"]}
        self.assertEqual(by_key["EXT_KEY"]["source"], "external")
        self.assertEqual(by_key["EXT_KEY"]["provider_name"], "Prod Vault")  # by NAME
        self.assertEqual(by_key["EXT_KEY"]["external_ref"], "kv/app#EXT_KEY")
        self.assertEqual(by_key["EXT_KEY"]["encrypted_value"], "")  # external stores no value
        self.assertEqual(by_key["LOCAL_KEY"]["source"], "local")
        self.assertEqual(by_key["LOCAL_KEY"]["provider_name"], None)

    def test_round_trip_restores_providers_and_relinks_by_name(self):
        data = self.backup.create_backup(include_runs=False)
        old_provider_id = self.provider.id

        # Simulate a fresh target: drop the secrets (external first for PROTECT)
        # then the provider, so the restore must recreate the provider with a NEW
        # id and relink the external secret to it by name — not by the stale id.
        Secret.objects.all().delete()
        SecretProvider.objects.all().delete()

        result = self.backup.restore_backup(
            data, restore_runs=False, current_user=self.user
        )
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(result["counts"]["secret_providers"], 1)
        self.assertEqual(result["warnings"], [])

        provider = SecretProvider.objects.get(name="Prod Vault")
        self.assertNotEqual(provider.id, old_provider_id)  # relink can't rely on id
        self.assertEqual(provider.provider_type, "vault")
        self.assertEqual(provider.config, self.provider.config)
        self.assertEqual(provider.cache_ttl, 300)
        # Credentials survived the round-trip intact (decrypt to the original).
        self.assertEqual(provider.get_credentials(), {"token": "hvs.t0ken"})

        external = Secret.objects.get(key="EXT_KEY")
        self.assertEqual(external.source, Secret.Source.EXTERNAL)
        self.assertEqual(external.provider_id, provider.id)  # relinked by name
        self.assertEqual(external.external_ref, "kv/app#EXT_KEY")
        self.assertEqual(external.encrypted_value, "")

        local = Secret.objects.get(key="LOCAL_KEY")
        self.assertEqual(local.source, Secret.Source.LOCAL)
        self.assertEqual(local.get_decrypted_value(), "local-plaintext")

    def test_import_upserts_existing_provider_by_name(self):
        # An existing provider of the same name is updated in place (not
        # duplicated), and its credentials are refreshed from the backup.
        data = self.backup.create_backup(include_runs=False)
        Secret.objects.all().delete()
        # Leave the provider in place but mutate it — restore should overwrite it.
        self.provider.config = {"base_url": "https://STALE", "mount": "secret", "namespace": ""}
        self.provider.save()

        result = self.backup.restore_backup(
            data, restore_runs=False, current_user=self.user
        )
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(SecretProvider.objects.filter(name="Prod Vault").count(), 1)
        provider = SecretProvider.objects.get(name="Prod Vault")
        self.assertEqual(provider.config["base_url"], "https://vault.example.com:8200")

    def test_missing_provider_imports_external_secret_unlinked_with_warning(self):
        # A hand-edited / inconsistent archive: the external secret references a
        # provider the archive doesn't carry. Fail-closed: the row is imported
        # UNLINKED (surfaces for the user to re-point) with a warning, never a
        # silent downgrade to an empty local secret.
        data = self.backup.create_backup(include_runs=False)
        data["secret_providers"] = []
        Secret.objects.all().delete()
        SecretProvider.objects.all().delete()

        result = self.backup.restore_backup(
            data, restore_runs=False, current_user=self.user
        )
        self.assertTrue(result["success"], result.get("errors"))
        external = Secret.objects.get(key="EXT_KEY")
        self.assertEqual(external.source, Secret.Source.EXTERNAL)
        self.assertIsNone(external.provider_id)
        self.assertTrue(
            any("EXT_KEY" in w and "not found" in w for w in result["warnings"]),
            result["warnings"],
        )

    def test_pre_1_6_0_archive_defaults_secrets_to_local(self):
        # A pre-feature instance had only local secrets and no providers. Its
        # archive omits the secret_providers key and the per-secret source field.
        self.external.delete()
        self.provider.delete()
        data = self.backup.create_backup(include_runs=False)
        data.pop("secret_providers", None)
        for s in data["secrets"]:
            s.pop("source", None)
            s.pop("provider_name", None)
            s.pop("external_ref", None)
        data["backup_metadata"]["version"] = "1.5.0"
        Secret.objects.all().delete()

        result = self.backup.restore_backup(
            data, restore_runs=False, current_user=self.user
        )
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(SecretProvider.objects.count(), 0)
        local = Secret.objects.get(key="LOCAL_KEY")
        self.assertEqual(local.source, Secret.Source.LOCAL)
        self.assertEqual(local.get_decrypted_value(), "local-plaintext")
