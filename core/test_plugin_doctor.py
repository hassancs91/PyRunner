"""
Plugin Platform v2 — Stage 4 (plugin doctor) tests.

Tier-1 static lint: structural rules + the v2 "no models/migrations" rule + the
light-import (no core.models in apps.py) rule, with data-driven fail/warn
severities. Pure file/AST checks, so SimpleTestCase (no DB).
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from core.services.plugin_doctor import run_doctor

VALID_APPS = '''\
from core.plugins import PluginAppConfig, PyRunnerPlugin


class {cls}(PluginAppConfig):
    name = "plugins.{slug}"
    label = "{slug}"
    plugin = PyRunnerPlugin(slug="{slug}", name="X", version="1.0.0")
'''

VALID_URLS = 'app_name = "{slug}"\nurlpatterns = []\n'


class _Builder:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyrunner-doctor-")
        self.root = Path(self._tmp.name)

    def make(self, slug="goodplug", *, apps=VALID_APPS, urls=VALID_URLS,
             manifest=True, init=True, extra=None):
        folder = self.root / slug
        folder.mkdir(parents=True, exist_ok=True)
        if init:
            (folder / "__init__.py").write_text("", encoding="utf-8")
        if manifest:
            (folder / "plugin.json").write_text(
                '{"slug": "%s", "name": "X", "version": "1.0.0"}' % slug, encoding="utf-8"
            )
        if apps is not None:
            cls = "".join(p.capitalize() for p in slug.split("_")) + "Config"
            (folder / "apps.py").write_text(apps.format(cls=cls, slug=slug), encoding="utf-8")
        if urls is not None:
            (folder / "urls.py").write_text(urls.format(slug=slug), encoding="utf-8")
        for rel, content in (extra or {}).items():
            p = folder / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return folder

    def cleanup(self):
        self._tmp.cleanup()


def _rules(report, severity):
    return {f.rule for f in report.findings if f.severity == severity}


class DoctorTests(SimpleTestCase):
    def setUp(self):
        self.b = _Builder()
        self.addCleanup(self.b.cleanup)

    def test_clean_plugin_passes(self):
        report = run_doctor(self.b.make())
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.fail_count, 0)

    def test_reserved_slug_fails(self):
        report = run_doctor(self.b.make("core"))
        self.assertFalse(report.ok)
        self.assertIn("slug", _rules(report, "fail"))

    def test_missing_apps_fails(self):
        report = run_doctor(self.b.make(apps=None))
        self.assertFalse(report.ok)
        self.assertIn("package", _rules(report, "fail"))

    def test_models_py_fails(self):
        report = run_doctor(self.b.make(extra={"models.py": "from django.db import models\n"}))
        self.assertFalse(report.ok)
        self.assertIn("no-ddl", _rules(report, "fail"))

    def test_migrations_dir_fails(self):
        report = run_doctor(self.b.make(extra={"migrations/__init__.py": ""}))
        self.assertFalse(report.ok)
        self.assertIn("no-ddl", _rules(report, "fail"))

    def test_apps_imports_core_models_fails(self):
        bad = "from core.models import Script\n" + VALID_APPS
        report = run_doctor(self.b.make(apps=bad))
        self.assertFalse(report.ok)
        self.assertIn("apps-imports", _rules(report, "fail"))

    def test_apps_name_mismatch_fails(self):
        bad = VALID_APPS.replace('name = "plugins.{slug}"', 'name = "plugins.wrong"')
        report = run_doctor(self.b.make(apps=bad))
        self.assertFalse(report.ok)
        self.assertIn("apps", _rules(report, "fail"))

    def test_urls_app_name_mismatch_fails(self):
        report = run_doctor(self.b.make(urls='app_name = "wrong"\nurlpatterns = []\n'))
        self.assertFalse(report.ok)
        self.assertIn("urls", _rules(report, "fail"))

    def test_no_urls_is_fine(self):
        report = run_doctor(self.b.make(urls=None))
        self.assertTrue(report.ok, report.format())

    def test_template_shadow_fails(self):
        report = run_doctor(self.b.make(extra={"templates/index.html": "<h1>shadow</h1>"}))
        self.assertFalse(report.ok)
        self.assertIn("asset-shadow", _rules(report, "fail"))

    def test_namespaced_template_ok(self):
        report = run_doctor(self.b.make(
            "goodplug", extra={"templates/goodplug/index.html": "<h1>ok</h1>"}
        ))
        self.assertTrue(report.ok, report.format())

    def test_heavy_import_in_apps_warns(self):
        bad = "import requests\n" + VALID_APPS
        report = run_doctor(self.b.make(apps=bad))
        self.assertTrue(report.ok)  # warn does not block
        self.assertIn("apps-imports", _rules(report, "warn"))

    def test_core_internal_import_outside_apps_warns(self):
        report = run_doctor(self.b.make(
            extra={"views.py": "from core.models import Script\n"}
        ))
        self.assertTrue(report.ok)  # advisory only
        self.assertIn("sdk-usage", _rules(report, "warn"))

    def test_core_internal_import_in_test_file_is_exempt(self):
        # A plugin's own tests legitimately import core.models to assert behavior;
        # the sdk-usage advisory must not nudge authors away from shipping tests.
        report = run_doctor(self.b.make(
            extra={"tests.py": "from core.models import Script\n"}
        ))
        self.assertTrue(report.ok)
        self.assertNotIn("sdk-usage", _rules(report, "warn"))
        self.assertIn("sdk-usage", _rules(report, "pass"))

    def test_two_appconfigs_fails(self):
        bad = VALID_APPS + "\n\nclass Second(PluginAppConfig):\n    name = 'plugins.{slug}'\n    label = '{slug}'\n"
        report = run_doctor(self.b.make(apps=bad))
        self.assertFalse(report.ok)
        self.assertIn("apps", _rules(report, "fail"))
