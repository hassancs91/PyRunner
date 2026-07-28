"""
Script Libraries — Stage 1 (model + executor + SDK) tests.

The seam's load-bearing properties, in order of how much they'd hurt to get wrong:

- PINNED EXECUTION: a run imports the revision stamped at QUEUE time, so editing
  a library after queueing cannot change what an in-flight run executes.
- Fail-closed: a pinned revision that vanished fails the run with a named cause
  before spawn, never a silent ImportError from inside user code.
- Helpers win PYTHONPATH; ``pyrunner*`` keys are banned (the two fences that stop
  a library shadowing pyrunner_db/pyrunner_ai).
- Revision-only-on-change: idempotent plugin provisioning must not churn history.
- Every trigger stamps (the guarantee we get from stamping in queue_script_run).

The e2e tests stub the environment to the test runner's own Python so a REAL
subprocess really imports a materialized package — the Stage 1 acceptance bar.
"""

import os
import sys
import uuid
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.executor import _build_script_environment, execute_run
from core.models import (
    Environment,
    Library,
    LibraryRevision,
    Run,
    Script,
    ScriptLibrary,
    Workspace,
)
from core.models.library import hash_modules, validate_library_key, validate_modules
from core.plugins.api import LibraryAPI
from core.services.library_service import (
    LibraryMaterializationError,
    materialize_libraries,
    stamp_library_versions,
)

HELPER_MODULES = {
    "helper.py": "def greet(name):\n    return f'hello {name}'\n",
    "pipeline.py": "from .helper import greet\n\n\ndef run():\n    return greet('world')\n",
}


def _stub_python(func):
    """Make _validate_environment return the test runner's Python (real spawn)."""
    return mock.patch(
        "core.executor._validate_environment", return_value=sys.executable
    )(func)


class LibraryModelTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()

    def _library(self, key="demo_lib"):
        return Library.objects.create(key=key, name="Demo", workspace=self.ws)

    def test_valid_keys_accepted(self):
        for key in ("demo", "demo_lib", "a", "gmail_agent_lib", "lib2"):
            validate_library_key(key)  # must not raise

    def test_invalid_keys_rejected(self):
        for key in ("Demo", "2lib", "demo-lib", "demo lib", "demo.lib", "", "_lib"):
            with self.assertRaises(ValidationError, msg=key):
                validate_library_key(key)

    def test_pyrunner_prefix_is_reserved(self):
        # The second fence behind PYTHONPATH ordering: a library must never be
        # able to take the name of a built-in helper.
        for key in ("pyrunner", "pyrunner_db", "pyrunnerx"):
            with self.assertRaises(ValidationError, msg=key):
                validate_library_key(key)

    def test_stdlib_module_names_are_reserved(self):
        # PYTHONPATH is searched BEFORE the stdlib, so a library keyed 'email'
        # would hijack `import email` in every script it's attached to — and the
        # breakage surfaces far from its cause. These are all plausible names
        # someone would reach for.
        for key in ("json", "email", "types", "secrets", "logging", "queue", "os"):
            with self.assertRaises(ValidationError, msg=key):
                validate_library_key(key)

    def test_stdlib_suggestion_names_a_working_alternative(self):
        # An actionable error: say what to do, not just what broke.
        with self.assertRaises(ValidationError) as ctx:
            validate_library_key("email")
        self.assertIn("email_lib", " ".join(ctx.exception.messages))
        validate_library_key("email_lib")  # the suggestion must itself be valid

    def test_stdlib_ban_does_not_overreach(self):
        # Only exact top-level stdlib names are refused; a name that merely
        # contains one stays available.
        for key in ("json_lib", "my_email", "emails", "queue_worker"):
            validate_library_key(key)  # must not raise

    def test_module_filenames_must_be_flat(self):
        for bad in ("sub/mod.py", "../escape.py", "mod.txt", "mod", ".py", "a b.py"):
            with self.assertRaises(ValidationError, msg=bad):
                validate_modules({bad: "x = 1"})

    def test_modules_caps_enforced(self):
        with self.assertRaises(ValidationError):
            validate_modules({f"m{i}.py": "x = 1" for i in range(65)})
        with self.assertRaises(ValidationError):
            validate_modules({"big.py": "#" * (512 * 1024 + 1)})
        with self.assertRaises(ValidationError):
            validate_modules({})

    def test_first_revision_is_version_one(self):
        lib = self._library()
        self.assertEqual(lib.current_version, 0)
        self.assertIsNone(lib.head)

        revision, created = lib.save_revision(HELPER_MODULES)
        self.assertTrue(created)
        self.assertEqual(revision.version, 1)
        lib.refresh_from_db()
        self.assertEqual(lib.current_version, 1)
        self.assertEqual(lib.head.modules, HELPER_MODULES)

    def test_revision_written_only_when_content_changes(self):
        # The anti-churn property: plugin provisioning re-runs on every settings
        # save, so identical content must be a no-op or history becomes noise.
        lib = self._library()
        lib.save_revision(HELPER_MODULES)
        _, created = lib.save_revision(dict(HELPER_MODULES))
        self.assertFalse(created)
        self.assertEqual(lib.revisions.count(), 1)
        self.assertEqual(lib.current_version, 1)

        _, created = lib.save_revision({**HELPER_MODULES, "helper.py": "def greet(n):\n    return n\n"})
        self.assertTrue(created)
        self.assertEqual(lib.revisions.count(), 2)
        self.assertEqual(lib.current_version, 2)

    def test_hash_is_order_independent(self):
        self.assertEqual(
            hash_modules({"a.py": "1", "b.py": "2"}),
            hash_modules({"b.py": "2", "a.py": "1"}),
        )

    def test_key_unique_per_workspace(self):
        # Unique per workspace (unlike Secret.key's per-owner axis) BECAUSE the
        # key is a directory name — two same-key libs on one script would collide.
        self._library("shared")
        with self.assertRaises(Exception):
            Library.objects.create(key="shared", name="Other", workspace=self.ws)

    def test_same_key_allowed_in_another_workspace(self):
        other = Workspace.objects.create(name="Other")
        self._library("shared")
        Library.objects.create(key="shared", name="Other", workspace=other)  # no raise

    def test_attach_across_workspaces_rejected(self):
        other = Workspace.objects.create(name="Other")
        env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:8]}")
        script = Script.objects.create(
            name="s", code="pass", environment=env, workspace=other
        )
        attachment = ScriptLibrary(script=script, library=self._library())
        with self.assertRaises(ValidationError):
            attachment.full_clean()


class _RunFixtureMixin:
    """A script with one two-module library attached, in the default workspace."""

    def setUp(self):
        super().setUp()
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="t", path=f"env{uuid.uuid4().hex[:10]}"
        )
        self.library = Library.objects.create(
            key="demo_lib", name="Demo", workspace=self.ws
        )
        self.library.save_revision(HELPER_MODULES)

    def _script(self, code="pass", attach=True):
        script = Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}",
            code=code,
            environment=self.env,
            workspace=self.ws,
        )
        if attach:
            ScriptLibrary.objects.create(script=script, library=self.library)
        return script

    def _run(self, script):
        return Run.objects.create(
            script=script, workspace=self.ws, status=Run.Status.PENDING
        )


class StampingTests(_RunFixtureMixin, TestCase):
    def test_queue_stamps_attached_library_versions(self):
        run = self._run(self._script())
        stamp_library_versions(run)
        run.refresh_from_db()
        self.assertEqual(run.library_versions, {"demo_lib": 1})

    def test_script_without_libraries_stamps_nothing(self):
        # Byte-for-byte with a pre-Libraries instance: NULL column, no staging.
        run = self._run(self._script(attach=False))
        self.assertIsNone(stamp_library_versions(run))
        run.refresh_from_db()
        self.assertIsNone(run.library_versions)

    def test_empty_library_is_not_stamped(self):
        empty = Library.objects.create(key="empty_lib", name="E", workspace=self.ws)
        script = self._script(attach=False)
        ScriptLibrary.objects.create(script=script, library=empty)
        run = self._run(script)
        self.assertIsNone(stamp_library_versions(run))

    def test_stamp_records_head_at_queue_time_not_later(self):
        run = self._run(self._script())
        stamp_library_versions(run)
        self.library.save_revision({"helper.py": "def greet(n):\n    return 'v2'\n"})
        self.library.refresh_from_db()
        self.assertEqual(self.library.current_version, 2)
        run.refresh_from_db()
        self.assertEqual(run.library_versions, {"demo_lib": 1})

    def test_queue_script_run_stamps(self):
        # The structural guarantee: EVERY trigger (manual/scheduled/webhook/
        # channel/SDK) funnels through queue_script_run, so stamping there means
        # no entry point can forget to stamp.
        from core.tasks import queue_script_run

        run = self._run(self._script())
        with mock.patch("core.tasks.async_task", return_value="task-1"):
            queue_script_run(run)
        run.refresh_from_db()
        self.assertEqual(run.library_versions, {"demo_lib": 1})


class MaterializationTests(_RunFixtureMixin, TestCase):
    def _materialize(self, run):
        import tempfile

        dest = tempfile.mkdtemp(prefix="libtest-")
        materialize_libraries(run, dest)
        return dest

    def test_materializes_package_with_auto_init(self):
        run = self._run(self._script())
        stamp_library_versions(run)
        dest = self._materialize(run)

        package = os.path.join(dest, "demo_lib")
        self.assertTrue(os.path.isfile(os.path.join(package, "helper.py")))
        self.assertTrue(os.path.isfile(os.path.join(package, "pipeline.py")))
        # Auto-generated so `from demo_lib.x import y` works without the author
        # having to remember an empty __init__.py.
        self.assertTrue(os.path.isfile(os.path.join(package, "__init__.py")))

    def test_materializes_the_stamped_revision_not_the_head(self):
        run = self._run(self._script())
        stamp_library_versions(run)
        self.library.save_revision({"helper.py": "def greet(n):\n    return 'V2'\n"})

        dest = self._materialize(run)
        with open(os.path.join(dest, "demo_lib", "helper.py"), encoding="utf-8") as fh:
            self.assertIn("hello {name}", fh.read())

    def test_missing_pinned_revision_fails_closed(self):
        run = self._run(self._script())
        stamp_library_versions(run)
        self.library.delete()
        with self.assertRaises(LibraryMaterializationError):
            self._materialize(run)

    def test_unstamped_run_materializes_nothing(self):
        run = self._run(self._script(attach=False))
        self.assertEqual(materialize_libraries(run, "/nonexistent-should-not-be-used"), [])

    def test_illegal_module_name_in_row_is_refused(self):
        # Defense in depth: model validation covers every write path, but a
        # hand-edited/corrupted row must not be able to write outside dest_root.
        run = self._run(self._script())
        stamp_library_versions(run)
        LibraryRevision.objects.filter(library=self.library, version=1).update(
            modules={"../escape.py": "x = 1"}
        )
        with self.assertRaises(LibraryMaterializationError):
            self._materialize(run)


class PythonPathTests(_RunFixtureMixin, TestCase):
    def test_helpers_precede_libraries(self):
        # Order is the fence: a library can never shadow pyrunner_db/pyrunner_ai.
        env = _build_script_environment(library_root="/tmp/libs-x")
        entries = env["PYTHONPATH"].split(os.pathsep)
        helpers_idx = next(
            i for i, e in enumerate(entries) if e.endswith("script_helpers")
        )
        self.assertLess(helpers_idx, entries.index("/tmp/libs-x"))

    def test_no_library_root_leaves_pythonpath_unchanged(self):
        without = _build_script_environment()
        entries = without["PYTHONPATH"].split(os.pathsep)
        self.assertTrue(entries[0].endswith("script_helpers"))


class ExecutorEndToEndTests(_RunFixtureMixin, TestCase):
    """Real subprocesses importing real materialized packages."""

    @_stub_python
    def test_run_imports_attached_library(self, _val):
        run = self._run(
            self._script("from demo_lib.pipeline import run\nprint(run())\n")
        )
        stamp_library_versions(run)
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("hello world", run.stdout)

    @_stub_python
    def test_queued_run_is_pinned_against_a_later_edit(self, _val):
        # THE property the revision design exists for: edit after queue, run still
        # executes the stamped revision.
        run = self._run(
            self._script("from demo_lib.pipeline import run\nprint(run())\n")
        )
        stamp_library_versions(run)
        self.library.save_revision(
            {
                "helper.py": "def greet(name):\n    return 'EDITED'\n",
                "pipeline.py": HELPER_MODULES["pipeline.py"],
            }
        )

        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("hello world", run.stdout)
        self.assertNotIn("EDITED", run.stdout)

    @_stub_python
    def test_deleted_library_fails_run_with_named_error(self, _val):
        run = self._run(self._script("from demo_lib.pipeline import run\n"))
        stamp_library_versions(run)
        self.library.delete()

        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertEqual(run.exit_code, -1)
        # Named cause, not a raw ImportError traceback from inside the script.
        self.assertIn("demo_lib", run.stderr)
        self.assertIn("no longer exists", run.stderr)

    @_stub_python
    def test_staging_dir_is_cleaned_up(self, _val):
        from django.conf import settings

        run = self._run(self._script("print('ok')"))
        stamp_library_versions(run)
        execute_run(run)
        leftovers = [
            p for p in os.listdir(settings.SCRIPTS_WORKDIR) if p.startswith("libs-")
        ]
        self.assertEqual(leftovers, [])

    @_stub_python
    def test_library_cannot_shadow_a_pyrunner_helper(self, _val):
        # Belt (reserved-key ban) and braces (PYTHONPATH order): even with the ban
        # bypassed at the DB level, the helper still wins the import.
        Library.objects.filter(pk=self.library.pk).update(key="pyrunner_datastore")
        run = self._run(
            self._script("import pyrunner_datastore\nprint(pyrunner_datastore.__file__)\n")
        )
        run.library_versions = {"pyrunner_datastore": 1}
        run.save(update_fields=["library_versions"])

        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("script_helpers", run.stdout)


class SandboxStagingTests(TestCase):
    """An isolated run must be able to import its libraries — for free.

    The staging dir lives under the workdir, which the sandbox already binds
    writable into the jail, so no library-specific sandbox wiring exists (or
    should ever need to).
    """

    def _spec(self, library_root):
        from core.executor_backends import RunSpec

        return RunSpec(
            cmd=["/opt/venv/bin/python", "/work/script.py"],
            env={"PYTHONPATH": os.pathsep.join(["/app/core/script_helpers", library_root])},
            cwd="/work",
        )

    def test_staging_dir_reaches_the_jail_via_the_rw_workdir(self):
        from core.executor_backends import sandboxed

        spec = self._spec("/work/libs-abc")
        with mock.patch.object(sandboxed.os.path, "isdir", return_value=True):
            dirs = sandboxed._ro_bind_dirs(spec)
            argv = sandboxed.build_bwrap_argv("/usr/bin/bwrap", spec)

        # Not ro-bound: it is inside the workdir, which is bound rw below.
        self.assertNotIn("/work/libs-abc", dirs)
        self.assertIn("/app/core/script_helpers", dirs)
        # The rw workdir bind is what carries it in.
        self.assertIn("--bind", argv)
        self.assertEqual(argv[argv.index("--bind") + 1], "/work")

    def test_library_root_outside_the_workdir_is_still_ro_bound(self):
        # Defensive: if the staging dir ever moves out of the workdir, imports
        # must still resolve rather than silently breaking isolated runs.
        from core.executor_backends import sandboxed

        with mock.patch.object(sandboxed.os.path, "isdir", return_value=True):
            dirs = sandboxed._ro_bind_dirs(self._spec("/elsewhere/libs-abc"))
        self.assertIn("/elsewhere/libs-abc", dirs)


class Stage1AcceptanceTests(TestCase):
    """The plan's Stage 1 bar, as one unbroken chain.

    SDK-provisioned two-module library → attached to a script → queued through the
    real queue path → executed as a real subprocess that imports it. Each link is
    unit-tested above; this asserts they compose, which is the thing that actually
    ships.
    """

    @_stub_python
    def test_sdk_provisioned_library_is_imported_by_a_real_run(self, _val):
        ws = Workspace.get_default()
        env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:10]}")
        libs = LibraryAPI("demoplugin", workspace=ws)

        library = libs.upsert("demoplugin_lib", modules=HELPER_MODULES, name="Pipeline")
        script = Script.objects.create(
            name="worker",
            code="from demoplugin_lib.pipeline import run\nprint(run())\n",
            environment=env,
            workspace=ws,
        )
        libs.attach(script, library)

        run = Run.objects.create(script=script, workspace=ws, status=Run.Status.PENDING)
        with mock.patch("core.tasks.async_task", return_value="task-1"):
            from core.tasks import queue_script_run

            queue_script_run(run)

        run.refresh_from_db()
        self.assertEqual(run.library_versions, {"demoplugin_lib": 1})

        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("hello world", run.stdout)


class LibraryAPITests(TestCase):
    OWNER = "myplugin"

    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="t", path=f"env{uuid.uuid4().hex[:10]}"
        )
        self.api = LibraryAPI(self.OWNER, workspace=self.ws)

    def test_upsert_creates_owned_library_with_first_revision(self):
        lib = self.api.upsert("myplugin_lib", modules=HELPER_MODULES, name="Pipeline")
        self.assertEqual(lib.owner_plugin, self.OWNER)
        self.assertEqual(lib.owner_key, "myplugin_lib")
        self.assertEqual(lib.workspace, self.ws)
        self.assertEqual(lib.current_version, 1)

    def test_upsert_is_idempotent_by_content(self):
        # Re-provisioning (every settings save) must not churn revisions.
        self.api.upsert("myplugin_lib", modules=HELPER_MODULES)
        lib = self.api.upsert("myplugin_lib", modules=dict(HELPER_MODULES))
        self.assertEqual(lib.current_version, 1)
        self.assertEqual(Library.objects.filter(key="myplugin_lib").count(), 1)
        self.assertEqual(lib.revisions.count(), 1)

    def test_upsert_new_content_makes_a_revision(self):
        self.api.upsert("myplugin_lib", modules=HELPER_MODULES)
        lib = self.api.upsert("myplugin_lib", modules={"helper.py": "x = 2"})
        self.assertEqual(lib.current_version, 2)
        self.assertEqual(lib.revisions.count(), 2)

    def test_upsert_rejects_reserved_and_invalid_keys(self):
        with self.assertRaises(ValueError):
            self.api.upsert("pyrunner_db", modules=HELPER_MODULES)
        with self.assertRaises(ValueError):
            self.api.upsert("Bad-Key", modules=HELPER_MODULES)

    def test_upsert_will_not_hijack_another_owners_key(self):
        LibraryAPI("otherplugin", workspace=self.ws).upsert(
            "shared_lib", modules=HELPER_MODULES
        )
        with self.assertRaises(ValueError) as ctx:
            self.api.upsert("shared_lib", modules=HELPER_MODULES)
        self.assertIn("otherplugin", str(ctx.exception))

    def test_upsert_renames_the_import_key_in_place(self):
        # owner_key (not key) is the idempotency handle, so a plugin can change
        # the import name without orphaning the row or its history.
        self.api.upsert("old_name_lib", modules=HELPER_MODULES, owner_key="pipeline")
        lib = self.api.upsert("new_name_lib", modules=HELPER_MODULES, owner_key="pipeline")
        self.assertEqual(Library.objects.filter(owner_plugin=self.OWNER).count(), 1)
        self.assertEqual(lib.key, "new_name_lib")
        self.assertEqual(lib.current_version, 1)  # content unchanged => no churn

    def test_upsert_rename_onto_a_taken_key_is_refused(self):
        LibraryAPI("otherplugin", workspace=self.ws).upsert(
            "taken_lib", modules=HELPER_MODULES
        )
        self.api.upsert("mine_lib", modules=HELPER_MODULES, owner_key="pipeline")
        with self.assertRaises(ValueError) as ctx:
            self.api.upsert("taken_lib", modules=HELPER_MODULES, owner_key="pipeline")
        self.assertIn("otherplugin", str(ctx.exception))

    def test_owner_scoping_isolates_get_and_list(self):
        self.api.upsert("myplugin_lib", modules=HELPER_MODULES)
        other = LibraryAPI("otherplugin", workspace=self.ws)
        self.assertIsNone(other.get("myplugin_lib"))
        self.assertEqual(other.list(), [])
        self.assertEqual(len(self.api.list()), 1)

    def test_legacy_lane_creates_unowned_library(self):
        lib = LibraryAPI(workspace=self.ws).upsert("user_lib", modules=HELPER_MODULES)
        self.assertIsNone(lib.owner_plugin)
        self.assertIsNone(lib.owner_key)

    def test_attach_is_idempotent_and_detach_removes(self):
        lib = self.api.upsert("myplugin_lib", modules=HELPER_MODULES)
        script = Script.objects.create(
            name="s", code="pass", environment=self.env, workspace=self.ws
        )
        first = self.api.attach(script, lib)
        second = self.api.attach(script, lib)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ScriptLibrary.objects.filter(script=script).count(), 1)

        self.assertTrue(self.api.detach(script, lib))
        self.assertFalse(self.api.detach(script, lib))
        self.assertEqual(ScriptLibrary.objects.filter(script=script).count(), 0)

    def test_attach_rejects_cross_workspace(self):
        other_ws = Workspace.objects.create(name="Other")
        lib = self.api.upsert("myplugin_lib", modules=HELPER_MODULES)
        script = Script.objects.create(
            name="s", code="pass", environment=self.env, workspace=other_ws
        )
        with self.assertRaises(ValueError):
            self.api.attach(script, lib)
