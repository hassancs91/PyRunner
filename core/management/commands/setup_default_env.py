"""
Management command to set up the default Python environment.

Creates a virtual environment at data/environments/default/ and registers it
in the database as the default environment for script execution.
"""

import os
import subprocess
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Environment


class Command(BaseCommand):
    help = "Create and register the default Python environment"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Recreate the environment even if it already exists",
        )

    def handle(self, *args, **options):
        force = options["force"]

        # Check if default environment already exists in DB
        existing = Environment.objects.filter(is_default=True).first()
        if existing and not force:
            self.stdout.write(
                self.style.WARNING(
                    f'Default environment already exists: "{existing.name}" '
                    f"at {existing.get_full_path()}"
                )
            )
            self.stdout.write("Use --force to recreate it.")
            return

        # Define paths
        env_path = "default"
        full_path = os.path.join(settings.ENVIRONMENTS_ROOT, env_path)

        # Check if directory already exists
        if os.path.exists(full_path):
            if not force:
                self.stdout.write(
                    self.style.WARNING(
                        f"Environment directory already exists: {full_path}"
                    )
                )
                self.stdout.write("Use --force to recreate it.")
                return
            else:
                self.stdout.write(f"Removing existing environment at {full_path}...")
                import shutil

                shutil.rmtree(full_path)

        # Create the virtual environment
        self.stdout.write(f"Creating virtual environment at {full_path}...")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", full_path],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise CommandError(f"Failed to create virtual environment: {e.stderr}")

        # Get Python version from the new venv
        if os.name == "nt":
            python_path = os.path.join(full_path, "Scripts", "python.exe")
        else:
            python_path = os.path.join(full_path, "bin", "python")

        try:
            result = subprocess.run(
                [python_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            python_version = result.stdout.strip().replace("Python ", "")
        except subprocess.CalledProcessError:
            python_version = "unknown"

        # Create or update the Environment record
        if existing and force:
            # Update existing record
            existing.path = env_path
            existing.python_version = python_version
            existing.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f'Updated default environment "{existing.name}" '
                    f"(Python {python_version})"
                )
            )
        else:
            # Create new record
            Environment.objects.create(
                name="Default Environment",
                description="Auto-created default Python environment",
                path=env_path,
                python_version=python_version,
                is_default=True,
                is_active=True,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created default environment (Python {python_version})"
                )
            )

        self.stdout.write(f"Environment path: {full_path}")
        self.stdout.write(f"Python executable: {python_path}")
