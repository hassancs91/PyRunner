"""
Seam 3 — workspace column + default-workspace backfill (tenancy seam).

Proves the seam is inert and non-breaking: one default workspace, every existing
row backfilled to it, the backfill idempotent + reversible, new rows still
default to NULL (no query-scoping), and a failed migration is now boot-fatal.
"""

import importlib
from unittest import mock

from django.apps import apps as global_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import (
    DataStore,
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Secret,
    Workspace,
)
from core.services.setup_service import SetupService

# The migration module name starts with a digit, so import it dynamically.
_backfill_mig = importlib.import_module("core.migrations.0029_workspace_backfill")
backfill_default_workspace = _backfill_mig.backfill_default_workspace
unbackfill = _backfill_mig.unbackfill


class WorkspaceBackfillTests(TestCase):
    def setUp(self):
        # Rows that predate any workspace assignment (workspace left NULL).
        self.env = Environment.objects.create(name="e", path="wsenv1")
        self.script = Script.objects.create(
            name="s", code="print(1)", environment=self.env
        )
        self.secret = Secret.objects.create(key="TESTKEY", encrypted_value="enc")
        self.run = Run.objects.create(script=self.script)
        self.ds = DataStore.objects.create(name="ds1")
        self.sched = ScriptSchedule.objects.create(script=self.script)
        self._all = [self.env, self.script, self.secret, self.run, self.ds, self.sched]

    def test_migration_created_one_default_workspace(self):
        # 0029 ran during test-DB build and created exactly one default.
        self.assertEqual(Workspace.objects.filter(is_default=True).count(), 1)
        self.assertIsNotNone(Workspace.get_default())

    def test_rows_start_unscoped(self):
        for obj in self._all:
            obj.refresh_from_db()
            self.assertIsNone(obj.workspace_id)

    def test_backfill_assigns_all_to_default(self):
        backfill_default_workspace(global_apps, None)
        default = Workspace.get_default()
        for obj in self._all:
            obj.refresh_from_db()
            self.assertEqual(obj.workspace_id, default.id)
        # No duplicate default created (reused the migration's one).
        self.assertEqual(Workspace.objects.filter(is_default=True).count(), 1)

    def test_backfill_is_idempotent(self):
        backfill_default_workspace(global_apps, None)
        backfill_default_workspace(global_apps, None)
        self.assertEqual(Workspace.objects.filter(is_default=True).count(), 1)
        self.script.refresh_from_db()
        self.assertEqual(self.script.workspace_id, Workspace.get_default().id)

    def test_reverse_detaches_and_deletes_default(self):
        backfill_default_workspace(global_apps, None)
        unbackfill(global_apps, None)
        for obj in self._all:
            obj.refresh_from_db()
            self.assertIsNone(obj.workspace_id)
        self.assertEqual(Workspace.objects.filter(is_default=True).count(), 0)

    def test_new_row_defaults_to_null_workspace(self):
        # The seam is inert: nothing auto-scopes new rows (no query-scoping yet).
        fresh = Script.objects.create(name="fresh", code="x", environment=self.env)
        self.assertIsNone(fresh.workspace_id)


class BootFatalMigrationTests(TestCase):
    """A failed migration must abort boot (non-zero exit), not serve a half-migrated DB."""

    def test_setup_raises_on_migration_failure(self):
        with mock.patch.object(
            SetupService, "run_migrations", return_value=(False, "boom")
        ):
            with self.assertRaises(CommandError):
                call_command("setup")

    def test_setup_succeeds_when_migrations_ok(self):
        with mock.patch.object(
            SetupService, "run_migrations", return_value=(True, "ok")
        ), mock.patch.object(SetupService, "is_setup_needed", return_value=False):
            # Should return cleanly (already-complete path) without raising.
            call_command("setup")
