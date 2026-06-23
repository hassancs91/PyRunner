"""
Tests for the SDK Showcase plugin.

Run in-tree with the normal Django test runner (so ``core.plugins.api`` is real),
imported into the suite by ``core/test_sdk_showcase_plugin.py`` — the same splice
shim pattern as ``examples/qdrant_backup/tests.py``. Coverage: idempotent
provisioning through the SDK, the counter read/write, the worker secret/config
contract, and form validation.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from . import provisioning as prov
from .forms import SECRET_FIELDS, SetupForm


# --------------------------------------------------------------------------- #
# Worker contract — secret/config names are wired to the standalone worker by
# convention; _worker_code() must fail loudly if they drift.
# --------------------------------------------------------------------------- #

class WorkerContractTests(SimpleTestCase):
    def test_worker_references_secret_and_config_keys(self):
        code = prov._worker_code()
        for token in list(SECRET_FIELDS.values()) + list(prov.CONFIG_KEYS):
            self.assertIn(token, code, f"worker_body.py missing reference to {token}")

    def test_drift_raises_loudly(self):
        with mock.patch.object(prov, "CONFIG_KEYS", prov.CONFIG_KEYS + ("zzz_unreferenced",)):
            with self.assertRaises(ValueError) as cm:
                prov._worker_code()
        self.assertIn("zzz_unreferenced", str(cm.exception))


# --------------------------------------------------------------------------- #
# Setup form.
# --------------------------------------------------------------------------- #

class SetupFormTests(SimpleTestCase):
    ENVS = [SimpleNamespace(name="prod")]

    def _form(self, *, has_secret=False, **over):
        data = {"environment": "prod", "demo_token": "x", "message": "hi", "steps": "5"}
        data.update(over)
        return SetupForm(data, environments=self.ENVS, has_secret=has_secret)

    def test_valid(self):
        self.assertTrue(self._form().is_valid())

    def test_environment_required_when_available(self):
        self.assertFalse(self._form(environment="").is_valid())

    def test_steps_bounds_enforced(self):
        self.assertFalse(self._form(steps="0").is_valid())
        self.assertFalse(self._form(steps="99").is_valid())

    def test_token_optional(self):
        # The demo secret is always optional (blank keeps any existing value).
        self.assertTrue(self._form(has_secret=True, demo_token="").is_valid())
        self.assertTrue(self._form(demo_token="").is_valid())


# --------------------------------------------------------------------------- #
# Provisioning — one setup creates exactly the declared resources, idempotently.
# --------------------------------------------------------------------------- #

class ProvisionTests(TestCase):
    def setUp(self):
        from core.models import Environment, Workspace

        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="prod", path="scenv", requirements="")
        patch = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        patch.start()
        self.addCleanup(patch.stop)

    def _data(self, **over):
        data = {"environment": "prod", "demo_token": "tok", "message": "hi", "steps": 5}
        data.update(over)
        return data

    def _counts(self):
        from core.models import DataStore, Script, ScriptSchedule, Secret, SecretGrant

        script = Script.objects.get(owner_plugin=prov.OWNER, owner_key=prov.SCRIPT_KEY)
        return {
            "scripts": Script.objects.filter(owner_plugin=prov.OWNER).count(),
            "secrets": Secret.objects.filter(owner_plugin=prov.OWNER).count(),
            "stores": DataStore.objects.filter(name=f"{prov.OWNER}:{prov.STORE_KEY}").count(),
            "grants": SecretGrant.objects.filter(script=script).count(),
            "schedules": ScriptSchedule.objects.filter(script=script).count(),
        }

    def test_provision_creates_all_declared_resources(self):
        from core.models import Script

        script, warnings = prov.provision(self._data())
        self.assertEqual(warnings, [])
        self.assertEqual(script.name, prov.SCRIPT_NAME)
        self.assertEqual(script.injection_mode, Script.InjectionMode.SELECTED)
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        self.assertEqual(prov.get_config()["message"], "hi")
        self.assertEqual(prov.get_counter(), 0)
        self.assertEqual(prov.owned_inventory(), {"datastores": 1, "secrets": 1, "scripts": 1, "schedules": 1})

    def test_provision_is_idempotent(self):
        prov.provision(self._data())
        prov.provision(self._data(steps=9, message="changed"))
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        self.assertEqual(prov.get_config()["steps"], 9)

    def test_blank_token_keeps_existing(self):
        from core.models import Secret

        prov.provision(self._data())
        prov.provision(self._data(demo_token=""))
        secret = Secret.objects.get(owner_plugin=prov.OWNER, owner_key="DEMO_TOKEN")
        self.assertEqual(secret.get_decrypted_value(), "tok")
        self.assertEqual(Secret.objects.filter(owner_plugin=prov.OWNER).count(), 1)

    def test_counter_increment_and_reset(self):
        prov.provision(self._data())
        self.assertEqual(prov.increment_counter(), 1)
        self.assertEqual(prov.increment_counter(), 2)
        self.assertEqual(prov.get_counter(), 2)
        prov.reset_demo_data()
        self.assertEqual(prov.get_counter(), 0)

    def test_sync_schedule_switches_mode(self):
        prov.provision(self._data())
        self.assertTrue(prov.sync_schedule("interval"))
        sched = prov.get_schedule()
        self.assertEqual(sched.run_mode, "interval")
        self.assertEqual(sched.interval_minutes, 60)

    def test_unknown_environment_raises(self):
        with self.assertRaises(ValueError):
            prov.provision(self._data(environment="ghost"))
