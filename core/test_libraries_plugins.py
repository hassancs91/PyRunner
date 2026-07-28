"""
Script Libraries — Stage 3 (plugin integration + backup) tests.

The plan's Stage 3 bar, in order:

- a dev-mode plugin's ``lib/`` folder round-trips edit → run → new revision with
  ZERO manual syncs, and is inert for a production (non-dev) plugin
- plugin uninstall with remove_data leaves no library or revision rows
- backup round-trips libraries (head revision + attachments + run stamps) and a
  pre-1.7.0 backup still restores

Dev mode is normally gated on DEBUG + PYRUNNER_PLUGIN_DEV + RUN_MAIN in settings;
these tests drive the resolved ``settings.DEV_PLUGIN`` directly, which is the
value that gate produces.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from core.models import (
    Environment,
    Library,
    LibraryRevision,
    Plugin,
    Run,
    Script,
    ScriptLibrary,
    User,
    Workspace,
)
from core.services.backup_service import BackupService
from core.services.library_service import (
    read_library_folder,
    resync_dev_libraries,
    dev_plugin_slug,
)
from core.services.plugin_service import PluginService

MODULES = {"helpers.py": "def greet(n):\n    return f'hi {n}'\n"}


class ReadLibraryFolderTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pyrunner-libfolder-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_reads_top_level_python_files(self):
        (self.folder / "helpers.py").write_text("x = 1", encoding="utf-8")
        (self.folder / "pipeline.py").write_text("y = 2", encoding="utf-8")
        self.assertEqual(
            read_library_folder(str(self.folder)),
            {"helpers.py": "x = 1", "pipeline.py": "y = 2"},
        )

    def test_ignores_non_python_and_subdirectories(self):
        # Subdirs are skipped, not flattened — flattening would collide names.
        (self.folder / "keep.py").write_text("x = 1", encoding="utf-8")
        (self.folder / "notes.md").write_text("hi", encoding="utf-8")
        (self.folder / "__pycache__").mkdir()
        (self.folder / "__pycache__" / "stale.py").write_text("z = 9", encoding="utf-8")
        self.assertEqual(read_library_folder(str(self.folder)), {"keep.py": "x = 1"})

    def test_missing_folder_is_empty_not_an_error(self):
        self.assertEqual(read_library_folder(str(self.folder / "nope")), {})
        self.assertEqual(read_library_folder(None), {})


class DevResyncTests(TestCase):
    """The dev-mode edit→run loop."""

    OWNER = "devplug"

    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.script = Script.objects.create(
            name="worker",
            code="pass",
            environment=self.env,
            workspace=self.ws,
            owner_plugin=self.OWNER,
        )
        self.library = Library.objects.create(
            key="devplug_lib", name="Dev", workspace=self.ws, owner_plugin=self.OWNER
        )
        self.library.save_revision(MODULES)
        ScriptLibrary.objects.create(script=self.script, library=self.library)

        # A fake plugin package on disk with a lib/ folder, importable as
        # plugins.<slug> — the same shape the dev-mode splice produces.
        self.tmp = tempfile.TemporaryDirectory(prefix="pyrunner-devplug-")
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name) / self.OWNER
        (self.plugin_dir / "lib").mkdir(parents=True)
        (self.plugin_dir / "__init__.py").write_text("", encoding="utf-8")
        self._write_lib(MODULES["helpers.py"])

        import plugins as plugins_pkg

        plugins_pkg.__path__.append(str(self.tmp.name))
        self.addCleanup(plugins_pkg.__path__.remove, str(self.tmp.name))
        self.addCleanup(sys.modules.pop, f"plugins.{self.OWNER}", None)

    def _write_lib(self, code):
        (self.plugin_dir / "lib" / "helpers.py").write_text(code, encoding="utf-8")

    def _dev_on(self):
        return override_settings(DEV_PLUGIN=f"plugins.{self.OWNER}")

    def test_dev_plugin_slug_reads_settings(self):
        with self._dev_on():
            self.assertEqual(dev_plugin_slug(), self.OWNER)

    def test_no_dev_plugin_means_no_slug(self):
        with override_settings(DEV_PLUGIN=None):
            self.assertIsNone(dev_plugin_slug())

    def test_edit_on_disk_creates_a_new_revision(self):
        self._write_lib("def greet(n):\n    return f'EDITED {n}'\n")
        with self._dev_on():
            self.assertTrue(resync_dev_libraries(self.script))

        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 2)
        self.assertIn("EDITED", self.library.head.modules["helpers.py"])

    def test_unchanged_folder_writes_nothing(self):
        # Re-queuing an unedited dev plugin must not churn history.
        with self._dev_on():
            self.assertFalse(resync_dev_libraries(self.script))
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 1)
        self.assertEqual(self.library.revisions.count(), 1)

    def test_production_plugin_is_never_resynced(self):
        # The whole point of the dev gate: production stays DB-deterministic.
        self._write_lib("def greet(n):\n    return 'EDITED'\n")
        with override_settings(DEV_PLUGIN=None):
            self.assertFalse(resync_dev_libraries(self.script))
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 1)

    def test_a_different_plugins_script_is_not_resynced(self):
        other = Script.objects.create(
            name="other", code="pass", environment=self.env,
            workspace=self.ws, owner_plugin="otherplug",
        )
        self._write_lib("def greet(n):\n    return 'EDITED'\n")
        with self._dev_on():
            self.assertFalse(resync_dev_libraries(other))

    def test_user_script_is_not_resynced(self):
        user_script = Script.objects.create(
            name="mine", code="pass", environment=self.env, workspace=self.ws
        )
        with self._dev_on():
            self.assertFalse(resync_dev_libraries(user_script))

    def test_ambiguous_multi_library_plugin_is_skipped_loudly(self):
        # One lib/ folder cannot say which of two libraries it is — skip and say
        # so rather than guess and overwrite the wrong one.
        Library.objects.create(
            key="devplug_second", name="Second", workspace=self.ws,
            owner_plugin=self.OWNER,
        )
        self._write_lib("def greet(n):\n    return 'EDITED'\n")
        with self._dev_on(), self.assertLogs("core.services.library_service", "WARNING") as logs:
            self.assertFalse(resync_dev_libraries(self.script))
        self.assertIn("cannot say which", "".join(logs.output))
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 1)

    def test_invalid_module_on_disk_does_not_block_the_run(self):
        # A dev typo must degrade to "run on the last good revision", loudly.
        (self.plugin_dir / "lib" / "helpers.py").unlink()
        (self.plugin_dir / "lib" / "9bad.py").write_text("x = 1", encoding="utf-8")
        with self._dev_on(), self.assertLogs("core.services.library_service", "WARNING") as logs:
            self.assertFalse(resync_dev_libraries(self.script))
        self.assertIn("rejected", "".join(logs.output))
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 1)

    def test_unreadable_module_does_not_block_the_run(self):
        # A .py that isn't valid UTF-8 raises out of the reader. This runs inside
        # queue_script_run, so escaping would turn one bad byte into "nothing
        # queues at all" — it must degrade like any other bad revision.
        (self.plugin_dir / "lib" / "helpers.py").write_bytes(
            b"x = 1  # \xff\xfe not utf-8\n"
        )
        with self._dev_on(), self.assertLogs("core.services.library_service", "WARNING") as logs:
            self.assertFalse(resync_dev_libraries(self.script))
        self.assertIn("could not read", "".join(logs.output))
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 1)

    def test_unreadable_module_still_lets_the_run_queue(self):
        from core.tasks import queue_script_run

        (self.plugin_dir / "lib" / "helpers.py").write_bytes(b"\xff\xfe\n")
        run = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.PENDING
        )
        with self._dev_on(), mock.patch("core.tasks.async_task", return_value="t1"):
            queue_script_run(run)  # must not raise

        run.refresh_from_db()
        # Queued and pinned to the last good revision.
        self.assertEqual(run.library_versions, {"devplug_lib": 1})

    def test_queue_path_resyncs_then_stamps(self):
        # The integration the plan asks for: zero manual steps between an edit
        # and a run that uses it.
        from core.tasks import queue_script_run

        self._write_lib("def greet(n):\n    return f'EDITED {n}'\n")
        run = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.PENDING
        )
        with self._dev_on(), mock.patch("core.tasks.async_task", return_value="t1"):
            queue_script_run(run)

        run.refresh_from_db()
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 2)
        # Stamped to the JUST-synced revision, not the stale one.
        self.assertEqual(run.library_versions, {"devplug_lib": 2})


class UpsertFromFolderTests(TestCase):
    """The provisioning shape documented in docs/plugins.md, executed.

    Keeps the author guide honest: if a signature here changes, the doc's example
    stops working and this fails.
    """

    OWNER = "my_flows"

    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.tmp = tempfile.TemporaryDirectory(prefix="pyrunner-libsrc-")
        self.addCleanup(self.tmp.cleanup)
        self.lib_dir = Path(self.tmp.name) / "lib"
        self.lib_dir.mkdir()
        (self.lib_dir / "helpers.py").write_text(MODULES["helpers.py"], encoding="utf-8")
        (self.lib_dir / "pipeline.py").write_text(
            "from .helpers import greet\n\n\ndef run_for_account(a):\n    return greet(a)\n",
            encoding="utf-8",
        )

    def test_documented_provisioning_flow(self):
        from core.plugins.api import LibraryAPI, ScriptAPI

        libs = LibraryAPI(self.OWNER, workspace=self.ws)
        lib = libs.upsert_from_folder("my_flows_lib", str(self.lib_dir))
        self.assertEqual(lib.current_version, 1)
        self.assertEqual(sorted(lib.head.modules), ["helpers.py", "pipeline.py"])

        scripts = []
        for account in ("acme", "globex"):
            script = ScriptAPI(self.OWNER, workspace=self.ws).upsert(
                key=f"worker_{account}",
                code="from my_flows_lib.pipeline import run_for_account\n",
                environment=self.env,
            )
            libs.attach(script, lib)
            scripts.append(script)

        # The whole point: N workers, ONE shared library.
        for script in scripts:
            self.assertEqual(list(script.libraries.all()), [lib])
        self.assertEqual(Library.objects.filter(owner_plugin=self.OWNER).count(), 1)

    def test_reprovisioning_unchanged_folder_writes_no_revision(self):
        from core.plugins.api import LibraryAPI

        libs = LibraryAPI(self.OWNER, workspace=self.ws)
        libs.upsert_from_folder("my_flows_lib", str(self.lib_dir))
        lib = libs.upsert_from_folder("my_flows_lib", str(self.lib_dir))
        self.assertEqual(lib.current_version, 1)
        self.assertEqual(lib.revisions.count(), 1)

    def test_empty_folder_is_a_named_error_not_a_silent_noop(self):
        from core.plugins.api import LibraryAPI

        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError) as ctx:
            LibraryAPI(self.OWNER, workspace=self.ws).upsert_from_folder("x_lib", str(empty))
        self.assertIn("no .py modules", str(ctx.exception))


class UninstallCascadeTests(TestCase):
    OWNER = "cascadeplug"

    def setUp(self):
        self.ws = Workspace.get_default()
        self.plugin = Plugin.objects.create(slug=self.OWNER, name="Cascade")
        self.library = Library.objects.create(
            key="cascade_lib", name="C", workspace=self.ws, owner_plugin=self.OWNER
        )
        self.library.save_revision(MODULES)

    def test_owned_counts_include_libraries(self):
        counts = PluginService.owned_resource_counts(self.OWNER)
        self.assertEqual(counts["libraries"], 1)
        self.assertEqual(counts["total"], 1)

    def test_cleanup_removes_libraries_and_revisions(self):
        removed = PluginService._cleanup_owned_resources(self.OWNER)
        self.assertIn("libraries", removed)
        self.assertFalse(Library.objects.filter(owner_plugin=self.OWNER).exists())
        # Revisions cascade with their library — no orphans.
        self.assertFalse(
            LibraryRevision.objects.filter(library_id=self.library.id).exists()
        )

    def test_cleanup_leaves_user_libraries_alone(self):
        mine = Library.objects.create(key="mine_lib", name="M", workspace=self.ws)
        PluginService._cleanup_owned_resources(self.OWNER)
        self.assertTrue(Library.objects.filter(pk=mine.pk).exists())

    def test_delete_with_remove_data_leaves_no_rows(self):
        with mock.patch.object(PluginService, "_run_uninstall_data", return_value=(True, "")):
            PluginService.delete(self.plugin, remove_data=True)
        self.assertFalse(Library.objects.filter(owner_plugin=self.OWNER).exists())
        self.assertEqual(LibraryRevision.objects.count(), 0)

    def test_delete_without_remove_data_keeps_libraries(self):
        PluginService.delete(self.plugin, remove_data=False)
        self.assertTrue(Library.objects.filter(owner_plugin=self.OWNER).exists())

    def test_delete_preview_names_the_libraries_it_will_delete(self):
        # The delete-confirm modal is built from this string. It previously listed
        # only scripts/secrets/datastores, so libraries were deleted without ever
        # being disclosed.
        self.assertEqual(PluginService.owned_resource_summary(self.OWNER), "1 library")

    def test_delete_preview_covers_every_model_the_cleanup_deletes(self):
        """Parity guard: anything the cleanup deletes must appear in the preview.

        This is the invariant that actually matters — not the exact wording. If a
        future resource is added to the cleanup and not to the label table, this
        fails instead of silently under-reporting a destructive action.
        """
        from core.models.plugin import RESOURCE_LABELS

        counts = PluginService.owned_resource_counts(self.OWNER)
        labelled = {key for key, _, _ in RESOURCE_LABELS}
        deleted_models = set(counts) - {"total"}
        self.assertEqual(deleted_models - labelled, set())


class BackupRoundTripTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="dev@example.com")
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.library = Library.objects.create(
            key="round_lib", name="Round", description="d",
            workspace=self.ws, created_by=self.user,
        )
        self.library.save_revision(MODULES)
        self.library.save_revision({"helpers.py": "def greet(n):\n    return 'v2'\n"})
        self.script = Script.objects.create(
            name="worker", code="pass", environment=self.env, workspace=self.ws
        )
        ScriptLibrary.objects.create(script=self.script, library=self.library)
        self.run = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.SUCCESS,
            library_versions={"round_lib": 2},
        )

    def test_backup_version_is_bumped(self):
        self.assertEqual(BackupService.BACKUP_VERSION, "1.7.0")

    def test_export_carries_head_only_with_its_version(self):
        data = BackupService.create_backup()
        [lib] = data["libraries"]
        self.assertEqual(lib["key"], "round_lib")
        self.assertEqual(lib["current_version"], 2)
        # HEAD only — v1's content is deliberately not in the backup.
        self.assertEqual(lib["modules"], {"helpers.py": "def greet(n):\n    return 'v2'\n"})
        [script] = data["scripts"]
        self.assertEqual(script["libraries"], [str(self.library.id)])
        self.assertEqual(data["runs"][0]["library_versions"], {"round_lib": 2})

    def test_restore_round_trips_library_attachment_and_stamp(self):
        data = BackupService.create_backup()
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result.get("errors"))

        library = Library.objects.get(key="round_lib")
        # current_version is carried, and the head is recreated under its
        # ORIGINAL number — so the restored run's stamp still resolves.
        self.assertEqual(library.current_version, 2)
        self.assertEqual(library.head.version, 2)
        self.assertIn("v2", library.head.modules["helpers.py"])
        self.assertEqual(library.revisions.count(), 1)  # history collapses to head

        script = Script.objects.get(name="worker")
        self.assertEqual(list(script.libraries.all()), [library])
        self.assertEqual(
            Run.objects.get(pk=self.run.pk).library_versions, {"round_lib": 2}
        )
        self.assertEqual(result["counts"]["libraries"], 1)

    def test_restored_stamp_still_materializes(self):
        # The point of carrying current_version: the pinned revision resolves.
        from core.services.library_service import materialize_libraries

        BackupService.restore_backup(BackupService.create_backup(), current_user=self.user)
        run = Run.objects.get(pk=self.run.pk)
        with tempfile.TemporaryDirectory() as dest:
            self.assertEqual(materialize_libraries(run, dest), ["round_lib"])
            with open(os.path.join(dest, "round_lib", "helpers.py"), encoding="utf-8") as fh:
                self.assertIn("v2", fh.read())

    def test_restore_replaces_rather_than_collides(self):
        # Full replace: a library already present must not trip the per-workspace
        # unique key on the incoming row.
        data = BackupService.create_backup()
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(Library.objects.filter(key="round_lib").count(), 1)

    def test_pre_1_7_0_backup_restores_without_libraries(self):
        # Backward compatibility: an old backup has no libraries key at all.
        data = BackupService.create_backup()
        data.pop("libraries")
        for script in data["scripts"]:
            script.pop("libraries", None)
        for run in data["runs"]:
            run.pop("library_versions", None)

        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(Library.objects.count(), 0)
        self.assertIsNone(Run.objects.get(pk=self.run.pk).library_versions)
