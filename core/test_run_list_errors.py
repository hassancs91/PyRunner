"""
Inline error expander on the runs list (cpanel/runs/list.html).

A failed run's stderr is readable from the list itself — expand the row instead
of opening the detail page. Covers:
- a run with stderr renders the toggle + its error text (last-10-lines preview);
- a clean run renders no toggle at all;
- stdout stays deferred: the list must never ship it, however large.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Environment,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "script.py", line 2, in <module>\n'
    "    1 / 0\n"
    "ZeroDivisionError: division by zero"
)


class RunListErrorExpanderTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="runlist@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)
        env = Environment.objects.create(name="e-runlist", path="p-runlist")
        self.script = Script.objects.create(
            name="s-runlist", code="1 / 0", environment=env, workspace=self.ws
        )

    def _run(self, status, **kwargs):
        return Run.objects.create(
            script=self.script, status=status, workspace=self.ws, **kwargs
        )

    def _get_list(self):
        resp = self.client.get(reverse("cpanel:run_list"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_failed_run_shows_toggle_and_error_text(self):
        run = self._run(Run.Status.FAILED, stderr=TRACEBACK, exit_code=1)

        body = self._get_list()

        self.assertIn(f'data-error-toggle="run-err-{run.pk}"', body)
        self.assertIn(f'id="run-err-{run.pk}"', body)
        self.assertIn("ZeroDivisionError: division by zero", body)
        self.assertIn("exit 1", body)

    def test_clean_run_shows_no_toggle(self):
        self._run(Run.Status.SUCCESS, stdout="all good", exit_code=0)

        body = self._get_list()

        # Assert the attribute-with-value form, as the sibling test does: the
        # bare name also appears in the page's own querySelectorAll wiring, which
        # ships on every render and says nothing about the rows.
        self.assertNotIn('data-error-toggle="run-err-', body)
        self.assertNotIn("run-err-", body)

    def test_long_stderr_is_truncated_to_the_tail(self):
        # The exception is on the last line — the preview must keep the tail,
        # which is the whole point of surfacing it inline.
        noise = "\n".join(f"frame {i}" for i in range(50))
        self._run(Run.Status.FAILED, stderr=f"{noise}\nBoomError: the real cause")

        body = self._get_list()

        self.assertIn("BoomError: the real cause", body)
        self.assertNotIn("frame 0", body)

    def test_stdout_is_never_shipped_to_the_list(self):
        # Guards the .defer("stdout") in run_list_view: if a future edit reads
        # run.stdout in the template it silently becomes a query per row.
        self._run(Run.Status.FAILED, stdout="SECRET-STDOUT-PAYLOAD", stderr=TRACEBACK)

        body = self._get_list()

        self.assertNotIn("SECRET-STDOUT-PAYLOAD", body)
