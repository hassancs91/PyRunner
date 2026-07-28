"""
Tests for the generic AI provider system (AIProvider + ClaudeService).

The load-bearing suite here is Anthropic env parity: an instance configured
with an Anthropic provider must inject exactly the env the old single-provider
code injected (plus the additive PYRUNNER_AI_PROVIDER attribution var).
"""

import json
import sys
import uuid
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings as django_settings
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from core.executor import execute_run
from core.forms import AIProviderForm, AISettingsForm
from core.models import (
    AIProvider,
    ClaudeUsage,
    Environment,
    GlobalSettings,
    PROVIDER_PRESETS,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.script_helpers import pyrunner_ai
from core.services.claude_service import ClaudeService
from core.services.encryption_service import EncryptionService

_TEST_KEY = Fernet.generate_key().decode()


def _make_provider(ptype, name=None, credential="", **kwargs):
    if credential:
        kwargs["credential_encrypted"] = EncryptionService.encrypt(credential)
    return AIProvider.objects.create(
        provider_type=ptype,
        name=name or f"{ptype}-{uuid.uuid4().hex[:6]}",
        **kwargs,
    )


def _activate(provider, enabled=True):
    s = GlobalSettings.get_settings()
    s.claude_enabled = enabled
    s.active_ai_provider = provider
    s.save()
    return s


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ScriptEnvTests(TestCase):
    """get_script_env per provider type — Anthropic parity is the contract."""

    def test_anthropic_api_key_env_matches_legacy_output(self):
        p = _make_provider(
            "anthropic",
            credential="sk-ant-test",
            auth_method=AIProvider.AuthMethod.API_KEY,
            default_model="claude-sonnet-4-6",
        )
        _activate(p)
        env = ClaudeService.get_script_env()
        # Exactly the legacy vars…
        legacy = {
            "CLAUDE_CONFIG_DIR": str(django_settings.CLAUDE_CONFIG_DIR),
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        }
        for key, value in legacy.items():
            self.assertEqual(env[key], value)
        # …plus ONLY the additive attribution var. No BASE_URL/AUTH_TOKEN leak.
        self.assertEqual(set(env) - set(legacy), {"PYRUNNER_AI_PROVIDER"})
        self.assertEqual(env["PYRUNNER_AI_PROVIDER"], "anthropic")

    def test_anthropic_subscription_env_matches_legacy_output(self):
        p = _make_provider(
            "anthropic",
            credential="sk-ant-oat01-test",
            auth_method=AIProvider.AuthMethod.SUBSCRIPTION,
        )
        _activate(p)
        env = ClaudeService.get_script_env()
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-test")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_MODEL", env)  # no default model set

    def test_zai_env_routes_via_base_url_with_blank_api_key(self):
        p = _make_provider("zai", credential="zai-key", default_model="glm-5.2")
        _activate(p)
        env = ClaudeService.get_script_env()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "zai-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")  # explicitly empty
        self.assertEqual(env["ANTHROPIC_MODEL"], "glm-5.2")
        self.assertEqual(env["API_TIMEOUT_MS"], "3000000")  # zai preset extra
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["PYRUNNER_AI_PROVIDER"], "zai")

    def test_openrouter_env_has_no_extra_vars(self):
        p = _make_provider("openrouter", credential="or-key")
        _activate(p)
        env = ClaudeService.get_script_env()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        self.assertNotIn("API_TIMEOUT_MS", env)

    def test_ollama_without_credential_uses_default_token(self):
        p = _make_provider("ollama")  # no credential on purpose
        _activate(p)
        env = ClaudeService.get_script_env()
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "ollama")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://localhost:11434")

    def test_custom_uses_row_base_url(self):
        p = _make_provider("custom", credential="k", base_url="http://litellm:4000")
        _activate(p)
        self.assertEqual(
            ClaudeService.get_script_env()["ANTHROPIC_BASE_URL"], "http://litellm:4000"
        )

    def test_blank_base_url_falls_back_to_preset(self):
        p = _make_provider("zai", credential="k", base_url="")
        _activate(p)
        self.assertEqual(
            ClaudeService.get_script_env()["ANTHROPIC_BASE_URL"],
            PROVIDER_PRESETS["zai"]["base_url"],
        )

    def test_disabled_returns_empty(self):
        p = _make_provider("anthropic", credential="k")
        _activate(p, enabled=False)
        self.assertEqual(ClaudeService.get_script_env(), {})

    def test_no_active_provider_returns_empty(self):
        _activate(None)
        self.assertEqual(ClaudeService.get_script_env(), {})

    def test_missing_required_credential_returns_empty(self):
        p = _make_provider("zai")  # credential required but absent
        _activate(p)
        self.assertEqual(ClaudeService.get_script_env(), {})


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ConflictingKeysTests(TestCase):
    def test_anthropic_api_key_strips_oauth_and_routing(self):
        _activate(_make_provider("anthropic", credential="k", auth_method="api_key"))
        self.assertEqual(
            ClaudeService.conflicting_env_keys(),
            ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
        )

    def test_anthropic_subscription_strips_api_key_and_routing(self):
        _activate(_make_provider("anthropic", credential="k", auth_method="subscription"))
        self.assertEqual(
            ClaudeService.conflicting_env_keys(),
            ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
        )

    def test_third_party_strips_oauth_token(self):
        _activate(_make_provider("zai", credential="k"))
        self.assertEqual(ClaudeService.conflicting_env_keys(), ["CLAUDE_CODE_OAUTH_TOKEN"])

    def test_no_provider_strips_everything(self):
        _activate(None)
        self.assertEqual(len(ClaudeService.conflicting_env_keys()), 4)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class IsConfiguredTests(TestCase):
    def test_none_active(self):
        _activate(None)
        self.assertFalse(ClaudeService.is_configured())

    def test_anthropic_without_credential(self):
        _activate(_make_provider("anthropic"))
        self.assertFalse(ClaudeService.is_configured())

    def test_ollama_without_credential_is_configured(self):
        _activate(_make_provider("ollama"))
        self.assertTrue(ClaudeService.is_configured())

    def test_with_credential(self):
        _activate(_make_provider("zai", credential="k"))
        self.assertTrue(ClaudeService.is_configured())


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ConnectionTestTests(TestCase):
    """Provider-aware test dispatch (SDK calls mocked)."""

    _usage = {"model": "m", "input_tokens": 5, "output_tokens": 2,
              "cache_creation_tokens": 0, "cache_read_tokens": 0,
              "num_turns": 1, "duration_ms": 100, "cost_usd": None}

    def test_anthropic_uses_web_search_test(self):
        with mock.patch.object(ClaudeService, "cli_available", return_value=True), \
                mock.patch.object(ClaudeService, "_run_test_query",
                                  return_value=("Python 3.14", ["WebSearch"], self._usage)) as q, \
                mock.patch.object(ClaudeService, "_run_ping_test") as ping:
            ok, msg = ClaudeService.test_connection_with_credentials(
                "anthropic", "sk-ant-x", auth_method="api_key"
            )
        self.assertTrue(ok)
        self.assertIn("Python 3.14", msg)
        q.assert_called_once()
        ping.assert_not_called()

    def test_third_party_uses_ping_tool_test(self):
        from core.services.claude_service import _PING_TOOL

        with mock.patch.object(ClaudeService, "cli_available", return_value=True), \
                mock.patch.object(ClaudeService, "_run_ping_test",
                                  return_value=("PYRUNNER-PONG", [_PING_TOOL], self._usage)) as ping:
            ok, msg = ClaudeService.test_connection_with_credentials(
                "zai", "zai-key", base_url="https://api.z.ai/api/anthropic"
            )
        self.assertTrue(ok)
        ping.assert_called_once()
        # env passed to the ping test must route at the provider
        env = ping.call_args[0][0]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    def test_third_party_fails_when_tool_not_called(self):
        with mock.patch.object(ClaudeService, "cli_available", return_value=True), \
                mock.patch.object(ClaudeService, "_run_ping_test",
                                  return_value=("hello!", [], self._usage)):
            ok, msg = ClaudeService.test_connection_with_credentials(
                "openrouter", "k", base_url="https://openrouter.ai/api"
            )
        self.assertFalse(ok)
        self.assertIn("never called the test tool", msg)

    def test_third_party_requires_base_url(self):
        ok, msg = ClaudeService.test_connection_with_credentials("custom", "k")
        self.assertFalse(ok)
        self.assertIn("endpoint URL", msg)

    def test_test_usage_row_stamps_provider(self):
        ClaudeService._record_test_usage(dict(self._usage), "zai")
        row = ClaudeUsage.objects.get()
        self.assertEqual(row.provider, "zai")
        self.assertEqual(row.source, ClaudeUsage.Source.TEST)

    def test_test_provider_stamps_last_tested(self):
        p = _make_provider("ollama")
        with mock.patch.object(
            ClaudeService, "test_connection_with_credentials", return_value=(True, "ok")
        ):
            ok, _ = ClaudeService.test_provider(p)
        self.assertTrue(ok)
        p.refresh_from_db()
        self.assertIsNotNone(p.last_tested_at)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ProviderFormTests(TestCase):
    def _data(self, **over):
        data = {
            "provider_type": "zai",
            "name": "My GLM",
            "base_url": "",
            "auth_method": "",
            "credential": "zai-key",
            "default_model": "glm-5.2",
        }
        data.update(over)
        return data

    def test_create_prefills_base_url_and_forces_api_key(self):
        form = AIProviderForm(self._data())
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.base_url, PROVIDER_PRESETS["zai"]["base_url"])
        self.assertEqual(p.auth_method, AIProvider.AuthMethod.API_KEY)
        self.assertEqual(EncryptionService.decrypt(p.credential_encrypted), "zai-key")

    def test_credential_required_on_create(self):
        form = AIProviderForm(self._data(credential=""))
        self.assertFalse(form.is_valid())
        self.assertIn("credential", form.errors)

    def test_ollama_credential_optional(self):
        form = AIProviderForm(self._data(provider_type="ollama", credential=""))
        self.assertTrue(form.is_valid(), form.errors)

    def test_custom_requires_base_url(self):
        form = AIProviderForm(self._data(provider_type="custom", base_url=""))
        self.assertFalse(form.is_valid())
        self.assertIn("base_url", form.errors)

    def test_edit_blank_credential_keeps_saved_one(self):
        p = _make_provider("zai", name="My GLM", credential="original")
        form = AIProviderForm(self._data(credential=""), instance=p)
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(EncryptionService.decrypt(p.credential_encrypted), "original")

    def test_duplicate_name_rejected(self):
        _make_provider("zai", name="My GLM", credential="k")
        form = AIProviderForm(self._data())
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_anthropic_defaults_to_subscription(self):
        form = AIProviderForm(
            self._data(provider_type="anthropic", name="Claude", auth_method="")
        )
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.auth_method, AIProvider.AuthMethod.SUBSCRIPTION)
        self.assertEqual(p.base_url, "")


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class SettingsFormAndViewTests(TestCase):
    def setUp(self):
        # Get past SetupWizardMiddleware: setup complete + a default environment.
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

    def test_settings_form_sets_active_provider(self):
        p = _make_provider("zai", credential="k")
        form = AISettingsForm({"claude_enabled": "on", "active_provider": str(p.id)})
        self.assertTrue(form.is_valid(), form.errors)
        s = form.save(GlobalSettings.get_settings())
        self.assertTrue(s.claude_enabled)
        self.assertEqual(s.active_ai_provider_id, p.id)

    def test_settings_view_saves(self):
        p = _make_provider("openrouter", credential="k")
        resp = self.client.post(
            reverse("cpanel:claude_settings"),
            {"claude_enabled": "on", "active_provider": str(p.id)},
        )
        self.assertEqual(resp.status_code, 302)
        s = GlobalSettings.get_settings()
        self.assertTrue(s.claude_enabled)
        self.assertEqual(s.active_ai_provider_id, p.id)

    def test_test_connection_view_unknown_provider(self):
        resp = self.client.post(
            reverse("cpanel:claude_test_connection"),
            data=json.dumps({"provider_id": str(uuid.uuid4())}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["success"])

    def test_internal_usage_endpoint_stores_provider(self):
        from core.services.datastore_token import mint_datastore_token

        token = mint_datastore_token(uuid.uuid4())
        resp = self.client.post(
            reverse("internal:claude_usage"),
            data=json.dumps({"provider": "zai", "model": "glm-5.2", "input_tokens": 3}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ClaudeUsage.objects.get().provider, "zai")


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ProviderCrudViewTests(TestCase):
    def setUp(self):
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

    def _save(self, **over):
        data = {
            "provider_id": "",
            "provider_type": "zai",
            "name": "My GLM",
            "base_url": "",
            "auth_method": "",
            "credential": "zai-key",
            "default_model": "glm-5.2",
        }
        data.update(over)
        return self.client.post(reverse("cpanel:ai_provider_save"), data)

    def test_save_creates_and_first_provider_becomes_active(self):
        resp = self._save()
        self.assertEqual(resp.status_code, 302)
        p = AIProvider.objects.get()
        self.assertEqual(p.name, "My GLM")
        self.assertEqual(GlobalSettings.get_settings().active_ai_provider_id, p.id)

    def test_save_second_provider_does_not_steal_active(self):
        self._save()
        first = AIProvider.objects.get()
        self._save(name="Backup", provider_type="openrouter", credential="or-key")
        self.assertEqual(AIProvider.objects.count(), 2)
        self.assertEqual(GlobalSettings.get_settings().active_ai_provider_id, first.id)

    def test_save_edits_existing(self):
        self._save()
        p = AIProvider.objects.get()
        self._save(provider_id=str(p.id), name="Renamed", credential="")
        p.refresh_from_db()
        self.assertEqual(p.name, "Renamed")
        self.assertEqual(EncryptionService.decrypt(p.credential_encrypted), "zai-key")

    def test_invalid_form_creates_nothing(self):
        self._save(credential="", name="No cred")
        self.assertEqual(AIProvider.objects.count(), 0)

    def test_activate_switches(self):
        self._save()
        self._save(name="Backup", provider_type="openrouter", credential="k2")
        backup = AIProvider.objects.get(name="Backup")
        resp = self.client.post(
            reverse("cpanel:ai_provider_activate", kwargs={"provider_id": backup.id})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            GlobalSettings.get_settings().active_ai_provider_id, backup.id
        )

    def test_delete_active_clears_active(self):
        self._save()
        p = AIProvider.objects.get()
        resp = self.client.post(
            reverse("cpanel:ai_provider_delete", kwargs={"provider_id": p.id})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIProvider.objects.count(), 0)
        self.assertIsNone(GlobalSettings.get_settings().active_ai_provider_id)

    def test_non_superuser_blocked(self):
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        pid = uuid.uuid4()
        for url in (
            reverse("cpanel:ai_provider_save"),
            reverse("cpanel:ai_provider_delete", kwargs={"provider_id": pid}),
            reverse("cpanel:ai_provider_activate", kwargs={"provider_id": pid}),
        ):
            resp = self.client.post(url, {})
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse("auth:login"), resp["Location"])
        self.assertEqual(AIProvider.objects.count(), 0)

    def test_services_page_renders_with_providers(self):
        self._save()
        _activate(AIProvider.objects.get())
        resp = self.client.get(reverse("cpanel:services"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("AI Provider", content)
        self.assertIn("My GLM", content)
        self.assertIn("ai-providers-data", content)
        self.assertNotIn("zai-key", content)  # credential never leaks into the page

    def test_usage_page_renders_with_provider_rows(self):
        ClaudeUsage.objects.create(
            source=ClaudeUsage.Source.SCRIPT, provider="zai", model="glm-5.2",
            input_tokens=10, output_tokens=5,
        )
        resp = self.client.get(reverse("cpanel:claude_usage"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("glm-5.2", resp.content.decode())


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class DataMigrationTests(TransactionTestCase):
    """Roll back to 0042, seed old-style claude_* fields, migrate forward."""

    def setUp(self):
        # Whatever happens (including a failed assertion mid-test), put the schema
        # back at head: this is a TransactionTestCase, so a DB left on an old
        # migration state would leak into every test that follows.
        self.addCleanup(self._migrate_to_head)

    def _executor(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor

    def _migrate_to_head(self):
        """Migrate core fully forward.

        Derived from the graph rather than hardcoded, so adding a migration never
        silently strands this test on an old schema.
        """
        executor = self._executor()
        executor.migrate(
            [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        )

    def test_forward_seeds_providers_and_active(self):
        executor = self._executor()
        executor.migrate([("core", "0042_cache_table")])
        old_apps = executor.loader.project_state([("core", "0042_cache_table")]).apps
        OldSettings = old_apps.get_model("core", "GlobalSettings")
        OldSettings.objects.update_or_create(
            pk=1,
            defaults={
                "claude_enabled": True,
                "claude_auth_method": "api_key",
                "claude_oauth_token_encrypted": "enc-oauth-blob",
                "claude_api_key_encrypted": "enc-key-blob",
                "claude_default_model": "claude-sonnet-4-6",
            },
        )

        executor = self._executor()
        executor.migrate([("core", "0043_ai_providers")])
        # The assertions below use the CURRENT models, so the schema has to be
        # current too — 0043's data migration has already run by this point, and
        # everything after it is additive.
        self._migrate_to_head()

        providers = {p.auth_method: p for p in AIProvider.objects.all()}
        self.assertEqual(len(providers), 2)
        self.assertEqual(providers["subscription"].credential_encrypted, "enc-oauth-blob")
        self.assertEqual(providers["api_key"].credential_encrypted, "enc-key-blob")
        self.assertEqual(providers["api_key"].default_model, "claude-sonnet-4-6")

        s = GlobalSettings.get_settings()
        # active follows the old auth_method selection; toggle preserved
        self.assertEqual(s.active_ai_provider_id, providers["api_key"].id)
        self.assertTrue(s.claude_enabled)

    def test_forward_with_no_credentials_creates_nothing(self):
        executor = self._executor()
        executor.migrate([("core", "0042_cache_table")])
        old_apps = executor.loader.project_state([("core", "0042_cache_table")]).apps
        OldSettings = old_apps.get_model("core", "GlobalSettings")
        OldSettings.objects.update_or_create(pk=1, defaults={"claude_enabled": False})

        executor = self._executor()
        executor.migrate([("core", "0043_ai_providers")])
        self._migrate_to_head()

        self.assertEqual(AIProvider.objects.count(), 0)
        self.assertIsNone(GlobalSettings.get_settings().active_ai_provider_id)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class HelperCredentialGateTests(TestCase):
    """pyrunner_ai's credential gate must accept every env shape ClaudeService
    injects — regression for review 7.1, where third-party providers were
    rejected because ANTHROPIC_AUTH_TOKEN was not recognized."""

    def _gate_under_injected_env(self):
        env = ClaudeService.get_script_env()
        self.assertTrue(env, "sanity: the active provider should inject an env")
        with mock.patch.dict("os.environ", env, clear=True):
            return pyrunner_ai._has_credentials()

    def test_gate_accepts_every_provider_type(self):
        cases = [
            ("anthropic", {"credential": "sk-ant-test", "auth_method": "api_key"}),
            ("anthropic", {"credential": "sk-ant-oat01-t", "auth_method": "subscription"}),
            ("zai", {"credential": "zai-key"}),
            ("openrouter", {"credential": "or-key"}),
            ("ollama", {}),  # rides the preset's default token
            ("custom", {"credential": "k", "base_url": "http://litellm:4000"}),
        ]
        for ptype, kwargs in cases:
            with self.subTest(provider=ptype, auth=kwargs.get("auth_method", "")):
                AIProvider.objects.all().delete()
                _activate(_make_provider(ptype, **kwargs))
                self.assertTrue(self._gate_under_injected_env())

    def test_gate_rejects_empty_environment(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(pyrunner_ai._has_credentials())

    def test_gate_rejects_blanked_api_key(self):
        # Third-party envs blank ANTHROPIC_API_KEY to "" — the empty string
        # alone must not satisfy the gate.
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=True):
            self.assertFalse(pyrunner_ai._has_credentials())


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ThirdPartyCredentialMaskingTests(TestCase):
    """A third-party provider's credential must be masked in run output like
    the Anthropic ones — regression for review 11.3, where
    ANTHROPIC_AUTH_TOKEN was missing from the executor's masking set."""

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_auth_token_masked_in_run_output(self, _val):
        _activate(_make_provider("zai", credential="zai-secret-token-12"))
        env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:10]}")
        script = Script.objects.create(
            name="s",
            code="import os; print(os.environ['ANTHROPIC_AUTH_TOKEN'])",
            environment=env,
            timeout_seconds=60,
        )
        run = Run.objects.create(script=script, status=Run.Status.PENDING)
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertNotIn("zai-secret-token-12", run.stdout)
        self.assertIn("[ANTHROPIC_AUTH_TOKEN:MASKED]", run.stdout)
