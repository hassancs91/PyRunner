"""
Management command to restart qcluster workers.

This command signals the entrypoint script to restart the worker process.
It's designed to be called from the web UI or manually.
"""

import os
import signal
import time
import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import GlobalSettings

logger = logging.getLogger(__name__)

PID_FILE = "/tmp/qcluster.pid"


class Command(BaseCommand):
    help = "Restart the django-q2 worker process"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force restart even if workers appear healthy",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="Seconds to wait for workers to restart (default: 30)",
        )

    def handle(self, *args, **options):
        timeout = options["timeout"]

        # Check if PID file exists
        if not os.path.exists(PID_FILE):
            raise CommandError(
                f"Worker PID file not found at {PID_FILE}. "
                "Workers may not be running or not started via entrypoint.sh"
            )

        # Read current worker PID
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except (ValueError, IOError) as e:
            raise CommandError(f"Failed to read PID file: {e}")

        # Verify process exists
        try:
            os.kill(old_pid, 0)
            self.stdout.write(f"Current worker PID: {old_pid}")
        except OSError:
            self.stdout.write(
                self.style.WARNING(
                    f"Worker process {old_pid} not found. May already be restarting."
                )
            )

        # Record the restart request time
        restart_requested_at = timezone.now()

        # Try to send SIGUSR1 to PID 1 (entrypoint process in Docker)
        restart_sent = False
        try:
            os.kill(1, signal.SIGUSR1)
            self.stdout.write(self.style.SUCCESS("Restart signal sent to entrypoint"))
            restart_sent = True
        except OSError as e:
            # Fallback: try to kill the worker directly (non-Docker case)
            self.stdout.write(
                self.style.WARNING(f"Could not signal PID 1: {e}")
            )
            self.stdout.write("Attempting direct worker termination...")
            try:
                os.kill(old_pid, signal.SIGTERM)
                self.stdout.write(
                    self.style.SUCCESS(f"SIGTERM sent to worker {old_pid}")
                )
                restart_sent = True
            except OSError as e2:
                raise CommandError(f"Failed to stop worker: {e2}")

        if not restart_sent:
            raise CommandError("Failed to send restart signal")

        # Wait for restart to complete
        self.stdout.write(f"Waiting up to {timeout}s for workers to restart...")

        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(2)

            # Check if PID changed (new worker started)
            if os.path.exists(PID_FILE):
                try:
                    with open(PID_FILE, "r") as f:
                        new_pid = int(f.read().strip())
                    if new_pid != old_pid:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"New worker started with PID {new_pid}"
                            )
                        )
                        return
                except (ValueError, IOError):
                    pass

            # Check if new heartbeat received after restart
            settings = GlobalSettings.get_settings()
            settings.refresh_from_db()
            if (
                settings.worker_heartbeat_at
                and settings.worker_heartbeat_at > restart_requested_at
            ):
                self.stdout.write(
                    self.style.SUCCESS("Workers restarted successfully!")
                )
                return

        self.stdout.write(
            self.style.WARNING(
                f"Timeout waiting for restart confirmation. "
                "Check worker logs for status."
            )
        )
