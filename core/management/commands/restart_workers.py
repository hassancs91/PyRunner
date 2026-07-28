"""
Management command to restart qcluster workers.

Sends SIGTERM to the worker recorded in the PID file; the entrypoint's monitor
loop (entrypoint.sh) notices the death within ~5s and starts a fresh worker.
Success is confirmed ONLY by the PID file changing to a new, live PID — a
worker heartbeat is NOT proof (the old worker can still heartbeat while dying,
which is exactly how this command used to report false successes).
"""

import logging
import os
import signal
import time

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

PID_FILE = "/tmp/qcluster.pid"


class Command(BaseCommand):
    help = "Restart the django-q2 worker process"

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="Seconds to wait for workers to restart (default: 30)",
        )

    def handle(self, *args, **options):
        timeout = options["timeout"]

        if not os.path.exists(PID_FILE):
            raise CommandError(
                f"Worker PID file not found at {PID_FILE}. "
                "Workers may not be running or not started via entrypoint.sh"
            )

        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except (ValueError, IOError) as e:
            raise CommandError(f"Failed to read PID file: {e}")

        # Stop the current worker; the entrypoint's monitor loop notices the
        # death and starts a replacement (rewriting the PID file).
        try:
            os.kill(old_pid, signal.SIGTERM)
            self.stdout.write(f"SIGTERM sent to worker {old_pid}")
        except OSError:
            self.stdout.write(
                self.style.WARNING(
                    f"Worker process {old_pid} not found — waiting for the "
                    "monitor to start a fresh one."
                )
            )

        self.stdout.write(f"Waiting up to {timeout}s for a new worker...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(1)
            new_pid = self._read_pid()
            if new_pid and new_pid != old_pid and self._alive(new_pid):
                self.stdout.write(
                    self.style.SUCCESS(f"New worker started with PID {new_pid}")
                )
                return

        raise CommandError(
            f"No new worker appeared within {timeout}s. If PyRunner runs via "
            "entrypoint.sh (Docker), check the container logs; otherwise "
            "nothing auto-restarts the worker — start it manually with "
            "`manage.py pyrunner_qcluster`."
        )

    @staticmethod
    def _read_pid():
        try:
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
