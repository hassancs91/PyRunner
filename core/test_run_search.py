"""
Text search on the runs list (roadmap 2.7).

``?q=`` matches the script name and stderr by default; stdout joins only with
``?deep=1`` because it is the large column. The filter runs in SQL (stdout
stays deferred from the SELECT), composes with the status/script filters,
respects workspace scoping, and survives switching the other filters.
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


class RunSearchTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="runsearch@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)
        env = Environment.objects.create(name="e-search", path="p-search")
        self.sync = Script.objects.create(
            name="Nightly sync", code="pass", environment=env, workspace=self.ws
        )
        self.report = Script.objects.create(
            name="Weekly report", code="pass", environment=env, workspace=self.ws
        )
        self.failed = Run.objects.create(
            script=self.sync, workspace=self.ws, status=Run.Status.FAILED,
            stderr="KeyError: 'customer_id'", stdout="fetched 10 orders",
        )
        self.ok = Run.objects.create(
            script=self.sync, workspace=self.ws, status=Run.Status.SUCCESS,
            stderr="", stdout="processed 42 orders",
        )
        self.other = Run.objects.create(
            script=self.report, workspace=self.ws, status=Run.Status.SUCCESS,
            stderr="", stdout="report sent",
        )

    def _ids(self, **params):
        resp = self.client.get(reverse("cpanel:run_list"), params)
        self.assertEqual(resp.status_code, 200)
        return {str(r.pk) for r in resp.context["runs"]}

    def test_no_query_lists_everything(self):
        self.assertEqual(self._ids(), {str(self.failed.pk), str(self.ok.pk), str(self.other.pk)})

    def test_matches_stderr(self):
        self.assertEqual(self._ids(q="keyerror"), {str(self.failed.pk)})

    def test_matches_script_name(self):
        self.assertEqual(self._ids(q="weekly"), {str(self.other.pk)})

    def test_stdout_excluded_by_default(self):
        self.assertEqual(self._ids(q="processed"), set())

    def test_stdout_included_with_deep(self):
        self.assertEqual(self._ids(q="processed", deep="1"), {str(self.ok.pk)})

    def test_composes_with_status_filter(self):
        # "sync" matches both runs of the Nightly sync script by name …
        self.assertEqual(self._ids(q="sync"), {str(self.failed.pk), str(self.ok.pk)})
        # … and the status tab narrows it.
        self.assertEqual(self._ids(q="sync", status="failed"), {str(self.failed.pk)})

    def test_composes_with_script_filter(self):
        self.assertEqual(self._ids(q="orders", deep="1", script=str(self.report.pk)), set())

    def test_scoped_to_workspace(self):
        other_ws = Workspace.objects.create(name="Elsewhere")
        env = Environment.objects.create(name="e-other", path="p-other")
        foreign = Script.objects.create(
            name="Foreign", code="pass", environment=env, workspace=other_ws
        )
        Run.objects.create(
            script=foreign, workspace=other_ws, status=Run.Status.FAILED,
            stderr="KeyError: leaked",
        )
        self.assertEqual(self._ids(q="keyerror"), {str(self.failed.pk)})

    def test_query_is_capped(self):
        resp = self.client.get(reverse("cpanel:run_list"), {"q": "x" * 500})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["search_query"]), 200)

    def test_stdout_stays_deferred_even_when_searched(self):
        resp = self.client.get(reverse("cpanel:run_list"), {"q": "processed", "deep": "1"})
        run = list(resp.context["runs"])[0]
        self.assertIn("stdout", run.get_deferred_fields())

    def test_status_tabs_and_script_picker_keep_the_query(self):
        resp = self.client.get(
            reverse("cpanel:run_list"), {"q": "sync", "deep": "1", "status": "failed"}
        )
        body = resp.content.decode()
        # The "Success" tab link carries q + deep but drops the old status.
        self.assertIn("?status=success&q=sync&amp;deep=1", body)
        # The hidden inputs on the search form carry the active status.
        self.assertIn('name="status" value="failed"', body)
        self.assertIn('value="sync"', body)

    def test_empty_state_names_the_query(self):
        resp = self.client.get(reverse("cpanel:run_list"), {"q": "nothing-here"})
        body = resp.content.decode()
        self.assertIn("No runs match", body)
        self.assertIn("nothing-here", body)
        self.assertIn("Include output", body)
