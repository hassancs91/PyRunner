"""
Migrate this PyRun instance's data to a Postgres database (SQLite -> Postgres).

A guided, maintenance-window operation. It exports the current DB (uncapped),
creates the schema on the target, restores the data into it, and verifies row
counts table-by-table. Built on the existing backup/restore machinery: the
import runs as a subprocess with DATABASE_URL pointed at the target, so the
tested ``restore_backup`` path writes into the target with no special-casing.

    manage.py migrate_db --to postgres://user:pass@host:5432/db --dry-run
    manage.py migrate_db --to postgres://user:pass@host:5432/db --yes

Caveats (printed at run time): stop writes first (a maintenance window); take a
backup; after success, switch DATABASE_URL to the target and restart.
"""

import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Core tables whose row counts we verify match after the move.
_VERIFY_TABLES = [
    ("scripts", "Script"),
    ("secrets", "Secret"),
    ("runs", "Run"),
    ("datastores", "DataStore"),
    ("datastore_entries", "DataStoreEntry"),
    ("environments", "Environment"),
    ("script_schedules", "ScriptSchedule"),
]


class Command(BaseCommand):
    help = "Migrate this instance's data from SQLite to a Postgres database."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Target DATABASE_URL (postgres://...)")
        parser.add_argument("--dry-run", action="store_true", help="Report only; make no changes")
        parser.add_argument("--yes", action="store_true", help="Confirm the real migration")

    def handle(self, *args, **opts):
        target = opts["to"]
        if not target.startswith(("postgres://", "postgresql://")):
            raise CommandError("--to must be a postgres:// URL")

        self._check_target_reachable(target)
        source_counts = self._source_counts()

        self.stdout.write("Source row counts:")
        for _table, model in _VERIFY_TABLES:
            self.stdout.write(f"  {model}: {source_counts[model]}")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry run complete — no changes made."))
            return

        if not opts["yes"]:
            raise CommandError(
                "This WRITES to the target database. Stop writes (maintenance window) "
                "and take a backup first, then re-run with --yes."
            )

        # 1. Export the source, uncapped (max_runs=0 = all runs).
        self.stdout.write("Exporting source data (uncapped)...")
        from core.services.backup_service import BackupService

        backup = BackupService.create_backup(
            include_runs=True,
            max_runs=0,
            include_package_operations=True,
            include_datastores=True,
        )

        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json.gz", delete=False
        ) as fh:
            fh.write(gzip.compress(json.dumps(backup).encode("utf-8")))
            backup_path = fh.name

        try:
            # 2. Create the schema on the target (subprocess: DATABASE_URL=target).
            self.stdout.write("Creating schema on target (migrate)...")
            self._subprocess(["migrate", "--noinput"], target, disable_plugins=True)

            # 3. Restore the data into the target.
            self.stdout.write("Restoring data into target...")
            self._subprocess(["restore_backup", backup_path, "--yes"], target, disable_plugins=True)

            # 4. Verify row counts.
            self.stdout.write("Verifying row counts...")
            target_counts = self._target_counts(target)
            mismatches = []
            for table, model in _VERIFY_TABLES:
                src, tgt = source_counts[model], target_counts.get(table, -1)
                flag = "OK" if src == tgt else "MISMATCH"
                if src != tgt:
                    mismatches.append((model, src, tgt))
                self.stdout.write(f"  {model}: source={src} target={tgt} [{flag}]")

            if mismatches:
                raise CommandError(f"Row-count mismatch after migration: {mismatches}")
        finally:
            Path(backup_path).unlink(missing_ok=True)

        self.stdout.write(self.style.SUCCESS("Migration complete and verified."))
        self.stdout.write(
            "Next: set DATABASE_URL to the target, then restart PyRun so it "
            "runs on Postgres."
        )

    # --- helpers ---------------------------------------------------------

    def _source_counts(self) -> dict:
        from core.models import (
            DataStore,
            DataStoreEntry,
            Environment,
            Run,
            Script,
            ScriptSchedule,
            Secret,
        )

        models = {
            "Script": Script,
            "Secret": Secret,
            "Run": Run,
            "DataStore": DataStore,
            "DataStoreEntry": DataStoreEntry,
            "Environment": Environment,
            "ScriptSchedule": ScriptSchedule,
        }
        return {name: m.objects.count() for name, m in models.items()}

    def _check_target_reachable(self, target: str) -> None:
        try:
            import psycopg

            conn = psycopg.connect(target, connect_timeout=10)
            conn.close()
        except Exception as e:
            raise CommandError(f"Cannot connect to target database: {e}")

    def _target_counts(self, target: str) -> dict:
        import psycopg

        out = {}
        conn = psycopg.connect(target, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                for table, _model in _VERIFY_TABLES:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    out[table] = cur.fetchone()[0]
        finally:
            conn.close()
        return out

    def _subprocess(self, args: list, target: str, disable_plugins: bool = False) -> None:
        import os

        env = dict(os.environ)
        env["DATABASE_URL"] = target
        if disable_plugins:
            # Mirror entrypoint: migrations never need plugins, and a broken
            # active plugin must not derail the migration.
            env["PYRUNNER_DISABLE_PLUGINS"] = "1"
        cmd = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py")] + args
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(
                f"Step `{' '.join(args)}` failed:\n{result.stdout}\n{result.stderr}"
            )
