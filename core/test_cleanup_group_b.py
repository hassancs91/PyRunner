"""
⚪ nit-batch cleanups — Group (b) regressions for the 2026-07 code review.

- 2.5 / 2.4  ``s3_backup_last_size`` is now ``BigIntegerField`` (>2 GB backups no
       longer overflow on Postgres); ``allow_registration`` help_text says the flag
       is dormant/invite-only (migration 0047).
- 3.1  ``execute_run`` resolves secrets ONCE and passes the dict into
       ``_build_script_environment`` (injection + masking share it; single decrypt).
- 4b.4  ``get_database_size`` returns 0 / "N/A" on non-SQLite (was a bogus "0 B");
       one shared heartbeat window; the webhook SSRF check DNS-resolves via the S3
       helper; ``_map_boto3_error`` is shared; ``list_files`` paginates;
       ``should_notify`` compares ``Run.Status`` enums.
- 6.2  the stale ``CONSOLE_INPUT_CLASS`` alias is gone.
- 7.3  ``_safe_extract`` enforces the size cap on ACTUAL bytes + uses
       ``Path.is_relative_to`` for containment.
- 7.4  ``ScheduleAPI.sync`` validates required inputs per mode.
"""

import io
import tempfile
import zipfile
from datetime import timedelta
from unittest import mock

from django.db import models as dj_models
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.executor import _build_script_environment
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    ScriptSchedule,
)
from core.services.notification_service import NotificationService
from core.services.plugin_service import (
    MAX_SINGLE_FILE,  # noqa: F401 (imported to document the patched constant)
    PluginInstallError,
    PluginService,
)
from core.services.s3_service import S3Service
from core.services.system_info_service import SystemInfoService


def _make_script(**kwargs):
    env = Environment.objects.create(name="e-cleanupb", path="p-cleanupb")
    return Script.objects.create(name="s-cleanupb", code="print('x')", environment=env, **kwargs)


class SettingsFieldTests(TestCase):
    """Reviews 2.5 / 2.4 — field type + help_text (migration 0047)."""

    def test_s3_backup_size_is_bigint_and_holds_over_2gb(self):
        field = GlobalSettings._meta.get_field("s3_backup_last_size")
        self.assertIsInstance(field, dj_models.BigIntegerField)
        gs = GlobalSettings.get_settings()
        gs.s3_backup_last_size = 5_000_000_000  # > 2^31
        gs.save(update_fields=["s3_backup_last_size"])
        gs.refresh_from_db()
        self.assertEqual(gs.s3_backup_last_size, 5_000_000_000)

    def test_allow_registration_help_text_says_dormant(self):
        ht = GlobalSettings._meta.get_field("allow_registration").help_text
        self.assertIn("invite-only", ht)


class SecretsResolvedOnceTests(TestCase):
    """Review 3.1 — a pre-resolved secrets dict is used verbatim (no re-resolve)."""

    def test_passed_secrets_are_used_without_reresolving(self):
        with mock.patch("core.executor.resolve_secrets_for_run") as resolver:
            env = _build_script_environment(run=None, secrets={"MY_SECRET": "v"})
            resolver.assert_not_called()
        self.assertEqual(env["MY_SECRET"], "v")

    def test_omitted_secrets_are_resolved_once(self):
        with mock.patch(
            "core.executor.resolve_secrets_for_run", return_value={}
        ) as resolver:
            _build_script_environment(run=None)
            resolver.assert_called_once()


class DatabaseSizeVendorTests(TestCase):
    """Review 4b.4 — DB size is SQLite-only; non-SQLite reports N/A, not "0 B"."""

    def test_non_sqlite_returns_zero_and_na(self):
        with mock.patch("django.db.connection") as conn:
            conn.vendor = "postgresql"
            self.assertEqual(SystemInfoService.get_database_size(), 0)
            self.assertEqual(SystemInfoService.get_database_size_display(), "N/A")

    def test_sqlite_display_is_not_na(self):
        # The suite runs on SQLite; display is a real (possibly "0 B") size.
        self.assertNotEqual(SystemInfoService.get_database_size_display(), "N/A")


class HeartbeatWindowTests(TestCase):
    """Review 4b.4 — one shared heartbeat staleness window."""

    def test_shared_constant_and_default(self):
        self.assertEqual(GlobalSettings.WORKER_HEARTBEAT_TIMEOUT_SECONDS, 180)
        gs = GlobalSettings.get_settings()
        gs.worker_heartbeat_at = timezone.now() - timedelta(seconds=120)
        self.assertTrue(gs.worker_is_alive())  # 120 < 180
        gs.worker_heartbeat_at = timezone.now() - timedelta(seconds=200)
        self.assertFalse(gs.worker_is_alive())  # 200 > 180


class WebhookSSRFTests(TestCase):
    """Review 4b.4 — the webhook SSRF check DNS-resolves (a DNS name pointing at a
    private IP is now blocked, not just a literal private IP)."""

    def test_private_literal_blocked(self):
        self.assertFalse(NotificationService._is_safe_webhook_url("http://10.0.0.5/h"))

    def test_bad_scheme_and_unspecified_blocked(self):
        self.assertFalse(NotificationService._is_safe_webhook_url("ftp://example.com/h"))
        self.assertFalse(NotificationService._is_safe_webhook_url("http://0.0.0.0/h"))

    def test_dns_name_resolving_to_private_ip_blocked(self):
        with mock.patch(
            "core.services.s3_service.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("10.1.2.3", 0))],
        ):
            self.assertFalse(
                NotificationService._is_safe_webhook_url("http://evil.example/h")
            )

    def test_dns_name_resolving_to_public_ip_allowed(self):
        with mock.patch(
            "core.services.s3_service.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
        ):
            self.assertTrue(
                NotificationService._is_safe_webhook_url("https://ok.example/h")
            )


class S3HelperTests(TestCase):
    """Review 4b.4 — shared boto3 error map + paginated list_files."""

    def test_error_map_covers_common_cases(self):
        self.assertIn("does not exist", S3Service._map_boto3_error("NoSuchBucket", "b"))
        self.assertIn("Access denied", S3Service._map_boto3_error("AccessDenied", "b"))
        self.assertIn("Invalid access key", S3Service._map_boto3_error("InvalidAccessKeyId", "b"))
        self.assertIn("Connection failed", S3Service._map_boto3_error("something odd", "b"))

    def test_list_files_collects_all_pages(self):
        from core.models import StorageConnection

        connection = StorageConnection(name="t", bucket="b")
        client = mock.Mock()
        paginator = mock.Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a", "Size": 1, "LastModified": "t1"}]},
            {"Contents": [{"Key": "b", "Size": 2, "LastModified": "t2"}]},
        ]
        client.get_paginator.return_value = paginator
        with mock.patch.object(S3Service, "get_client", return_value=client):
            files = S3Service.list_files(connection)
        client.get_paginator.assert_called_once_with("list_objects_v2")
        self.assertEqual([f["key"] for f in files], ["a", "b"])


class ShouldNotifyEnumTests(TestCase):
    """Review 4b.4 — should_notify compares Run.Status enums."""

    def test_failure_covers_failed_and_timeout(self):
        script = _make_script(notify_on=Script.NotifyOn.FAILURE)
        self.assertTrue(NotificationService.should_notify(Run(script=script, status=Run.Status.FAILED)))
        self.assertTrue(NotificationService.should_notify(Run(script=script, status=Run.Status.TIMEOUT)))
        self.assertFalse(NotificationService.should_notify(Run(script=script, status=Run.Status.SUCCESS)))

    def test_success_only_on_success(self):
        script = _make_script(notify_on=Script.NotifyOn.SUCCESS)
        self.assertTrue(NotificationService.should_notify(Run(script=script, status=Run.Status.SUCCESS)))
        self.assertFalse(NotificationService.should_notify(Run(script=script, status=Run.Status.FAILED)))


class ZipExtractHardeningTests(SimpleTestCase):
    """Review 7.3 — cap enforced on real bytes + is_relative_to containment."""

    def _zip(self, name, data):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(name, data)
        buf.seek(0)
        return zipfile.ZipFile(buf)

    def test_actual_bytes_over_cap_raises(self):
        zf = self._zip("plugin_slug/big.txt", b"x" * 100)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("core.services.plugin_service.MAX_SINGLE_FILE", 10):
                with self.assertRaises(PluginInstallError):
                    PluginService._safe_extract(zf, tmp)

    def test_path_traversal_blocked(self):
        zf = self._zip("../escape.txt", b"data")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PluginInstallError):
                PluginService._safe_extract(zf, tmp)


class ScheduleAPIValidationTests(TestCase):
    """Review 7.4 — sync rejects missing required inputs per mode."""

    def setUp(self):
        from core.plugins.api import ScheduleAPI

        self.api = ScheduleAPI()
        self.script = _make_script()

    def test_daily_requires_time(self):
        with self.assertRaises(ValueError):
            self.api.sync(self.script, mode=ScriptSchedule.RunMode.DAILY, time_str=None)

    def test_weekly_requires_time(self):
        with self.assertRaises(ValueError):
            self.api.sync(
                self.script, mode=ScriptSchedule.RunMode.WEEKLY, weekday=0, time_str=None
            )

    def test_interval_requires_minutes(self):
        with self.assertRaises(ValueError):
            self.api.sync(
                self.script, mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=None
            )


class FormsInputClassTests(SimpleTestCase):
    """Review 6.2 — the stale CONSOLE_INPUT_CLASS alias is gone."""

    def test_alias_removed(self):
        import core.forms as forms_mod

        self.assertTrue(hasattr(forms_mod, "INPUT_CLASS"))
        self.assertFalse(hasattr(forms_mod, "CONSOLE_INPUT_CLASS"))
