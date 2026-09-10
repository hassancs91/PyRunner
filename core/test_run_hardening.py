"""
Step 5 — per-run hardening (RunBackend Stage 2).

PyRun's own infra secrets are filtered out of a script's environment, while
the user's Secrets still inject; and optional posix resource caps are wired
through the RunSpec (off by default, so today's behavior is unchanged).
"""

import os
from unittest import mock

from django.test import TestCase, override_settings

from core.executor import _build_script_environment, _run_resource_limits
from core.models import Environment, Run, Script


def _make_run():
    env = Environment.objects.create(name="e", path="hardenv")
    script = Script.objects.create(name="s", code="x", environment=env)
    return Run.objects.create(script=script)


class RunEnvDenylistTests(TestCase):
    def test_infra_secrets_filtered_from_run_env(self):
        # ENCRYPTION_KEY / SECRET_KEY are in the worker's os.environ (loaded from
        # .env) but must never reach a script.
        self.assertIn("ENCRYPTION_KEY", os.environ)
        built = _build_script_environment(run=_make_run())
        self.assertNotIn("ENCRYPTION_KEY", built)
        self.assertNotIn("SECRET_KEY", built)

    @mock.patch("core.executor.resolve_secrets_for_run", return_value={"MY_API": "v"})
    def test_user_secrets_still_inject(self, _s):
        built = _build_script_environment(run=_make_run())
        self.assertEqual(built["MY_API"], "v")

    @mock.patch("core.executor.resolve_secrets_for_run", return_value={"ENCRYPTION_KEY": "userval"})
    def test_user_secret_named_like_infra_overrides_after_filter(self, _s):
        # The host ENCRYPTION_KEY is dropped first, then the user's Secret of the
        # same name injects — so the user's value wins, not the host's.
        built = _build_script_environment(run=_make_run())
        self.assertEqual(built["ENCRYPTION_KEY"], "userval")


class RunResourceLimitsTests(TestCase):
    def test_off_by_default(self):
        self.assertIsNone(_run_resource_limits())

    @override_settings(
        PYRUNNER_RUN_RLIMIT_MEMORY_MB=256,
        PYRUNNER_RUN_RLIMIT_CPU_SECONDS=10,
        PYRUNNER_RUN_RLIMIT_NPROC=64,
    )
    def test_built_from_settings(self):
        self.assertEqual(
            _run_resource_limits(),
            {"memory_bytes": 256 * 1024 * 1024, "cpu_seconds": 10, "nproc": 64},
        )
