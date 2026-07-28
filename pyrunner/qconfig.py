"""Pure computation of the effective django-q cluster configuration.

Shared by two callers so the safety invariants can never drift apart:

- ``pyrunner/settings.py`` builds the env-default ``Q_CLUSTER`` at settings
  import — which happens inside ``django.setup()``, before the app registry
  (and therefore the ORM) exists, so the DB-backed Workers settings CANNOT be
  read there, in any process.
- core's ``pyrunner_qcluster`` command re-runs this with the DB values and the
  fleet's largest script timeout once apps are ready, and patches
  ``django_q.conf.Conf`` before the cluster starts. That command is the one
  real apply-path for "Settings → Workers (restart to apply)".

This module must stay import-light (stdlib only): settings.py imports it.
"""

import os


def compute_effective_q_config(
    base: dict,
    db_values: tuple | None = None,
    max_script_timeout: int = 0,
    platform_nt: bool | None = None,
) -> dict:
    """Return a new config dict with overrides and safety invariants applied.

    ``db_values`` is ``(workers, timeout, retry, queue_limit)`` from the
    GlobalSettings row, or None when the DB is unreachable (settings import).
    ``max_script_timeout`` is the fleet's largest ``Script.timeout_seconds``.
    ``platform_nt`` is injectable for tests; defaults to the real platform.
    """
    if platform_nt is None:
        platform_nt = os.name == "nt"
    config = dict(base)

    if db_values:
        workers, timeout, retry, queue_limit = db_values
        config["workers"] = workers or config["workers"]
        # 0 is a meaningful timeout ("none") — only None falls back.
        config["timeout"] = timeout if timeout is not None else config["timeout"]
        config["retry"] = retry or config["retry"]
        config["queue_limit"] = queue_limit or config["queue_limit"]

    # On Windows a task timeout is unsupported — force 0 regardless of source.
    if platform_nt:
        config["timeout"] = 0

    # Defensive backstop: django-q2 re-queues a task that hasn't finished within
    # `retry` seconds, running it AGAIN on another worker. If retry is smaller
    # than a task's real duration the task is restarted forever (each restart
    # never finishes in time either) — a death spiral that, for package installs,
    # also resets the operation to "running" repeatedly and makes the packages
    # page poll endlessly.
    #
    # When timeout is enforced, keep retry above it so a task is killed before it
    # can be re-queued. When timeout == 0 (no timeout — e.g. Windows, where
    # django-q2 can't enforce one) there is no upper bound on task duration, so a
    # finite retry WILL eventually re-queue long-running tasks. Push retry far out
    # so legitimately slow tasks (big pip installs, long scripts) complete first;
    # genuinely dead tasks are surfaced via Run.reconcile_stale/manual retry.
    if config["timeout"]:
        if config["retry"] <= config["timeout"]:
            config["retry"] = config["timeout"] + 60
    else:
        config["retry"] = 86400  # 24h — effectively "don't auto re-queue"

    # Safety floor: the broker's re-delivery window (`retry`) is CLUSTER-WIDE —
    # the per-task timeout queue_script_run passes does NOT extend the broker
    # lock — so any script allowed a timeout above `retry` would have its task
    # re-delivered to a second worker mid-run. Float retry above every declared
    # script timeout (+60s task cushion +60s margin). Never lowers a configured
    # value. Recomputed at every worker start; a script timeout raised past the
    # old fleet max is covered from the next restart (plugin installs already
    # trigger one; the script form warns — see views/scripts.py).
    if max_script_timeout:
        floor = max_script_timeout + 120
        if config["retry"] < floor:
            config["retry"] = floor

    return config
