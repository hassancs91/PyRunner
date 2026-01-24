"""
Management command for initial setup.

This command is designed to be used as a Docker entrypoint or for
scripted deployments where browser-based setup is not available.
"""

from django.core.management.base import BaseCommand

from core.services.setup_service import SetupService


class Command(BaseCommand):
    help = "Run initial setup (migrations + default environment)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-env",
            action="store_true",
            help="Skip creating the default Python environment",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run setup even if already completed",
        )

    def handle(self, *args, **options):
        skip_env = options["skip_env"]
        force = options["force"]

        # Check if setup is already complete
        if not force and not SetupService.is_setup_needed():
            self.stdout.write(
                self.style.SUCCESS("Setup already completed. Use --force to re-run.")
            )
            return

        self.stdout.write("Starting PyRunner setup...\n")

        # Run migrations
        self.stdout.write("Running database migrations...")
        success, message = SetupService.run_migrations()
        if success:
            self.stdout.write(self.style.SUCCESS("  Migrations complete"))
        else:
            self.stdout.write(self.style.ERROR(f"  Migration failed: {message}"))
            return

        # Create default environment
        if not skip_env:
            self.stdout.write("Creating default Python environment...")
            success, message = SetupService.create_default_environment()
            if success:
                self.stdout.write(self.style.SUCCESS(f"  {message}"))
            else:
                self.stdout.write(self.style.ERROR(f"  Failed: {message}"))
                return
        else:
            self.stdout.write(
                self.style.WARNING("  Skipping default environment (--skip-env)")
            )

        # Mark setup as complete
        SetupService.complete_setup()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Setup completed successfully!"))
        self.stdout.write("")
        self.stdout.write("Next steps:")
        self.stdout.write("  1. Start the task worker: python manage.py qcluster")
        self.stdout.write("  2. Start the web server: python manage.py runserver")
        self.stdout.write("  3. Log in - the first user becomes the administrator")
