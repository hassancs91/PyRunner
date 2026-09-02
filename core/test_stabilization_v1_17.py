"""
Regression tests for the v1.17 stabilization pass.

Each class pins one of the fixes in docs/PLAN_v1_17.md (1C/1D): reconciled runs
notify, hardened pip tasks, orphan venv / plugin folder recovery, the
``min_pyrunner`` gate, the ensurepip probe, and the admin-slug restart notice.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    PackageOperation,
    Plugin,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.environment_service import EnvironmentService
from core.services.plugin_service import PluginInstallError, PluginService
from core.tasks import worker_heartbeat_task

SEND = "core.services.notification_service.NotificationService.send_notification"


def _script(timeout_seconds=60):
    env = Environment.objects.create(name="e-stab", path="p-stab")
    return Script.objects.create(
        name="s-stab", code="print('x')", environment=env, timeout_seconds=timeout_seconds
    )


def _stale_running(script):
    run = Run.objects.create(script=script, status=Run.Status.RUNNING, pid=999)
    Run.objects.filter(pk=run.pk).update(
        started_at=timezone.now()
        - timedelta(seconds=script.timeout_seconds + Run.RECONCILE_GRACE_SECONDS + 5)
    )
    run.refresh_from_db()
    return run


# ---------------------------------------------------------------------------
# 1.2 — reconciled runs send the normal failure notification
# ---------------------------------------------------------------------------


class ReconcileNotifyTests(TestCase):
    def test_reconciled_running_run_is_notified_as_failed(self):
        run = _stale_running(_script())

        with mock.patch(SEND) as send:
            self.assertEqual(Run.reconcile_stale(), 1)

        send.assert_called_once()
        notified = send.call_args[0][0]
        self.assertEqual(notified.pk, run.pk)
        self.assertEqual(notified.status, Run.Status.FAILED)
        self.assertIn("RECONCILED", notified.stderr)

    def test_stale_pending_run_is_notified_once_workers_are_alive(self):
        run = Run.objects.create(script=_script(), status=Run.Status.PENDING)
        Run.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=25))
        gs = GlobalSettings.get_settings()
        gs.worker_heartbeat_at = timezone.now()
        gs.save(update_fields=["worker_heartbeat_at"])

        with mock.patch(SEND) as send:
            self.assertEqual(Run.reconcile_stale(), 1)

        self.assertEqual([c[0][0].pk for c in send.call_args_list], [run.pk])

    def test_healthy_runs_send_nothing(self):
        script = _script(timeout_seconds=7200)
        Run.objects.create(script=script, status=Run.Status.RUNNING, started_at=timezone.now())

        with mock.patch(SEND) as send:
            self.assertEqual(Run.reconcile_stale(), 0)

        send.assert_not_called()

    def test_notification_failure_never_breaks_the_reconciler(self):
        run = _stale_running(_script())

        with mock.patch(SEND, side_effect=RuntimeError("smtp down")):
            self.assertEqual(Run.reconcile_stale(), 1)

        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)


# ---------------------------------------------------------------------------
# 1.1 — pip tasks: per-task timeouts above the pip cap, hardened subprocess,
#       package reconcile on the heartbeat
# ---------------------------------------------------------------------------


class PipTaskHardeningTests(TestCase):
    def test_task_timeouts_leave_room_for_pip_to_die_first(self):
        for op, cap in (
            (PackageOperation.Operation.INSTALL, 300),
            (PackageOperation.Operation.UNINSTALL, 120),
            (PackageOperation.Operation.BULK_INSTALL, 600),
        ):
            self.assertEqual(
                EnvironmentService.task_timeout(op), cap + EnvironmentService.TASK_TIMEOUT_GRACE
            )
        self.assertEqual(EnvironmentService.max_task_timeout(), 660)
        self.assertGreater(
            PackageOperation.STALE_AFTER.total_seconds(), EnvironmentService.max_task_timeout()
        )

    def test_bulk_install_runs_through_the_hardened_runner(self):
        env = Environment.objects.create(name="e-pip", path="p-pip")
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with (
            mock.patch("core.services.environment_service.os.path.isfile", return_value=True),
            mock.patch.object(EnvironmentService, "_run_pip", return_value=completed) as run_pip,
        ):
            ok, out, _err = EnvironmentService.install_requirements(env, "requests\n")

        self.assertTrue(ok)
        self.assertEqual(out, "ok")
        self.assertEqual(run_pip.call_args.kwargs["timeout"], 600)

    def test_run_pip_closes_stdin_and_forbids_prompts(self):
        proc = mock.Mock(pid=4242, returncode=0)
        proc.communicate.return_value = ("out", "err")
        with mock.patch(
            "core.services.environment_service.subprocess.Popen", return_value=proc
        ) as popen:
            result = EnvironmentService._run_pip(["pip", "install", "x"], timeout=5)

        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["PIP_NO_INPUT"], "1")
        self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "out", "err"))

    def test_run_pip_kills_the_whole_tree_on_timeout(self):
        proc = mock.Mock(pid=4242, returncode=None)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("pip", 5), ("", "")]
        with (
            mock.patch("core.services.environment_service.subprocess.Popen", return_value=proc),
            mock.patch("core.executor_backends.local.kill_process_tree") as kill,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                EnvironmentService._run_pip(["pip", "install", "x"], timeout=5)

        kill.assert_called_once_with(4242)

    def test_enqueue_passes_the_task_timeout(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        user = User.objects.create(email="pkg@example.com", is_staff=True, is_superuser=True)
        WorkspaceMembership.ensure(user, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)
        env = Environment.objects.create(name="Default Environment", path="default", is_default=True)

        with mock.patch("core.views.environments.async_task", return_value="task-1") as enqueue:
            self.client.post(
                reverse("cpanel:bulk_install", args=[env.pk]),
                {"requirements": "requests\n"},
            )

        self.assertEqual(enqueue.call_args.kwargs["timeout"], 660)

    def test_heartbeat_reconciles_stale_package_operations(self):
        env = Environment.objects.create(name="e-op", path="p-op")
        op = PackageOperation.objects.create(
            environment=env,
            operation=PackageOperation.Operation.BULK_INSTALL,
            package_spec="requests",
            status=PackageOperation.Status.RUNNING,
        )
        PackageOperation.objects.filter(pk=op.pk).update(
            created_at=timezone.now() - PackageOperation.STALE_AFTER - timedelta(minutes=1)
        )

        self.assertTrue(worker_heartbeat_task()["success"])

        op.refresh_from_db()
        self.assertEqual(op.status, PackageOperation.Status.FAILED)

    def test_retry_floor_outlasts_the_longest_pip_task(self):
        from django.conf import settings as dj_settings
        from django_q.conf import Conf

        saved = (Conf.WORKERS, Conf.TIMEOUT, Conf.RETRY, Conf.QUEUE_LIMIT)
        saved_q = dict(dj_settings.Q_CLUSTER)

        def _restore():
            Conf.WORKERS, Conf.TIMEOUT, Conf.RETRY, Conf.QUEUE_LIMIT = saved
            dj_settings.Q_CLUSTER.clear()
            dj_settings.Q_CLUSTER.update(saved_q)

        self.addCleanup(_restore)
        from core.services.worker_config import apply_db_worker_settings

        # No scripts at all: the floor still has to cover a 660s bulk install.
        config = apply_db_worker_settings()

        self.assertGreaterEqual(config["retry"], EnvironmentService.max_task_timeout() + 120)


# ---------------------------------------------------------------------------
# F3 / F4 — environment creation recovers from orphan folders and only offers
#           interpreters that can build a venv
# ---------------------------------------------------------------------------


class EnvironmentCreateRecoveryTests(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pyrunner-envs-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _venv_ok(self, cmd, **_kwargs):
        os.makedirs(cmd[-1], exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _venv_fails_midway(self, cmd, **_kwargs):
        os.makedirs(cmd[-1], exist_ok=True)
        return subprocess.CompletedProcess(cmd, 1, "", "Error: ensurepip is not available")

    def test_orphan_folder_without_a_row_is_reclaimed(self):
        os.makedirs(os.path.join(self.root, "qa-env"))
        with (
            override_settings(ENVIRONMENTS_ROOT=self.root),
            mock.patch("core.services.environment_service.subprocess.run", side_effect=self._venv_ok),
        ):
            ok, message = EnvironmentService.create_environment(sys.executable, "qa-env")

        self.assertTrue(ok, message)

    def test_folder_referenced_by_an_environment_still_blocks(self):
        Environment.objects.create(name="qa", path="qa-env")
        os.makedirs(os.path.join(self.root, "qa-env"))
        with (
            override_settings(ENVIRONMENTS_ROOT=self.root),
            mock.patch("core.services.environment_service.subprocess.run", side_effect=self._venv_ok),
        ):
            ok, message = EnvironmentService.create_environment(sys.executable, "qa-env")

        self.assertFalse(ok)
        self.assertIn("already exists", message)

    def test_failed_create_leaves_nothing_behind(self):
        with (
            override_settings(ENVIRONMENTS_ROOT=self.root),
            mock.patch(
                "core.services.environment_service.subprocess.run",
                side_effect=self._venv_fails_midway,
            ),
        ):
            ok, message = EnvironmentService.create_environment(sys.executable, "qa-env")

        self.assertFalse(ok)
        self.assertIn("ensurepip", message)
        self.assertFalse(os.path.exists(os.path.join(self.root, "qa-env")))

    def test_interpreters_without_ensurepip_are_not_offered(self):
        stray = {"path": "/usr/bin/python3", "version": "3.11.2", "display": "Python 3.11.2"}
        with (
            mock.patch.object(EnvironmentService, "_discover_via_py_launcher", return_value=[]),
            mock.patch.object(EnvironmentService, "_discover_in_path", return_value=[stray]),
            mock.patch.object(
                EnvironmentService, "_supports_venv", side_effect=lambda p: p != stray["path"]
            ),
        ):
            paths = [p["path"] for p in EnvironmentService.discover_python_versions()]

        self.assertNotIn(stray["path"], paths)
        self.assertIn(sys.executable, paths)

    def test_supports_venv_probe_is_honest(self):
        self.assertTrue(EnvironmentService._supports_venv(sys.executable))
        self.assertFalse(EnvironmentService._supports_venv("/definitely/not/python"))


# ---------------------------------------------------------------------------
# F6 / F2 — min_pyrunner gate at upload + activation; orphan plugin folders
# ---------------------------------------------------------------------------


def _plugin_zip(slug="demo", **manifest_extra):
    manifest = {"slug": slug, "name": "Demo", "version": "1.0.0", **manifest_extra}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        import json

        zf.writestr(f"{slug}/plugin.json", json.dumps(manifest))
        zf.writestr(f"{slug}/__init__.py", "")
        zf.writestr(
            f"{slug}/apps.py",
            "from django.apps import AppConfig\n\nclass PluginAppConfig(AppConfig):\n"
            f"    name = 'plugins.{slug}'\n",
        )
    buf.seek(0)
    return buf


class PluginMinVersionTests(TestCase):
    def setUp(self):
        self.plugins_dir = tempfile.mkdtemp(prefix="pyrunner-plugins-")
        self.addCleanup(shutil.rmtree, self.plugins_dir, True)

    def test_problem_only_when_the_plugin_needs_a_newer_pyrunner(self):
        with mock.patch("pyrunner.version.__version__", "1.17.0"):
            for ok in ("1.16.0", "1.17.0", "1.17", "", None, "garbage"):
                self.assertIsNone(
                    PluginService.min_version_problem({"min_pyrunner": ok}, "demo"), ok
                )
            for too_new in ("1.17.1", "1.18.0", "2.0"):
                self.assertIn(
                    too_new, PluginService.min_version_problem({"min_pyrunner": too_new}, "demo")
                )
        self.assertIsNone(PluginService.min_version_problem(None, "demo"))

    def test_upload_is_refused_when_pyrunner_is_too_old(self):
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.install_from_zip(_plugin_zip(min_pyrunner="99.0.0"))

        self.assertIn("99.0.0", str(caught.exception))
        self.assertFalse(Plugin.objects.filter(slug="demo").exists())
        self.assertFalse((Path(self.plugins_dir) / "demo").exists())

    def test_activation_is_refused_before_the_doctor_runs(self):
        plugin = Plugin.objects.create(
            slug="demo",
            name="Demo",
            version="1.0.0",
            status=Plugin.Status.INSTALLED,
            manifest={"slug": "demo", "min_pyrunner": "99.0.0"},
        )
        with mock.patch("core.services.plugin_doctor.run_doctor") as doctor:
            ok, message = PluginService.activate(plugin)

        self.assertFalse(ok)
        self.assertIn("99.0.0", message)
        doctor.assert_not_called()
        plugin.refresh_from_db()
        self.assertEqual(plugin.status, Plugin.Status.INSTALLED)
        self.assertIn("99.0.0", plugin.error_message)


class OrphanPluginFolderTests(TestCase):
    def setUp(self):
        self.plugins_dir = tempfile.mkdtemp(prefix="pyrunner-plugins-")
        self.addCleanup(shutil.rmtree, self.plugins_dir, True)
        self.stray = Path(self.plugins_dir) / "demo"
        self.stray.mkdir()
        (self.stray / "leftover.txt").write_text("old bytes")

    def test_folder_without_a_row_is_moved_aside_and_upload_succeeds(self):
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            plugin = PluginService.install_from_zip(_plugin_zip())

        self.assertEqual(plugin.slug, "demo")
        self.assertTrue((self.stray / "plugin.json").exists())
        retired = [p for p in Path(self.plugins_dir).iterdir() if p.name.startswith("demo.orphaned-")]
        self.assertEqual(len(retired), 1)
        self.assertTrue((retired[0] / "leftover.txt").exists())

    def test_folder_with_a_row_still_blocks(self):
        Plugin.objects.create(slug="demo", name="Demo", version="1.0.0", status=Plugin.Status.INSTALLED)
        with override_settings(PLUGINS_DIR=self.plugins_dir):
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.install_from_zip(_plugin_zip())
        self.assertIn("already exists", str(caught.exception))
        self.assertTrue((self.stray / "leftover.txt").exists())

    def test_dev_mode_folder_is_never_touched(self):
        with override_settings(PLUGINS_DIR=self.plugins_dir, DEV_PLUGIN="plugins.demo"):
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.install_from_zip(_plugin_zip())
        self.assertIn("dev mode", str(caught.exception))
        self.assertTrue((self.stray / "leftover.txt").exists())


# ---------------------------------------------------------------------------
# F5 — honest pyrunner_db message
# ---------------------------------------------------------------------------


class PyRunnerDbMessageTests(TestCase):
    def test_missing_psycopg_names_the_environment_fix(self):
        from core.script_helpers import pyrunner_db

        with mock.patch.dict(sys.modules, {"psycopg": None}):
            with self.assertRaises(pyrunner_db.PyRunnerDbError) as caught:
                pyrunner_db.connect("crm")

        self.assertIn("not installed in this script's environment", str(caught.exception))
        self.assertNotIn("ships with", str(caught.exception))


# ---------------------------------------------------------------------------
# F8 — admin URL slug change tells you it needs a restart
# ---------------------------------------------------------------------------


class AdminSlugNoticeTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.admin_url_slug = "django-admin"
        gs.save()
        user = User.objects.create(email="admin@example.com", is_staff=True, is_superuser=True)
        WorkspaceMembership.ensure(user, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)
        Environment.objects.create(name="Default Environment", path="default", is_default=True)

    def _post(self, slug):
        response = self.client.post(
            reverse("cpanel:general_settings"),
            {"instance_name": "PyRunner", "timezone": "UTC", "admin_url_slug": slug},
        )
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_changed_slug_warns_about_the_restart(self):
        messages = self._post("secret-admin")
        self.assertTrue(any("restart" in m.lower() for m in messages), messages)
        self.assertEqual(GlobalSettings.get_settings().admin_url_slug, "secret-admin")

    def test_unchanged_slug_does_not_warn(self):
        messages = self._post("django-admin")
        self.assertFalse(any("restart" in m.lower() for m in messages), messages)


# ---------------------------------------------------------------------------
# F9 — drive django-q's real scheduler loop against freshly synced schedules
# ---------------------------------------------------------------------------


class ScheduleFirstTickTests(TestCase):
    """What django-q actually enqueues on the ticks right after a sync."""

    def setUp(self):
        from django_q.conf import Conf

        self.env = Environment.objects.create(name="e-tick", path="p-tick")
        self.script = Script.objects.create(
            name="tick", code="print(1)", environment=self.env, workspace=Workspace.get_default()
        )
        # Our q-schedule rows carry no cluster name; the scheduler only picks
        # those up when it believes it is the default cluster.
        self._saved_cluster = Conf.CLUSTER_NAME
        Conf.CLUSTER_NAME = Conf.PREFIX
        self.addCleanup(setattr, Conf, "CLUSTER_NAME", self._saved_cluster)

    @staticmethod
    def _tick():
        from django_q.brokers import get_broker
        from django_q.scheduler import scheduler

        scheduler(get_broker())

    @staticmethod
    def _queued():
        """Script-run tasks in the ORM queue (the infra schedules fire on tick one too)."""
        from django_q.models import OrmQ

        from core.services.schedule_service import ScheduleService

        return sum(1 for q in OrmQ.objects.all() if q.task["func"] == ScheduleService.TASK_FUNC)

    def _schedule(self, **fields):
        from core.models import ScriptSchedule

        return ScriptSchedule.objects.create(
            script=self.script, workspace=Workspace.get_default(), **fields
        )

    def test_interval_schedule_fires_once_then_waits_a_full_interval(self):
        from django_q.models import Schedule as QSchedule

        from core.models import ScriptSchedule
        from core.services.schedule_service import ScheduleService

        sched = self._schedule(run_mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=5)
        ids = ScheduleService.sync_schedule(sched)

        self._tick()  # an interval schedule starts right away by design
        self.assertEqual(self._queued(), 1)
        self._tick()  # the next scheduler pass must not fire it again
        self.assertEqual(self._queued(), 1)
        self.assertGreater(
            QSchedule.objects.get(id=ids[0]).next_run, timezone.now() + timedelta(minutes=4)
        )

    def test_clock_schedule_does_not_fire_on_creation(self):
        from core.models import ScriptSchedule
        from core.services.schedule_service import ScheduleService

        in_one_hour = (timezone.now() + timedelta(hours=1)).strftime("%H:%M")
        sched = self._schedule(
            run_mode=ScriptSchedule.RunMode.DAILY, daily_times=[in_one_hour], timezone="UTC"
        )
        ScheduleService.sync_schedule(sched)

        self._tick()

        self.assertEqual(self._queued(), 0)
