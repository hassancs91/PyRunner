"""
Plugin Platform v2 — Stage 3 (SDK facade, WS2) tests.

Exercises core.plugins.api: idempotent owner-keyed upserts, ownership + workspace
auto-stamping, DataStore auto-naming, the legacy (owner=None) lane, bulk
set_environment, and that the SDK is import-light (no core.models at module top).
"""

import json
import subprocess
import sys

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.plugins.api import (
    API_VERSION,
    DataStoreAPI,
    EnvironmentAPI,
    RunView,
    ScheduleAPI,
    ScriptAPI,
    SecretAPI,
)
from core.models import (
    DataStore,
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Secret,
    SecretGrant,
    Workspace,
)


class LightImportTests(SimpleTestCase):
    def test_api_version_present(self):
        self.assertTrue(API_VERSION)

    def test_sdk_does_not_import_core_models_at_module_top(self):
        # The whole point: a plugin's apps.py can `from core.plugins.api import …`
        # without dragging in core.models (keeps the light-import boot guard).
        code = "import sys; import core.plugins.api; sys.exit(7 if 'core.models' in sys.modules else 0)"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(settings.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ScriptAPITests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="sdkenv")
        self.env2 = Environment.objects.create(name="e2", path="sdkenv2")
        self.api = ScriptAPI("myplugin")

    def test_upsert_is_idempotent_and_stamps_owner_workspace(self):
        s1 = self.api.upsert(key="backup", code="print(1)", environment=self.env)
        s2 = self.api.upsert(key="backup", code="print(2)", environment=self.env)
        self.assertEqual(s1.pk, s2.pk)  # same row updated, not duplicated
        self.assertEqual(Script.objects.filter(owner_plugin="myplugin").count(), 1)
        self.assertEqual(s2.owner_plugin, "myplugin")
        self.assertEqual(s2.owner_key, "backup")
        self.assertEqual(s2.workspace_id, self.ws.id)
        self.assertEqual(s2.code, "print(2)")
        # Owned scripts default to selected injection; isolation left to the policy.
        self.assertEqual(s2.injection_mode, Script.InjectionMode.SELECTED)
        self.assertEqual(s2.isolation_mode, Script.IsolationMode.INHERIT)

    def test_name_auto_derived_when_omitted(self):
        s = self.api.upsert(key="restore", code="x", environment=self.env)
        self.assertEqual(s.name, "myplugin:restore")

    def test_upsert_requires_key_for_owned(self):
        with self.assertRaises(ValueError):
            self.api.upsert(code="x", environment=self.env)

    def test_upsert_requires_environment_on_create(self):
        with self.assertRaises(ValueError):
            self.api.upsert(key="k", code="x")

    def test_environment_accepts_name_string(self):
        s = self.api.upsert(key="k", code="x", environment="e")
        self.assertEqual(s.environment_id, self.env.id)

    def test_set_environment_bulk_updates_all_owned(self):
        self.api.upsert(key="a", code="x", environment=self.env)
        self.api.upsert(key="b", code="y", environment=self.env)
        # A user script must NOT be touched.
        user_script = Script.objects.create(
            name="u", code="z", environment=self.env, workspace=self.ws
        )
        n = self.api.set_environment(self.env2)
        self.assertEqual(n, 2)
        self.assertTrue(
            all(s.environment_id == self.env2.id
                for s in Script.objects.filter(owner_plugin="myplugin"))
        )
        user_script.refresh_from_db()
        self.assertEqual(user_script.environment_id, self.env.id)

    def test_queue_run_creates_run_via_seam(self):
        self.api.upsert(key="backup", code="x", environment=self.env)
        with mock.patch("core.tasks.queue_script_run") as q:
            run = self.api.queue_run("backup")
        q.assert_called_once()
        self.assertEqual(run.workspace_id, self.ws.id)
        self.assertEqual(run.status, Run.Status.PENDING)


class RunLifecycleAPITests(TestCase):
    """ScriptAPI run-lifecycle surface (API 2.1): latest_run / runs /
    cancel_latest_run + the ORM-decoupled RunView read-model."""

    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="runenv")
        self.api = ScriptAPI("myplugin")
        self.script = self.api.upsert(key="backup", code="x", environment=self.env)

    def _make_run(self, *, script=None, status=Run.Status.SUCCESS, offset=0, **kwargs):
        """Create a Run with a deterministic created_at (offset seconds from now).

        ``auto_now_add`` ignores a passed created_at on create, so stamp the
        ordering explicitly via update().
        """
        script = script or self.script
        run = Run.objects.create(
            script=script,
            workspace_id=script.workspace_id,
            status=status,
            **kwargs,
        )
        Run.objects.filter(pk=run.pk).update(
            created_at=timezone.now() + timedelta(seconds=offset)
        )
        run.refresh_from_db()
        return run

    # -- latest_run ---------------------------------------------------------- #

    def test_latest_run_none_when_no_runs(self):
        self.assertIsNone(self.api.latest_run("backup"))

    def test_latest_run_none_for_unknown_key(self):
        self.assertIsNone(self.api.latest_run("nope"))

    def test_latest_run_returns_newest_as_runview(self):
        self._make_run(status=Run.Status.SUCCESS, offset=-10)
        newest = self._make_run(
            status=Run.Status.RUNNING,
            offset=0,
            trigger_type=Run.TriggerType.MANUAL,
            pid=4321,
            task_id="t-xyz",
        )
        view = self.api.latest_run("backup")
        self.assertIsInstance(view, RunView)
        self.assertEqual(view.id, str(newest.id))
        self.assertEqual(view.status, "running")
        self.assertEqual(view.trigger_type, "manual")
        self.assertEqual(view.pid, 4321)
        self.assertEqual(view.task_id, "t-xyz")
        self.assertTrue(view.is_running)
        self.assertFalse(view.is_finished)

    # -- runs ---------------------------------------------------------------- #

    def test_runs_newest_first_and_respects_limit(self):
        r_old = self._make_run(offset=-30)
        r_mid = self._make_run(offset=-20)
        r_new = self._make_run(offset=-10)
        ids = [v.id for v in self.api.runs("backup")]
        self.assertEqual(ids, [str(r_new.id), str(r_mid.id), str(r_old.id)])
        limited = self.api.runs("backup", limit=2)
        self.assertEqual([v.id for v in limited], [str(r_new.id), str(r_mid.id)])
        self.assertEqual(self.api.runs("backup", limit=0), [])

    def test_runs_owner_scoped(self):
        # Another owner's same-key script + run must NOT appear for myplugin.
        other = ScriptAPI("otherplugin").upsert(
            key="backup", code="y", environment=self.env
        )
        self._make_run(script=other)
        self.assertEqual(self.api.runs("backup"), [])  # myplugin's "backup" has no runs
        self.assertEqual(len(ScriptAPI("otherplugin").runs("backup")), 1)

    # -- cancel_latest_run --------------------------------------------------- #

    def test_cancel_latest_run_cancels_running(self):
        self._make_run(status=Run.Status.RUNNING, offset=0, pid=None, task_id="t-run")
        self.assertTrue(self.api.cancel_latest_run("backup"))
        run = Run.objects.get(task_id="t-run")
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertIsNotNone(run.ended_at)

    def test_cancel_latest_run_cancels_pending(self):
        self._make_run(status=Run.Status.PENDING, offset=0, task_id="t-pend")
        self.assertTrue(self.api.cancel_latest_run("backup"))
        run = Run.objects.get(task_id="t-pend")
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertIsNotNone(run.ended_at)

    def test_cancel_latest_run_false_when_nothing_cancellable(self):
        self._make_run(status=Run.Status.SUCCESS, offset=0)
        self.assertFalse(self.api.cancel_latest_run("backup"))

    def test_cancel_latest_run_false_for_unknown_key(self):
        self.assertFalse(self.api.cancel_latest_run("nope"))

    def test_cancel_latest_run_targets_latest_cancellable(self):
        # A newer finished run must not mask the latest PENDING/RUNNING one.
        running = self._make_run(
            status=Run.Status.RUNNING, offset=-5, pid=None, task_id="t-r"
        )
        self._make_run(status=Run.Status.SUCCESS, offset=0)  # newest overall, terminal
        self.assertTrue(self.api.cancel_latest_run("backup"))
        running.refresh_from_db()
        self.assertEqual(running.status, Run.Status.CANCELLED)

    def test_cancel_latest_run_owner_scoped(self):
        # owner A cannot cancel owner B's run.
        other = ScriptAPI("otherplugin").upsert(
            key="backup", code="y", environment=self.env
        )
        self._make_run(
            script=other, status=Run.Status.RUNNING, pid=None, task_id="t-b"
        )
        self.assertFalse(self.api.cancel_latest_run("backup"))  # myplugin has no run
        run_b = Run.objects.get(task_id="t-b")
        self.assertEqual(run_b.status, Run.Status.RUNNING)  # untouched

    # -- RunView decoupling + serialization ---------------------------------- #

    def test_runview_decoupled_and_as_dict_json_serializable(self):
        self._make_run(
            status=Run.Status.SUCCESS,
            offset=0,
            exit_code=0,
            started_at=timezone.now(),
            ended_at=timezone.now(),
            task_id="t-json",
        )
        view = self.api.latest_run("backup")
        self.assertIsInstance(view, RunView)
        # Frozen: no accidental mutation, and no live ORM object behind it.
        with self.assertRaises(Exception):
            view.status = "mutated"
        d = view.as_dict()
        self.assertIsInstance(d["created_at"], str)  # datetime -> ISO 8601
        self.assertIsInstance(d["started_at"], str)
        self.assertIsInstance(d["ended_at"], str)
        self.assertEqual(d["status"], "success")
        json.dumps(d)  # must round-trip through JSON without a custom encoder

    # -- legacy (owner=None) lane mirrors .get() ----------------------------- #

    def test_legacy_owner_none_lane_like_get(self):
        legacy_script = Script.objects.create(
            name="legacy-script", code="z", environment=self.env, workspace=self.ws
        )
        self._make_run(script=legacy_script, status=Run.Status.SUCCESS)
        legacy_api = ScriptAPI()  # owner=None — resolves by NAME, like .get()
        self.assertIsNotNone(legacy_api.get("legacy-script"))
        self.assertIsNotNone(legacy_api.latest_run("legacy-script"))
        self.assertEqual(len(legacy_api.runs("legacy-script")), 1)
        # An owned script is invisible in the legacy lane (mirrors .get()).
        self.assertIsNone(legacy_api.get("backup"))
        self.assertIsNone(legacy_api.latest_run("backup"))
        self.assertFalse(legacy_api.cancel_latest_run("backup"))


class SecretAPITests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()

    def test_owned_upsert_idempotent_clean_key(self):
        api = SecretAPI("myplugin")
        s1 = api.upsert("R2_BUCKET", "one")
        s2 = api.upsert("R2_BUCKET", "two")
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(s2.owner_plugin, "myplugin")
        self.assertEqual(s2.owner_key, "R2_BUCKET")
        self.assertEqual(s2.get_decrypted_value(), "two")
        self.assertEqual(s2.get_clean_name(), "R2_BUCKET")

    def test_two_plugins_same_clean_key(self):
        a = SecretAPI("plugin_a").upsert("R2_BUCKET", "a")
        b = SecretAPI("plugin_b").upsert("R2_BUCKET", "b")
        self.assertNotEqual(a.pk, b.pk)

    def test_legacy_lane_owner_none(self):
        api = SecretAPI()  # owner=None
        s = api.upsert("PLAIN_KEY", "v")
        self.assertIsNone(s.owner_plugin)
        # idempotent by key in the legacy lane too
        s2 = api.upsert("PLAIN_KEY", "v2")
        self.assertEqual(s.pk, s2.pk)
        self.assertEqual(s2.get_decrypted_value(), "v2")

    def test_grant_idempotent(self):
        env = Environment.objects.create(name="e", path="grenv")
        script = ScriptAPI("myplugin").upsert(key="k", code="x", environment=env)
        secret = SecretAPI("myplugin").upsert("API_KEY", "v")
        g1 = SecretAPI("myplugin").grant(script, secret)
        g2 = SecretAPI("myplugin").grant(script, secret)
        self.assertEqual(g1.pk, g2.pk)
        self.assertEqual(SecretGrant.objects.filter(script=script).count(), 1)


class DataStoreAPITests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()

    def test_auto_naming_and_entries(self):
        api = DataStoreAPI("myplugin")
        store = api.upsert("state", description="plugin state")
        self.assertEqual(store.name, "myplugin:state")
        self.assertEqual(store.model.owner_plugin, "myplugin")
        self.assertEqual(store.model.owner_key, "state")

        store.set("config", {"retries": 3})
        self.assertEqual(api.get("state").get("config"), {"retries": 3})

    def test_upsert_idempotent(self):
        api = DataStoreAPI("myplugin")
        a = api.upsert("state")
        b = api.upsert("state")
        self.assertEqual(a.model.pk, b.model.pk)
        self.assertEqual(DataStore.objects.filter(name="myplugin:state").count(), 1)

    def test_legacy_lane_raw_name(self):
        store = DataStoreAPI().upsert("plain_store")
        self.assertEqual(store.name, "plain_store")
        self.assertIsNone(store.model.owner_plugin)


class EnvironmentAPITests(TestCase):
    def test_list_and_get(self):
        Environment.objects.create(name="alpha", path="p1")
        Environment.objects.create(name="beta", path="p2")
        api = EnvironmentAPI()
        names = {e.name for e in api.list()}
        self.assertTrue({"alpha", "beta"}.issubset(names))
        self.assertEqual(api.get("alpha").name, "alpha")
        self.assertIsNone(api.get("nope"))


class ScheduleAPITests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="schedenv")

    def test_sync_creates_active_schedule(self):
        script = ScriptAPI("myplugin").upsert(key="backup", code="x", environment=self.env)
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            sched = ScheduleAPI("myplugin").sync(
                script, mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=60
            )
        self.assertTrue(sched.is_active)
        self.assertEqual(sched.interval_minutes, 60)
