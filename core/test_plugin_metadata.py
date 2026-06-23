"""
Plugin Platform v2 — Stage 6 (plugin metadata / marketplace-prep) tests.

Covers the three moving parts:
  * doctor metadata validation — FAIL on malformed values, WARN on missing
    recommended fields, legacy (slug/name/version-only) manifest still passes;
  * Plugin model accessors — read packaged metadata from the manifest JSONField
    with no migration (provisions_summary formatting, summary fallback, icon_url);
  * icon serve view + detail view — served from disk (works for installed-but-not
    -active plugins), traversal-guarded, superuser-gated.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Plugin, User
from core.services.plugin_doctor import DoctorReport, _check_metadata, run_doctor


# --------------------------------------------------------------------------- #
# Doctor — metadata validation (isolated _check_metadata calls)
# --------------------------------------------------------------------------- #

class _MetaBuilder:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyrunner-meta-")
        self.root = Path(self._tmp.name)

    def folder(self, manifest, slug="goodplug", icon_files=()):
        f = self.root / slug
        f.mkdir(parents=True, exist_ok=True)
        (f / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel in icon_files:
            p = f / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("<svg/>", encoding="utf-8")
        return f

    def cleanup(self):
        self._tmp.cleanup()


def _rules(report, severity):
    return {f.rule for f in report.findings if f.severity == severity}


class DoctorMetadataTests(SimpleTestCase):
    def setUp(self):
        self.b = _MetaBuilder()
        self.addCleanup(self.b.cleanup)

    def _check(self, manifest, slug="goodplug", icon_files=()):
        report = DoctorReport(slug=slug)
        _check_metadata(self.b.folder(manifest, slug, icon_files), report)
        return report

    def test_legacy_minimal_manifest_passes_with_only_warnings(self):
        report = self._check({"slug": "goodplug", "name": "X", "version": "1.0.0"})
        self.assertTrue(report.ok)  # no FAIL → still activates
        self.assertIn("metadata", _rules(report, "warn"))  # recommended fields missing

    def test_full_manifest_clean(self):
        report = self._check(
            {
                "manifest_version": 1,
                "slug": "goodplug",
                "publisher": "acme-co",
                "name": "X",
                "version": "1.2.3",
                "summary": "s",
                "author": "A",
                "license": "MIT",
                "icon": "assets/icon.svg",
                "categories": ["backup"],
                "keywords": ["x"],
                "provisions": {"scripts": 1, "secrets": 3, "secret_keys": ["A_KEY"]},
            },
            icon_files=("assets/icon.svg",),
        )
        self.assertTrue(report.ok, report.format())
        self.assertNotIn("metadata", _rules(report, "warn"))
        self.assertIn("metadata", _rules(report, "pass"))

    def test_bad_semver_fails(self):
        report = self._check({"slug": "goodplug", "version": "v1", "author": "A",
                              "license": "MIT", "summary": "s", "icon": "i.svg"},
                             icon_files=("i.svg",))
        self.assertFalse(report.ok)
        self.assertIn("metadata", _rules(report, "fail"))

    def test_unknown_manifest_version_fails(self):
        report = self._check({"slug": "goodplug", "manifest_version": 2})
        self.assertFalse(report.ok)
        self.assertIn("metadata", _rules(report, "fail"))

    def test_icon_traversal_fails(self):
        report = self._check({"slug": "goodplug", "icon": "../evil.png"})
        self.assertFalse(report.ok)

    def test_icon_bad_extension_fails(self):
        report = self._check({"slug": "goodplug", "icon": "assets/icon.txt"})
        self.assertFalse(report.ok)

    def test_icon_declared_but_missing_only_warns(self):
        report = self._check({"slug": "goodplug", "icon": "assets/icon.svg",
                              "author": "A", "license": "MIT", "summary": "s"})
        self.assertTrue(report.ok)  # cosmetic miss → never blocks activation
        self.assertIn("metadata-icon", _rules(report, "warn"))

    def test_provisions_wrong_count_type_fails(self):
        report = self._check({"slug": "goodplug", "provisions": {"scripts": "two"}})
        self.assertFalse(report.ok)

    def test_provisions_secret_keys_must_be_list_of_strings(self):
        report = self._check({"slug": "goodplug", "provisions": {"secret_keys": [1, 2]}})
        self.assertFalse(report.ok)

    def test_categories_must_be_list_fails(self):
        report = self._check({"slug": "goodplug", "categories": "backup"})
        self.assertFalse(report.ok)

    def test_malformed_publisher_fails(self):
        report = self._check({"slug": "goodplug", "publisher": "Bad Publisher"})
        self.assertFalse(report.ok)

    def test_run_doctor_end_to_end_metadata_blocks(self):
        """A full doctor run refuses a structurally-valid plugin with bad metadata."""
        apps = (
            'from core.plugins import PluginAppConfig, PyRunnerPlugin\n\n\n'
            'class GoodplugConfig(PluginAppConfig):\n'
            '    name = "plugins.goodplug"\n    label = "goodplug"\n'
            '    plugin = PyRunnerPlugin(slug="goodplug", name="X", version="1.0.0")\n'
        )
        f = self.b.folder({"slug": "goodplug", "name": "X", "version": "not-semver"})
        (f / "__init__.py").write_text("", encoding="utf-8")
        (f / "apps.py").write_text(apps, encoding="utf-8")
        report = run_doctor(f)
        self.assertFalse(report.ok)
        self.assertIn("metadata", _rules(report, "fail"))


# --------------------------------------------------------------------------- #
# Plugin model — manifest accessors (no DB needed)
# --------------------------------------------------------------------------- #

class PluginAccessorTests(SimpleTestCase):
    def _p(self, manifest):
        return Plugin(slug="demo", name="Demo", manifest=manifest)

    def test_provisions_summary_formats_and_pluralizes(self):
        p = self._p({"provisions": {"scripts": 1, "secrets": 3, "datastores": 1, "schedules": 2}})
        self.assertEqual(p.provisions_summary, "1 script, 3 secrets, 1 data store, 2 schedules")

    def test_provisions_summary_skips_zero_missing_and_bool(self):
        p = self._p({"provisions": {"scripts": 0, "secrets": True, "datastores": 2}})
        self.assertEqual(p.provisions_summary, "2 data stores")

    def test_provisions_summary_empty_when_none(self):
        self.assertEqual(self._p({}).provisions_summary, "")

    def test_summary_falls_back_to_description(self):
        self.assertEqual(self._p({"description": "long"}).summary, "long")
        self.assertEqual(self._p({"summary": "short", "description": "long"}).summary, "short")

    def test_categories_keywords_coerced_to_list(self):
        self.assertEqual(self._p({"categories": ["a", "b"]}).categories, ["a", "b"])
        self.assertEqual(self._p({"categories": "nope"}).categories, [])
        self.assertEqual(self._p({}).keywords, [])

    def test_meta_handles_missing_and_non_dict_manifest(self):
        self.assertEqual(self._p({}).author, "")
        p = Plugin(slug="demo", name="Demo")
        p.manifest = None  # defensive: not a dict
        self.assertEqual(p.author, "")

    def test_icon_url_present_and_absent(self):
        self.assertIsNone(self._p({}).icon_url)
        self.assertFalse(self._p({}).has_icon)
        p = self._p({"icon": "assets/icon.svg", "icon_fallback": "🗄️"})
        self.assertTrue(p.has_icon)
        self.assertEqual(p.icon_url, reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(p.icon_fallback, "🗄️")


# --------------------------------------------------------------------------- #
# Views — icon serve + detail page (superuser-gated, served from PLUGINS_DIR)
# --------------------------------------------------------------------------- #

class _SuperuserMixin(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True, is_staff=True)
        self.client.force_login(self.admin)


class IconViewTests(_SuperuserMixin):
    def setUp(self):
        super().setUp()
        # ignore_cleanup_errors: a streamed FileResponse can still hold the icon
        # handle on Windows when teardown runs; don't fail the suite over it.
        self._tmp = tempfile.TemporaryDirectory(prefix="pyrunner-plugins-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.plugins_dir = Path(self._tmp.name)

    def _make_plugin(self, *, icon, write_icon=True, status=Plugin.Status.INSTALLED):
        folder = self.plugins_dir / "demo"
        (folder / "assets").mkdir(parents=True, exist_ok=True)
        if write_icon and icon:
            (folder / icon).write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        manifest = {"slug": "demo", "name": "Demo", "version": "1.0.0"}
        if icon:
            manifest["icon"] = icon
        return Plugin.objects.create(slug="demo", name="Demo", version="1.0.0",
                                     status=status, manifest=manifest)

    def test_serves_icon_for_installed_plugin(self):
        self._make_plugin(icon="assets/icon.svg")
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            resp = self.client.get(reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/svg+xml")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        resp.close()  # release the file handle before tempdir teardown (Windows)

    def test_no_icon_declared_404(self):
        self._make_plugin(icon=None)
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            resp = self.client.get(reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(resp.status_code, 404)

    def test_missing_file_404(self):
        self._make_plugin(icon="assets/icon.svg", write_icon=False)
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            resp = self.client.get(reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(resp.status_code, 404)

    def test_traversal_in_manifest_blocked(self):
        # An attacker-crafted manifest must not be able to read outside the folder.
        (self.plugins_dir / "secret.svg").write_text("TOP SECRET", encoding="utf-8")
        self._make_plugin(icon="../secret.svg", write_icon=False)
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            resp = self.client.get(reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(resp.status_code, 404)

    def test_non_superuser_redirected(self):
        self._make_plugin(icon="assets/icon.svg")
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            resp = self.client.get(reverse("cpanel:plugin_icon", args=["demo"]))
        self.assertEqual(resp.status_code, 302)


class DetailViewTests(_SuperuserMixin):
    def test_detail_renders_metadata(self):
        Plugin.objects.create(
            slug="demo", name="Demo Plugin", version="1.0.0",
            status=Plugin.Status.INSTALLED,
            manifest={
                "slug": "demo", "name": "Demo Plugin", "version": "1.0.0",
                "author": "Hasan", "license": "MIT", "summary": "A demo.",
                "categories": ["backup"],
                "provisions": {"scripts": 1, "secrets": 3, "schedules": 1},
            },
        )
        resp = self.client.get(reverse("cpanel:plugin_detail", args=["demo"]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Demo Plugin", body)
        self.assertIn("Hasan", body)
        self.assertIn("MIT", body)
        self.assertIn("backup", body)
        self.assertIn("1 script, 3 secrets, 1 schedule", body)

    def test_detail_unknown_slug_404(self):
        resp = self.client.get(reverse("cpanel:plugin_detail", args=["nope"]))
        self.assertEqual(resp.status_code, 404)

    def test_detail_non_superuser_redirected(self):
        Plugin.objects.create(slug="demo", name="Demo", manifest={"slug": "demo"})
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        resp = self.client.get(reverse("cpanel:plugin_detail", args=["demo"]))
        self.assertEqual(resp.status_code, 302)
