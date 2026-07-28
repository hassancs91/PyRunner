"""
Start the django-q cluster with PyRunner's DB-backed Workers settings applied.

This wraps django-q2's ``qcluster`` command because the values saved on
Settings → Workers cannot take effect any other way: Q_CLUSTER is computed at
settings-module import, inside ``django.setup()``, before the app registry —
and thus the database — exists, in every process. By the time a management
command runs, apps are ready, so this command reads the GlobalSettings row
(plus the fleet's largest script timeout, for the retry safety floor), patches
``django_q.conf.Conf``, and only then starts the cluster.

A plain ``manage.py qcluster`` still works but runs on the env defaults only —
production (entrypoint.sh) and the restart-workers flow use this command.
"""

from django_q.management.commands.qcluster import Command as QClusterCommand

from core.services.worker_config import apply_db_worker_settings


class Command(QClusterCommand):
    help = (
        "Starts a Django Q Cluster with PyRunner's DB-backed worker settings "
        "(Settings → Workers) applied."
    )

    def handle(self, *args, **options):
        try:
            config = apply_db_worker_settings()
            self.stdout.write(
                "Worker settings applied: workers={workers} timeout={timeout} "
                "retry={retry} queue_limit={queue_limit}".format(**config)
            )
        except Exception as e:
            # A fresh install (migrations not run yet) must still boot a
            # cluster; env defaults are the safe fallback and the next restart
            # picks the DB values up.
            self.stderr.write(
                f"Could not apply DB worker settings ({e}); "
                "starting with environment defaults."
            )
        super().handle(*args, **options)
