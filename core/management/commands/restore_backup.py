"""
Restore a PyRun backup file from the CLI.

A thin wrapper over ``BackupService.restore_backup`` (the same code path the
Settings UI uses), plus a tenancy-seam touch-up: any restored row left without a
workspace is assigned to the default workspace. Used both standalone and as the
import step of ``migrate_db``.
"""

import gzip
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services.backup_service import BackupService


def assign_default_workspace() -> int:
    """Assign every un-scoped scoped-row to the default workspace. Idempotent."""
    from core.models import (
        DataStore,
        Environment,
        Run,
        Script,
        ScriptSchedule,
        Secret,
        Workspace,
    )

    default = Workspace.get_default()
    if default is None:
        default = Workspace.objects.create(name="Default Workspace", is_default=True)

    assigned = 0
    for Model in (Script, Secret, Run, DataStore, Environment, ScriptSchedule):
        assigned += Model.objects.filter(workspace__isnull=True).update(workspace=default)
    return assigned


class Command(BaseCommand):
    help = "Restore a PyRun backup file (.json or .json.gz). REPLACES existing data."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the backup file")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm the restore (it deletes and replaces existing data)",
        )
        parser.add_argument(
            "--no-runs", action="store_true", help="Skip restoring run history"
        )

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.exists():
            raise CommandError(f"Backup file not found: {path}")

        raw = path.read_bytes()
        if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise CommandError(f"Could not parse backup file: {e}")

        validation = BackupService.validate_backup(data)
        if not validation.get("valid"):
            raise CommandError(f"Invalid backup: {validation.get('errors')}")

        if not opts["yes"]:
            raise CommandError(
                "Refusing to restore without --yes (this REPLACES all existing data)."
            )

        result = BackupService.restore_backup(data, restore_runs=not opts["no_runs"])
        if not result.get("success"):
            raise CommandError(f"Restore failed: {result.get('errors')}")

        scoped = assign_default_workspace()
        self.stdout.write(
            self.style.SUCCESS(
                f"Restore complete: {result['counts']}; workspace-assigned {scoped} rows"
            )
        )
