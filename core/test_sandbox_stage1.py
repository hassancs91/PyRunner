"""
Sandbox Stage 1 — rlimits (the MVP) + dashboard-managed config.

FOUNDATIONS Seam 2, isolation layer, Stage 1: per-run POSIX resource caps,
configured from the dashboard (``GlobalSettings``) and resolved at execution
time (no restart, no env reliance). The default (sandbox off, every rlimit 0)
instance is byte-for-byte today's behavior.

Two tiers of tests:
- **Cross-platform** (always run): the DB-driven resolver, the DB→``RunSpec``
  wiring, the Windows no-op guard, that a normal run is unaffected when limits
  are configured, and that a default instance carries NO limits.
- **POSIX-only** (``skipUnless``; run on the Linux stack/CI, skipped on the
  Windows dev box): the limits are actually installed in the child and a memory
  bomb / CPU loop is killed. ``setrlimit`` is POSIX-only — on Windows the
  backend never attaches the ``preexec_fn`` at all, which the no-op test proves.
"""

import os
import subprocess
import sys
import uuid
from unittest import mock, skipUnless

from django.test import TestCase, override_settings

from core.executor import _resolve_run_limits, _run_resource_limits, execute_run
from core.executor_backends import LocalSubprocessBackend, RunSpec
from core.executor_backends.local import _make_rlimit_preexec
from core.models import Environment, GlobalSettings, Run, Script

MB = 1024 * 1024


def _make_run(code: str, timeout: int = 3600) -> Run:
    env = Environment.objects.create(name="t", path=f"env{uuid.uuid4().hex[:10]}")
    script = Script.objects.create(
        name="s", code=code, environment=env, timeout_seconds=timeout
    )
    return Run.objects.create(script=script, status=Run.Status.PENDING)


def _stub_python(func):
    """Decorator: make _validate_environment return the test runner's Python."""
    return mock.patch(
        "core.executor._validate_environment", return_value=sys.executable
    )(func)


def _set_limits(memory_mb=0, cpu_seconds=0, nproc=0, fsize_mb=0):
    gs = GlobalSettings.get_settings()
    gs.sandbox_rlimit_memory_mb = memory_mb
    gs.sandbox_rlimit_cpu_seconds = cpu_seconds
    gs.sandbox_rlimit_nproc = nproc
    gs.sandbox_rlimit_fsize_mb = fsize_mb
    gs.save()
    return gs


class _RecordingLocalBackend(LocalSubprocessBackend):
    """Local backend that records the RunSpec it was handed, then runs it."""

    last_spec = None

    def start(self, spec):
        type(self).last_spec = spec
        return super().start(spec)


# ---------------------------------------------------------------------------
# Resolver: DB-driven, env fallback, fsize is DB-only — all cross-platform.
# ---------------------------------------------------------------------------
class ResolveRunLimitsTests(TestCase):
    def test_default_instance_has_no_limits(self):
        # Fresh GlobalSettings (all 0) + no env vars -> None == today's behavior.
        self.assertIsNone(_resolve_run_limits())

    def test_db_values_are_resolved(self):
        _set_limits(memory_mb=256, cpu_seconds=10, nproc=64, fsize_mb=5)
        self.assertEqual(
            _resolve_run_limits(),
            {
                "memory_bytes": 256 * MB,
                "cpu_seconds": 10,
                "nproc": 64,
                "fsize_bytes": 5 * MB,
            },
        )

    def test_fsize_is_db_only(self):
        # There is no PYRUNNER_RUN_RLIMIT_FSIZE env var; fsize comes only from DB.
        _set_limits(fsize_mb=7)
        self.assertEqual(_resolve_run_limits(), {"fsize_bytes": 7 * MB})

    @override_settings(
        PYRUNNER_RUN_RLIMIT_MEMORY_MB=128,
        PYRUNNER_RUN_RLIMIT_CPU_SECONDS=20,
        PYRUNNER_RUN_RLIMIT_NPROC=32,
    )
    def test_env_is_fallback_when_db_unset(self):
        # DB all 0 -> legacy env vars supply the caps (break-glass back-compat).
        self.assertEqual(
            _resolve_run_limits(),
            {"memory_bytes": 128 * MB, "cpu_seconds": 20, "nproc": 32},
        )

    @override_settings(
        PYRUNNER_RUN_RLIMIT_MEMORY_MB=128,
        PYRUNNER_RUN_RLIMIT_CPU_SECONDS=20,
        PYRUNNER_RUN_RLIMIT_NPROC=32,
    )
    def test_db_overrides_env_per_field(self):
        # DB wins for the field it sets; env still fills the field DB leaves 0.
        _set_limits(memory_mb=512)  # only memory in DB
        self.assertEqual(
            _resolve_run_limits(),
            {"memory_bytes": 512 * MB, "cpu_seconds": 20, "nproc": 32},
        )

    def test_legacy_env_helper_unchanged(self):
        # The env-only helper still reads settings (the postgres-readiness path).
        self.assertIsNone(_run_resource_limits())
        with override_settings(PYRUNNER_RUN_RLIMIT_MEMORY_MB=64):
            self.assertEqual(_run_resource_limits(), {"memory_bytes": 64 * MB})


# ---------------------------------------------------------------------------
# DB -> RunSpec wiring + byte-for-byte default — cross-platform.
# ---------------------------------------------------------------------------
class RunSpecWiringTests(TestCase):
    @_stub_python
    def test_default_run_carries_no_limits(self, _val):
        backend = _RecordingLocalBackend()
        with mock.patch("core.executor.get_run_backend", return_value=backend):
            run = _make_run("print('ok')")
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        # The decisive byte-for-byte assertion: a default instance spawns with
        # limits=None, exactly as before this feature existed.
        self.assertIsNone(backend.last_spec.limits)

    @_stub_python
    def test_db_limits_flow_into_spec(self, _val):
        _set_limits(memory_mb=256, cpu_seconds=10, nproc=64, fsize_mb=5)
        backend = _RecordingLocalBackend()
        with mock.patch("core.executor.get_run_backend", return_value=backend):
            run = _make_run("print('ok')")
            execute_run(run)
        self.assertEqual(
            backend.last_spec.limits,
            {
                "memory_bytes": 256 * MB,
                "cpu_seconds": 10,
                "nproc": 64,
                "fsize_bytes": 5 * MB,
            },
        )


# ---------------------------------------------------------------------------
# A normal run is unaffected; secrets + datastore plumbing intact — x-platform.
# (Generous limits don't interfere on Linux; they no-op on Windows.)
# ---------------------------------------------------------------------------
class NormalRunUnaffectedTests(TestCase):
    @_stub_python
    def test_normal_script_succeeds_with_limits_configured(self, _val):
        _set_limits(memory_mb=2048, cpu_seconds=60, nproc=256, fsize_mb=64)
        run = _make_run("print('hello world')")
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertEqual(run.exit_code, 0)
        self.assertIn("hello world", run.stdout)

    @_stub_python
    @mock.patch(
        "core.executor.resolve_secrets_for_run",
        return_value={"MY_SECRET": "supersecretvalue12"},
    )
    def test_secrets_still_inject_and_mask_with_limits(self, _secrets, _val):
        _set_limits(memory_mb=2048, cpu_seconds=60)
        run = _make_run("import os; print(os.environ['MY_SECRET'])")
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertNotIn("supersecretvalue12", run.stdout)
        self.assertIn("[MY_SECRET:MASKED]", run.stdout)

    def test_datastore_env_plumbing_unaffected_by_limits(self):
        # Configuring isolation must not disturb how a run reaches its datastore.
        from core.executor import _build_script_environment

        _set_limits(memory_mb=512, fsize_mb=8)
        run = _make_run("pass")
        env = _build_script_environment(run=run)
        # script_helpers on PYTHONPATH (so `import pyrunner_datastore` works) and
        # the per-run workspace scope are still injected, exactly as before.
        self.assertIn("script_helpers", env["PYTHONPATH"])
        self.assertIn("PYRUNNER_WORKSPACE_ID", env)
        # On SQLite the helper still gets the DB path; on other engines it uses
        # the internal API. Either way the datastore reach is intact.
        from django.db import connection

        if connection.vendor == "sqlite":
            self.assertIn("PYRUNNER_DB_PATH", env)


# ---------------------------------------------------------------------------
# Windows no-op: the backend never attaches a preexec_fn on nt, even with
# limits configured (setrlimit is POSIX-only).
# ---------------------------------------------------------------------------
class WindowsNoOpTests(TestCase):
    @skipUnless(os.name == "nt", "Windows-specific no-op guard")
    def test_no_preexec_on_windows(self):
        backend = LocalSubprocessBackend()
        spec = RunSpec(
            cmd=[sys.executable, "-c", "pass"],
            env=dict(os.environ),
            cwd=os.getcwd(),
            limits={"memory_bytes": 64 * MB, "cpu_seconds": 5},
        )
        with mock.patch(
            "core.executor_backends.local.subprocess.Popen"
        ) as mpopen:
            mpopen.return_value = mock.Mock(pid=4321)
            backend.start(spec)
        _, kwargs = mpopen.call_args
        self.assertNotIn("preexec_fn", kwargs)  # never on Windows
        self.assertIn("creationflags", kwargs)  # the nt isolation path

    @skipUnless(os.name == "posix", "posix attaches the preexec_fn")
    def test_preexec_attached_on_posix_when_limited(self):
        backend = LocalSubprocessBackend()
        spec = RunSpec(
            cmd=[sys.executable, "-c", "pass"],
            env=dict(os.environ),
            cwd=os.getcwd(),
            limits={"memory_bytes": 512 * MB},
        )
        with mock.patch(
            "core.executor_backends.local.subprocess.Popen"
        ) as mpopen:
            mpopen.return_value = mock.Mock(pid=4321)
            backend.start(spec)
        _, kwargs = mpopen.call_args
        self.assertIn("preexec_fn", kwargs)
        self.assertTrue(callable(kwargs["preexec_fn"]))

    @skipUnless(os.name == "posix", "posix only: no limits -> no preexec")
    def test_no_preexec_when_unlimited_on_posix(self):
        backend = LocalSubprocessBackend()
        spec = RunSpec(
            cmd=[sys.executable, "-c", "pass"],
            env=dict(os.environ),
            cwd=os.getcwd(),
            limits=None,
        )
        with mock.patch(
            "core.executor_backends.local.subprocess.Popen"
        ) as mpopen:
            mpopen.return_value = mock.Mock(pid=4321)
            backend.start(spec)
        _, kwargs = mpopen.call_args
        self.assertNotIn("preexec_fn", kwargs)


# ---------------------------------------------------------------------------
# POSIX enforcement: the caps are installed in the child, and bombs are killed.
# Skipped on Windows (no setrlimit); validated on the Linux stack / CI.
# ---------------------------------------------------------------------------
@skipUnless(os.name == "posix", "rlimits (setrlimit) are POSIX-only")
class PosixRlimitEnforcementTests(TestCase):
    def test_preexec_installs_all_four_caps(self):
        """Deterministic proof (no bomb): the preexec sets RLIMIT_AS/CPU/NPROC/
        FSIZE in a forked child to exactly the configured values."""
        import resource

        limits = {
            "memory_bytes": 600 * MB,
            "cpu_seconds": 5,
            "nproc": 64,
            "fsize_bytes": 10 * MB,
        }
        preexec = _make_rlimit_preexec(limits)
        pid = os.fork()
        if pid == 0:  # child
            try:
                preexec()
                ok = (
                    resource.getrlimit(resource.RLIMIT_AS) == (600 * MB, 600 * MB)
                    and resource.getrlimit(resource.RLIMIT_CPU) == (5, 5)
                    and resource.getrlimit(resource.RLIMIT_NPROC) == (64, 64)
                    and resource.getrlimit(resource.RLIMIT_FSIZE)
                    == (10 * MB, 10 * MB)
                )
                os._exit(0 if ok else 1)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    @_stub_python
    def test_memory_bomb_is_killed(self, _val):
        # Cap address space at 1 GB: Python boots, but a 2 GB allocation is refused.
        _set_limits(memory_mb=1024)
        run = _make_run(
            "print('BOOT_OK', flush=True)\n"
            "x = bytearray(2 * 1024 * 1024 * 1024)\n"
            "print('ALLOC_OK')\n"
        )
        execute_run(run)
        run.refresh_from_db()
        self.assertIn("BOOT_OK", run.stdout or "")  # interpreter started under the cap
        self.assertNotIn("ALLOC_OK", run.stdout or "")  # the 2 GB alloc was refused
        self.assertNotEqual(run.status, Run.Status.SUCCESS)

    @_stub_python
    def test_cpu_loop_is_killed(self, _val):
        # CPU-time cap of 1s kills a busy loop well before the 60s wall timeout,
        # so it FAILS via SIGXCPU rather than timing out.
        _set_limits(cpu_seconds=1)
        run = _make_run(
            "print('BOOT_OK', flush=True)\nwhile True:\n    pass\n", timeout=60
        )
        execute_run(run)
        run.refresh_from_db()
        self.assertIn("BOOT_OK", run.stdout or "")
        self.assertEqual(run.status, Run.Status.FAILED)  # killed, not wall-timeout
        self.assertNotEqual(run.exit_code, 0)


# ---------------------------------------------------------------------------
# The dashboard form round-trips and the POST view persists — cross-platform.
# ---------------------------------------------------------------------------
class ExecutionIsolationFormTests(TestCase):
    def test_form_saves_to_global_settings(self):
        from core.forms import ExecutionIsolationForm

        gs = GlobalSettings.get_settings()
        form = ExecutionIsolationForm(
            data={
                "sandbox_default": "optional",
                "sandbox_fail_closed": "on",
                "sandbox_rlimit_memory_mb": 256,
                "sandbox_rlimit_cpu_seconds": 10,
                "sandbox_rlimit_nproc": 64,
                "sandbox_rlimit_fsize_mb": 5,
            },
            instance=gs,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(gs)
        gs.refresh_from_db()
        self.assertEqual(gs.sandbox_default, "optional")
        self.assertTrue(gs.sandbox_fail_closed)
        self.assertEqual(gs.sandbox_rlimit_memory_mb, 256)
        self.assertEqual(gs.sandbox_rlimit_fsize_mb, 5)
        self.assertTrue(gs.sandbox_rlimits_configured())

    def test_negative_limit_rejected(self):
        from core.forms import ExecutionIsolationForm

        form = ExecutionIsolationForm(
            data={
                "sandbox_default": "off",
                "sandbox_rlimit_memory_mb": -1,
                "sandbox_rlimit_cpu_seconds": 0,
                "sandbox_rlimit_nproc": 0,
                "sandbox_rlimit_fsize_mb": 0,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("sandbox_rlimit_memory_mb", form.errors)


class ExecutionIsolationViewTests(TestCase):
    def setUp(self):
        from django.urls import reverse

        from core.models import User

        # Bypass the setup-wizard middleware (mirrors the tenancy view tests).
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.url = reverse("cpanel:execution_isolation_settings")
        self.admin = User.objects.create(
            email="admin@example.com", is_staff=True, is_superuser=True
        )
        self.member = User.objects.create(email="m@example.com")

    def test_post_persists_and_redirects(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            self.url,
            {
                "sandbox_default": "required",
                "sandbox_rlimit_memory_mb": 512,
                "sandbox_rlimit_cpu_seconds": 30,
                "sandbox_rlimit_nproc": 100,
                "sandbox_rlimit_fsize_mb": 16,
            },
        )
        self.assertEqual(resp.status_code, 302)
        gs = GlobalSettings.get_settings()
        self.assertEqual(gs.sandbox_default, "required")
        self.assertEqual(gs.sandbox_rlimit_memory_mb, 512)
        self.assertEqual(gs.sandbox_rlimit_fsize_mb, 16)

    def test_requires_superuser(self):
        self.client.force_login(self.member)
        resp = self.client.post(self.url, {"sandbox_default": "required"})
        # superuser_required redirects non-superusers away; settings unchanged.
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(GlobalSettings.get_settings().sandbox_default, "off")
