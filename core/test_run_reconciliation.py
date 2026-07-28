"""
Run-lifecycle safety regressions — the long-run orphan bug (2026-07).

The failure this guards against, observed live: a run outliving the broker's
cluster-wide `retry` window had its task re-delivered to a second worker;
that worker's execute_run() saw status != PENDING, returned early — and its
`finally` stamped ended_at, cleared the pid and overwrote stdout/stderr on the
LIVE row, leaving the run stuck at 'running' forever with its output lost
(signature: ended_at - started_at == retry exactly, pid=None, empty stdout).
Separately, a worker killed while busy (container stop SIGKILLs busy workers
by design — entrypoint.sh) left runs at 'running' with no reconciler to catch
them: six such rows accumulated on a real instance.

Covers:
- execute_run duplicate delivery is a TRUE no-op (the atomic PENDING claim);
- django-q2's TimeoutException (a SystemExit subclass, invisible to
  `except Exception`) finalizes the run and kills the process tree;
- an unexpected executor exception kills the abandoned process tree;
- Run.reconcile_stale(): RUNNING past timeout+grace → FAILED; PENDING >24h
  with workers alive → FAILED; live/queued runs untouched;
- the worker heartbeat task and the runs list page invoke the reconciler;
- the boot-time Q_CLUSTER retry floor covers the fleet's largest script
  timeout;
- the script form warns (non-blocking) when a timeout outruns the running
  workers' recovery window.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_q.exceptions import TimeoutException

from core.executor import execute_run
from core.executor_backends.base import RunHandle, RunResult
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.tasks import worker_heartbeat_task


def _script(timeout_seconds=60, **kwargs):
    env = Environment.objects.create(name="e-reconcile", path="p-reconcile")
    return Script.objects.create(
        name="s-reconcile",
        code="print('x')",
        environment=env,
        timeout_seconds=timeout_seconds,
        **kwargs,
    )


def _run(script, status, started_ago=None, **kwargs):
    run = Run.objects.create(script=script, status=status, **kwargs)
    if started_ago is not None:
        Run.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - started_ago
        )
        run.refresh_from_db()
    return run


class _FakeBackend:
    """Backend double: records kill() calls; wait() behavior injectable."""

    def __init__(self, wait_effect):
        self._wait_effect = wait_effect
        self.kill_calls = []

    def start(self, spec):
        return RunHandle(pid=4242, native=None)

    def wait(self, handle, timeout):
        if isinstance(self._wait_effect, BaseException):
            raise self._wait_effect
        return self._wait_effect

    def kill(self, handle):
        self.kill_calls.append(handle.pid)


class _FakeDecision:
    sandbox = False
    mandatory = False
    reason = "test"


class DuplicateDeliveryTests(TestCase):
    """The core regression: a re-delivered task must not touch the live row."""

    def test_duplicate_delivery_is_a_true_noop(self):
        script = _script(timeout_seconds=7200)
        run = _run(
            script,
            Run.Status.RUNNING,
            started_ago=timedelta(seconds=90),
            pid=1234,
        )

        execute_run(run)  # second delivery: status is not PENDING

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)
        self.assertIsNone(run.ended_at)  # the old bug stamped this
        self.assertEqual(run.pid, 1234)  # ...and cleared this (breaking stop)
        self.assertEqual(run.stdout, "")

    def test_finished_run_is_not_reexecuted_or_touched(self):
        script = _script()
        run = _run(script, Run.Status.SUCCESS, started_ago=timedelta(minutes=5))
        Run.objects.filter(pk=run.pk).update(
            stdout="the output", exit_code=0, ended_at=timezone.now()
        )

        execute_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertEqual(run.stdout, "the output")

    def test_pending_claim_is_atomic(self):
        # A run claimed between the caller's fetch and execute_run's claim is
        # someone else's; this delivery must walk away without executing.
        script = _script()
        run = _run(script, Run.Status.PENDING)
        stale_copy = Run.objects.select_related("script").get(pk=run.pk)
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING, started_at=timezone.now()
        )

        execute_run(stale_copy)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)
        self.assertIsNone(run.ended_at)


class AbandonedProcessTests(TestCase):
    """Exceptions that interrupt wait() must kill the spawned tree."""

    def _execute_with_backend(self, backend, run):
        with mock.patch(
            "core.executor._validate_environment", return_value="python"
        ), mock.patch(
            "core.executor._select_backend_for_run",
            return_value=(backend, _FakeDecision()),
        ), mock.patch(
            "core.executor.resolve_secrets_for_run", return_value={}
        ):
            execute_run(run)

    def test_worker_timeout_systemexit_finalizes_and_kills(self):
        # django-q2's TimeoutException subclasses SystemExit — NOT Exception —
        # so the catch-all never sees it. Unhandled, it left the row half
        # stamped at 'running' and the subprocess alive and detached.
        script = _script(timeout_seconds=60)
        run = _run(script, Run.Status.PENDING)
        backend = _FakeBackend(TimeoutException("task exceeded timeout"))

        with self.assertRaises(SystemExit):
            self._execute_with_backend(backend, run)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.TIMEOUT)
        self.assertIsNotNone(run.ended_at)
        self.assertIsNone(run.pid)
        self.assertEqual(backend.kill_calls, [4242])

    def test_unexpected_exception_kills_abandoned_tree(self):
        script = _script()
        run = _run(script, Run.Status.PENDING)
        backend = _FakeBackend(RuntimeError("boom"))

        self._execute_with_backend(backend, run)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("Unexpected executor error", run.stderr)
        self.assertEqual(backend.kill_calls, [4242])

    def test_clean_completion_does_not_kill(self):
        script = _script()
        run = _run(script, Run.Status.PENDING)
        backend = _FakeBackend(RunResult(exit_code=0, stdout="ok", stderr=""))

        self._execute_with_backend(backend, run)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertEqual(run.stdout, "ok")
        self.assertEqual(backend.kill_calls, [])


class ReconcileStaleTests(TestCase):
    def _fresh_heartbeat(self):
        gs = GlobalSettings.get_settings()
        gs.worker_heartbeat_at = timezone.now()
        gs.save(update_fields=["worker_heartbeat_at"])

    def test_running_past_timeout_plus_grace_is_failed(self):
        script = _script(timeout_seconds=60)
        run = _run(
            script,
            Run.Status.RUNNING,
            started_ago=timedelta(seconds=60 + Run.RECONCILE_GRACE_SECONDS + 5),
            pid=999,
        )

        self.assertEqual(Run.reconcile_stale(), 1)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("RECONCILED", run.stderr)
        self.assertIsNotNone(run.ended_at)
        self.assertIsNone(run.pid)

    def test_running_within_its_own_timeout_is_left_alone(self):
        # A 2h script 30 minutes in is healthy — per-run deadlines, not a
        # global staleness window.
        script = _script(timeout_seconds=7200)
        run = _run(script, Run.Status.RUNNING, started_ago=timedelta(minutes=30))

        self.assertEqual(Run.reconcile_stale(), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)

    def test_running_within_grace_after_timeout_is_left_alone(self):
        script = _script(timeout_seconds=60)
        run = _run(script, Run.Status.RUNNING, started_ago=timedelta(seconds=120))

        self.assertEqual(Run.reconcile_stale(), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)

    def test_old_pending_failed_only_while_workers_alive(self):
        script = _script()
        run = _run(script, Run.Status.PENDING)
        Run.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )

        # Workers down (no heartbeat): "queued" is still the truth.
        self.assertEqual(Run.reconcile_stale(), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.PENDING)

        self._fresh_heartbeat()
        self.assertEqual(Run.reconcile_stale(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("RECONCILED", run.stderr)

    def test_recent_pending_is_left_alone(self):
        self._fresh_heartbeat()
        script = _script()
        run = _run(script, Run.Status.PENDING)

        self.assertEqual(Run.reconcile_stale(), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.PENDING)

    def test_heartbeat_task_reconciles(self):
        script = _script(timeout_seconds=60)
        run = _run(
            script,
            Run.Status.RUNNING,
            started_ago=timedelta(seconds=60 + Run.RECONCILE_GRACE_SECONDS + 5),
        )

        result = worker_heartbeat_task()

        self.assertTrue(result["success"])
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)


class RunListViewReconcilesTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="runs@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)

    def test_list_view_heals_stuck_runs(self):
        script = _script(timeout_seconds=60)
        script.workspace = self.ws
        script.save(update_fields=["workspace"])
        run = _run(
            script,
            Run.Status.RUNNING,
            started_ago=timedelta(seconds=60 + Run.RECONCILE_GRACE_SECONDS + 5),
            workspace=self.ws,
        )

        resp = self.client.get(reverse("cpanel:run_list"))

        self.assertEqual(resp.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)


class EffectiveQConfigTests(TestCase):
    """The shared invariant/floor computation (pyrunner.qconfig)."""

    BASE = {"workers": 2, "timeout": 600, "retry": 660, "queue_limit": 20}

    def test_db_values_override_env_defaults(self):
        from pyrunner.qconfig import compute_effective_q_config

        config = compute_effective_q_config(
            self.BASE, db_values=(4, 7800, 7860, 30), platform_nt=False
        )
        self.assertEqual(
            (config["workers"], config["timeout"], config["retry"], config["queue_limit"]),
            (4, 7800, 7860, 30),
        )

    def test_retry_invariant_keeps_retry_above_timeout(self):
        from pyrunner.qconfig import compute_effective_q_config

        config = compute_effective_q_config(
            self.BASE, db_values=(2, 7800, 660, 20), platform_nt=False
        )
        self.assertEqual(config["retry"], 7860)

    def test_retry_floors_above_fleet_max_script_timeout(self):
        from pyrunner.qconfig import compute_effective_q_config

        config = compute_effective_q_config(
            self.BASE, max_script_timeout=7200, platform_nt=False
        )
        self.assertEqual(config["retry"], 7200 + 120)

    def test_windows_forces_no_timeout_but_keeps_floor(self):
        from pyrunner.qconfig import compute_effective_q_config

        config = compute_effective_q_config(
            self.BASE, db_values=(2, 7800, 660, 20),
            max_script_timeout=90000, platform_nt=True,
        )
        self.assertEqual(config["timeout"], 0)
        self.assertEqual(config["retry"], 90000 + 120)  # floor beats the 86400

    def test_settings_module_pass_uses_env_defaults(self):
        from pyrunner.settings import _get_q_cluster_config

        config = _get_q_cluster_config()
        # DB is deliberately not read at settings import (apps not ready);
        # platform default retry only: 660 enforced-timeout, 86400 on Windows.
        self.assertIn(config["retry"], (660, 86400))


class ApplyDbWorkerSettingsTests(TestCase):
    """pyrunner_qcluster's apply path patches django-q's Conf from the DB."""

    def setUp(self):
        from django.conf import settings as dj_settings
        from django_q.conf import Conf

        self._saved_conf = (Conf.WORKERS, Conf.TIMEOUT, Conf.RETRY, Conf.QUEUE_LIMIT)
        self._saved_q = dict(dj_settings.Q_CLUSTER)

        def _restore():
            Conf.WORKERS, Conf.TIMEOUT, Conf.RETRY, Conf.QUEUE_LIMIT = self._saved_conf
            dj_settings.Q_CLUSTER.clear()
            dj_settings.Q_CLUSTER.update(self._saved_q)

        self.addCleanup(_restore)

    def test_conf_patched_from_db_row_and_fleet_floor(self):
        from django_q.conf import Conf

        from core.services.worker_config import apply_db_worker_settings

        gs = GlobalSettings.get_settings()
        gs.q_workers, gs.q_timeout, gs.q_retry, gs.q_queue_limit = 4, 0, 660, 30
        gs.save()
        _script(timeout_seconds=90000)

        config = apply_db_worker_settings()

        # timeout 0 → retry pushed to 86400, then floored by the 90000s script;
        # identical on every platform (0 stays 0 under the NT forcing too).
        self.assertEqual(Conf.WORKERS, 4)
        self.assertEqual(Conf.TIMEOUT, 0)
        self.assertEqual(Conf.RETRY, 90000 + 120)
        self.assertEqual(Conf.QUEUE_LIMIT, 30)
        self.assertEqual(config["retry"], 90000 + 120)

    def test_wrapper_command_is_registered(self):
        from django.core.management import get_commands

        self.assertEqual(get_commands().get("pyrunner_qcluster"), "core")


class TimeoutWarningTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY

        cache.delete(EFFECTIVE_RETRY_CACHE_KEY)
        self.addCleanup(cache.delete, EFFECTIVE_RETRY_CACHE_KEY)

    @override_settings(Q_CLUSTER={"retry": 660})
    def test_warns_when_timeout_outruns_window(self):
        from core.views.scripts import _warn_if_timeout_outruns_workers

        script = _script(timeout_seconds=7200)
        with mock.patch("core.views.scripts.messages") as msg:
            _warn_if_timeout_outruns_workers(mock.Mock(), script)
            msg.warning.assert_called_once()

    @override_settings(Q_CLUSTER={"retry": 7860})
    def test_silent_when_window_covers_timeout(self):
        from core.views.scripts import _warn_if_timeout_outruns_workers

        script = _script(timeout_seconds=7200)
        with mock.patch("core.views.scripts.messages") as msg:
            _warn_if_timeout_outruns_workers(mock.Mock(), script)
            msg.warning.assert_not_called()

    @override_settings(Q_CLUSTER={"retry": 660})
    def test_running_cluster_stamp_beats_env_default(self):
        # The heartbeat's cache stamp is the RUNNING cluster's window; it must
        # win over this process's env-default settings.Q_CLUSTER.
        from django.core.cache import cache

        from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY
        from core.views.scripts import _warn_if_timeout_outruns_workers

        cache.set(EFFECTIVE_RETRY_CACHE_KEY, 7860, None)
        script = _script(timeout_seconds=7200)
        with mock.patch("core.views.scripts.messages") as msg:
            _warn_if_timeout_outruns_workers(mock.Mock(), script)
            msg.warning.assert_not_called()
