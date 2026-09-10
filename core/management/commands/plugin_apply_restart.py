"""
plugin_apply_restart — trigger a controlled full restart so the web + worker
processes re-import the new active plugin set.

The mechanism is deliberately simple and safe: send SIGTERM to PID 1 (the
container's gunicorn/entrypoint). The container exits cleanly and the platform's
`restart: unless-stopped` + healthcheck bring it back — re-running the entrypoint,
which preflights every active plugin in isolation BEFORE gunicorn starts. Because
activation already passed preflight, the restart always boots clean.

Run detached (the management UI does this) with PYRUNNER_DISABLE_PLUGINS=1 so the
trigger process itself never imports plugin code. Outside a container it is a
no-op with a message (dev: restart manually / runserver auto-reloads code).
"""

import os
import signal
import time

from django.core.management.base import BaseCommand


def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("PYRUNNER_IN_CONTAINER") == "1"


class Command(BaseCommand):
    help = "Trigger a controlled container restart (SIGTERM to PID 1)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Seconds to wait before signalling (lets the HTTP response flush).",
        )

    def handle(self, *args, **options):
        time.sleep(options["delay"])

        if not _in_container():
            self.stdout.write(
                "Not running under a container init — restart PyRun manually "
                "to apply plugin changes."
            )
            return

        try:
            os.kill(1, signal.SIGTERM)
            self.stdout.write("Sent SIGTERM to PID 1 — container will restart.")
        except Exception as exc:  # pragma: no cover - best effort
            self.stderr.write(f"Could not signal PID 1: {exc}")
