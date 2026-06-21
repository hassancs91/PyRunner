"""
plugin_doctor — run the Tier-1 static lint on a plugin and print a per-rule report.

For plugin authors: validate a plugin's structure WITHOUT uploading or activating
it. Works on an installed plugin by slug, or on any local folder via ``--path``
(handy alongside dev mode). Reads files + AST only — never imports/executes the
plugin.

    python manage.py plugin_doctor qdrant_backup
    python manage.py plugin_doctor --path ./qdrant-backup-plugin/qdrant_backup

Exit code is 0 when nothing blocks activation (warnings are advisory), 1 when
there is at least one blocking failure — so it slots into CI.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.plugin_doctor import run_doctor


class Command(BaseCommand):
    help = "Run the plugin doctor (Tier-1 static lint) on a plugin folder."

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="Installed plugin slug to check.")
        parser.add_argument(
            "--path", help="Path to a local plugin folder (its name is the slug)."
        )

    def handle(self, *args, **options):
        slug = options.get("slug")
        path = options.get("path")
        if bool(slug) == bool(path):
            raise CommandError("Provide exactly one of <slug> or --path.")

        folder = Path(path) if path else Path(settings.PLUGINS_DIR) / slug
        if not folder.is_dir():
            raise CommandError(f"Not a directory: {folder}")

        report = run_doctor(folder)

        style = {"pass": self.style.SUCCESS, "warn": self.style.WARNING, "fail": self.style.ERROR}
        self.stdout.write(f"Plugin doctor — {report.slug}")
        for f in report.findings:
            label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[f.severity]
            self.stdout.write("  " + style[f.severity](f"[{label}] ") + f"{f.rule}: {f.message}")

        summary = f"{report.fail_count} fail, {report.warn_count} warn"
        if report.ok:
            self.stdout.write(self.style.SUCCESS(f"\nOK — activation allowed ({summary})."))
        else:
            self.stdout.write(self.style.ERROR(f"\nBLOCKED — activation refused ({summary})."))
            raise SystemExit(1)
