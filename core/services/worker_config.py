"""Apply the DB-backed Workers settings to a running process's django-q Conf.

Q_CLUSTER is computed at settings-module import — inside ``django.setup()``,
before the app registry exists — so the values saved on Settings → Workers can
never be read there (see ``pyrunner.settings._get_q_cluster_config``). This
module is the other half: once apps ARE ready, ``apply_db_worker_settings``
recomputes the effective config from the GlobalSettings row plus the fleet's
largest script timeout, and patches ``django_q.conf.Conf`` in place.

Called by the ``pyrunner_qcluster`` command in the cluster's parent process
before ``Cluster().start()``; on Linux the sentinel/pusher/workers inherit the
patched Conf via fork. (Windows spawns fresh interpreters that re-import the
env defaults — but an enforced task timeout is unsupported there anyway, so
the DB overrides only ever affected cosmetics on NT.)
"""

import logging

from django.conf import settings as django_settings
from django.db.models import Max
from django_q.conf import Conf

from pyrunner.qconfig import compute_effective_q_config

logger = logging.getLogger(__name__)

# Where the cluster advertises the retry window it actually booted with, so the
# web process (which only has the env defaults in settings.Q_CLUSTER) can warn
# accurately when a script's timeout outruns the RUNNING workers. Stamped every
# minute by the worker heartbeat; shared via the cross-process cache backend.
EFFECTIVE_RETRY_CACHE_KEY = "qcluster_effective_retry"


def apply_db_worker_settings() -> dict:
    """Patch django-q's Conf from the DB; return the effective config."""
    from core.models import GlobalSettings, Script

    gs = GlobalSettings.get_settings()
    from core.services.environment_service import EnvironmentService

    # Package operations ride the same cluster, so the re-delivery window must
    # also outlast the longest pip task (bulk install) or the broker hands a
    # still-running install to a second worker in the same venv.
    max_script_timeout = max(
        Script.objects.aggregate(m=Max("timeout_seconds"))["m"] or 0,
        EnvironmentService.max_task_timeout(),
    )
    config = compute_effective_q_config(
        django_settings.Q_CLUSTER,
        db_values=(gs.q_workers, gs.q_timeout, gs.q_retry, gs.q_queue_limit),
        max_script_timeout=max_script_timeout,
    )

    Conf.WORKERS = config["workers"]
    Conf.TIMEOUT = config["timeout"]
    Conf.RETRY = config["retry"]
    Conf.QUEUE_LIMIT = config["queue_limit"]
    # Keep this process's settings dict truthful for anything that reads it.
    django_settings.Q_CLUSTER.update(
        {k: config[k] for k in ("workers", "timeout", "retry", "queue_limit")}
    )

    logger.info(
        "Applied DB worker settings: workers=%s timeout=%s retry=%s "
        "queue_limit=%s (fleet max script timeout: %ss)",
        config["workers"],
        config["timeout"],
        config["retry"],
        config["queue_limit"],
        max_script_timeout,
    )
    return config
