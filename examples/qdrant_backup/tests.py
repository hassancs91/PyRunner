"""
Tests for the Qdrant Backup plugin.

These run inside the PyRunner repo with the normal Django test runner — the
plugin is developed in-tree, so ``core.plugins.api`` is importable and the SDK is
exercised for real (no fakes). They are imported into the main suite by the thin
shim ``core/test_qdrant_backup_plugin.py``, which splices ``examples/`` onto the
``plugins`` package path (exactly as Dev Mode does) so this module loads as
``plugins.qdrant_backup.tests`` and the relative imports below resolve.

Coverage focuses on the web/provisioning surface, where a bug is silent and
costly — the security boundaries (SSRF guard, download-key resolution), the
cross-process worker contract, form validation, and idempotent provisioning. The
worker body itself reads secret env vars at import time and talks to R2/Qdrant,
so it is verified end-to-end by real runs, not unit-imported here.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from . import provisioning as prov
from .forms import SECRET_FIELDS, QdrantBackupConfigForm


# --------------------------------------------------------------------------- #
# SSRF guard — Test-connection endpoints must reject the dangerous targets and
# allow the legitimate ones (including self-hosted Qdrant on a private network).
# --------------------------------------------------------------------------- #

class EndpointGuardTests(SimpleTestCase):
    def _ok(self, url):
        return prov._endpoint_allowed(url)[0]

    def test_allows_public_hostname_and_ip(self):
        self.assertTrue(self._ok("https://qdrant.example.com"))
        self.assertTrue(self._ok("http://qdrant.example.com:6333"))
        self.assertTrue(self._ok("https://8.8.8.8"))

    def test_allows_private_rfc1918_for_self_hosted(self):
        # A self-hosted Qdrant on an internal network is a legitimate target.
        self.assertTrue(self._ok("http://10.0.0.5:6333"))
        self.assertTrue(self._ok("http://192.168.1.10"))

    def test_blocks_cloud_metadata_and_link_local(self):
        # 169.254.169.254 is the cloud metadata endpoint — the classic SSRF prize.
        self.assertFalse(self._ok("http://169.254.169.254/latest/meta-data/"))

    def test_blocks_multicast_reserved_unspecified(self):
        self.assertFalse(self._ok("http://224.0.0.1"))
        self.assertFalse(self._ok("http://240.0.0.1"))
        self.assertFalse(self._ok("http://0.0.0.0"))

    def test_blocks_non_http_schemes_and_missing_host(self):
        self.assertFalse(self._ok("ftp://example.com"))
        self.assertFalse(self._ok("file:///etc/passwd"))
        self.assertFalse(self._ok("not a url"))
        self.assertFalse(self._ok(""))


# --------------------------------------------------------------------------- #
# Download-key resolution — THE security invariant: only an object recorded in
# the run history can ever be signed; an arbitrary/caller-supplied key never is.
# --------------------------------------------------------------------------- #

class ResolveDownloadKeyTests(SimpleTestCase):
    RUNS = [
        {
            "date": "2026-06-20",
            "zip_key": "qdrant-backups/2026-06-20/backup-2026-06-20.zip",
            "collections": [
                {"collection": "products", "s3_key": "qdrant-backups/2026-06-20/products.snapshot", "status": "ok"},
            ],
        },
        {
            "date": "2026-06-21",
            "collections": [
                {"collection": "products", "s3_key": "FAILED", "status": "failed"},
                {"collection": "support_kb", "s3_key": "qdrant-backups/2026-06-21/support_kb.snapshot", "status": "ok"},
            ],
        },
    ]

    def _resolve(self, *a, runs=None, **kw):
        with mock.patch.object(prov, "get_runs", return_value=runs if runs is not None else self.RUNS):
            return prov.resolve_download_key(*a, **kw)

    def test_returns_recorded_collection_key(self):
        self.assertEqual(
            self._resolve("2026-06-20", collection="products"),
            "qdrant-backups/2026-06-20/products.snapshot",
        )

    def test_returns_recorded_zip_key(self):
        self.assertEqual(
            self._resolve("2026-06-20", want_zip=True),
            "qdrant-backups/2026-06-20/backup-2026-06-20.zip",
        )

    def test_failed_collection_is_not_linkable(self):
        self.assertIsNone(self._resolve("2026-06-21", collection="products"))

    def test_missing_zip_returns_none(self):
        self.assertIsNone(self._resolve("2026-06-21", want_zip=True))

    def test_unknown_date_collection_or_blank_returns_none(self):
        # The crux: nothing outside the recorded history can be signed.
        self.assertIsNone(self._resolve("1999-01-01", collection="products"))
        self.assertIsNone(self._resolve("2026-06-20", collection="does-not-exist"))
        self.assertIsNone(self._resolve("", collection="products"))

    def test_newest_run_wins_for_a_duplicated_date(self):
        runs = [
            {"date": "2026-06-22", "collections": [{"collection": "a", "s3_key": "OLD", "status": "ok"}]},
            {"date": "2026-06-22", "collections": [{"collection": "a", "s3_key": "NEW", "status": "ok"}]},
        ]
        self.assertEqual(self._resolve("2026-06-22", collection="a", runs=runs), "NEW")

    def test_tolerates_garbage_rows_and_ts_date_fallback(self):
        runs = [
            "not-a-dict",
            {"ts": "2026-06-23 01:02:03", "collections": [{"collection": "a", "s3_key": "K", "status": "ok"}]},
        ]
        self.assertEqual(self._resolve("2026-06-23", collection="a", runs=runs), "K")


# --------------------------------------------------------------------------- #
# Cross-process worker contract — the secret env names + config keys are wired
# to the standalone worker_body by convention; _worker_code() must fail loudly
# at Save if they drift, never ship a silently misconfigured backup.
# --------------------------------------------------------------------------- #

class WorkerContractTests(SimpleTestCase):
    def test_shipped_worker_references_every_secret_and_config_key(self):
        code = prov._worker_code()
        for token in list(SECRET_FIELDS.values()) + list(prov.CONFIG_KEYS):
            self.assertIn(token, code, f"worker_body.py is missing reference to {token}")

    def test_drift_raises_loudly(self):
        with mock.patch.object(prov, "CONFIG_KEYS", prov.CONFIG_KEYS + ("zzz_unreferenced_token",)):
            with self.assertRaises(ValueError) as cm:
                prov._worker_code()
        self.assertIn("zzz_unreferenced_token", str(cm.exception))


# --------------------------------------------------------------------------- #
# Config form — schedule validation rules, normalizers, and the first-setup vs.
# already-configured credential requirement.
# --------------------------------------------------------------------------- #

class ConfigFormTests(SimpleTestCase):
    ENVS = [SimpleNamespace(name="prod")]

    def _form(self, *, configured=frozenset(), **over):
        data = {
            "qdrant_url": "https://qdrant.example.com",
            "r2_endpoint_url": "https://acc.r2.cloudflarestorage.com",
            "r2_bucket_name": "backups",
            "qdrant_api_key": "k", "r2_access_key_id": "ak", "r2_secret_access_key": "sk",
            "retention_days": "30", "backup_prefix": "qdrant-backups",
            "environment": "prod", "notify_on": "failure",
            "schedule_mode": "daily", "schedule_time": "02:00",
            "schedule_weekday": "0", "schedule_interval": "360", "timezone": "UTC",
        }
        data.update(over)
        return QdrantBackupConfigForm(
            data, environments=self.ENVS, configured_secrets=set(configured)
        )

    def test_valid_daily_form(self):
        self.assertTrue(self._form().is_valid())

    def test_daily_requires_time(self):
        form = self._form(schedule_mode="daily", schedule_time="")
        self.assertFalse(form.is_valid())
        self.assertIn("schedule_time", form.errors)

    def test_weekly_requires_weekday(self):
        form = self._form(schedule_mode="weekly", schedule_weekday="")
        self.assertFalse(form.is_valid())
        self.assertIn("schedule_weekday", form.errors)

    def test_interval_requires_interval(self):
        form = self._form(schedule_mode="interval", schedule_interval="")
        self.assertFalse(form.is_valid())
        self.assertIn("schedule_interval", form.errors)

    def test_bad_time_format_rejected(self):
        for bad in ("25:00", "12:60", "0200", "2am"):
            form = self._form(schedule_time=bad)
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("schedule_time", form.errors)

    def test_timezone_and_prefix_normalized(self):
        form = self._form(timezone="", backup_prefix="/nested/path/")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["timezone"], "UTC")
        self.assertEqual(form.cleaned_data["backup_prefix"], "nested/path")
        empty = self._form(backup_prefix="")
        self.assertTrue(empty.is_valid(), empty.errors)
        self.assertEqual(empty.cleaned_data["backup_prefix"], "qdrant-backups")

    def test_credentials_required_on_first_setup(self):
        # Nothing configured yet → blank creds are invalid.
        form = self._form(qdrant_api_key="", r2_access_key_id="", r2_secret_access_key="")
        self.assertFalse(form.is_valid())
        for field in SECRET_FIELDS:
            self.assertIn(field, form.errors)

    def test_credentials_optional_once_configured(self):
        # Already stored → blank means "keep existing", so the form is valid.
        form = self._form(
            configured=set(SECRET_FIELDS.values()),
            qdrant_api_key="", r2_access_key_id="", r2_secret_access_key="",
        )
        self.assertTrue(form.is_valid(), form.errors)


# --------------------------------------------------------------------------- #
# Provisioning — one Save idempotently creates exactly the declared resources,
# all owned by the plugin slug, through the real SDK.
# --------------------------------------------------------------------------- #

class ProvisionTests(TestCase):
    def setUp(self):
        from core.models import Environment, Workspace

        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="prod", path="qbenv", requirements="requests\nboto3"
        )
        # Don't push anything to django-q during the provision.
        patch = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        patch.start()
        self.addCleanup(patch.stop)

    def _data(self, **over):
        data = {
            "qdrant_url": "https://qdrant.example.com",
            "r2_endpoint_url": "https://acc.r2.cloudflarestorage.com",
            "r2_bucket_name": "backups",
            "qdrant_api_key": "qk", "r2_access_key_id": "ak", "r2_secret_access_key": "sk",
            "retention_days": 30, "backup_prefix": "qdrant-backups", "keep_zip": False,
            "environment": "prod", "notify_on": "failure", "notify_email": "",
            "schedule_mode": "daily", "schedule_time": "02:00", "timezone": "UTC",
        }
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
        self.assertEqual(warnings, [])  # env has requests + boto3
        self.assertEqual(script.name, prov.SCRIPT_NAME)
        self.assertEqual(script.injection_mode, Script.InjectionMode.SELECTED)
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 3, "stores": 1, "grants": 3, "schedules": 1})
        # The 3 owner secrets carry the clean env-var names the worker reads.
        self.assertEqual(set(prov.configured_secret_keys()), set(SECRET_FIELDS.values()))
        # Non-secret config is persisted to the data store, not encrypted.
        self.assertEqual(prov.get_config()["retention_days"], 30)
        self.assertEqual(prov.get_config()["qdrant_url"], "https://qdrant.example.com")

    def test_provision_is_idempotent(self):
        prov.provision(self._data())
        prov.provision(self._data(retention_days=7))  # re-Save with a change
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 3, "stores": 1, "grants": 3, "schedules": 1})
        self.assertEqual(prov.get_config()["retention_days"], 7)

    def test_blank_credential_keeps_existing_value(self):
        from core.models import Secret

        prov.provision(self._data())
        prov.provision(self._data(qdrant_api_key="", r2_access_key_id="", r2_secret_access_key=""))
        secret = Secret.objects.get(owner_plugin=prov.OWNER, owner_key="QDRANT_API_KEY")
        self.assertEqual(secret.get_decrypted_value(), "qk")  # unchanged, not blanked
        self.assertEqual(Secret.objects.filter(owner_plugin=prov.OWNER).count(), 3)

    def test_environment_missing_packages_warns_but_succeeds(self):
        from core.models import Environment

        Environment.objects.create(name="bare", path="bareenv", requirements="")
        _, warnings = prov.provision(self._data(environment="bare"))
        self.assertTrue(any("boto3" in w for w in warnings))

    def test_unknown_environment_raises(self):
        with self.assertRaises(ValueError):
            prov.provision(self._data(environment="ghost"))
