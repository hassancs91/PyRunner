"""
Duration chart on the script detail page (roadmap 3.8a).

``core.charts.duration_chart`` turns finished runs into bar geometry; the view
feeds it the last 30 finished runs oldest → newest and the template draws an
inline SVG. Covers scaling, the degenerate inputs (no runs, one run, all-zero
durations), status colouring, the bar cap, and the page render.
"""

import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.charts import DURATION_CHART_MAX_BARS, duration_chart
from core.models import (
    Environment,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)


class _FakeRun:
    """Just enough of Run for the pure helper."""

    def __init__(self, seconds, status="success", pk=None, started_at=None):
        self.pk = pk or uuid.uuid4()
        self.status = status
        self.started_at = started_at or timezone.now()
        self.ended_at = self.started_at + timedelta(seconds=seconds) if seconds is not None else None

    @property
    def duration(self):
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class DurationChartHelperTests(TestCase):
    def test_empty(self):
        chart = duration_chart([])
        self.assertEqual(chart["count"], 0)
        self.assertEqual(chart["bars"], [])

    def test_unfinished_runs_are_skipped(self):
        chart = duration_chart([_FakeRun(None), _FakeRun(5)])
        self.assertEqual(chart["count"], 1)

    def test_tallest_bar_fills_the_usable_height_and_order_is_kept(self):
        runs = [_FakeRun(10), _FakeRun(40), _FakeRun(20)]
        chart = duration_chart(runs, width=300, height=72)
        heights = [b["h"] for b in chart["bars"]]
        self.assertEqual(max(heights), heights[1])
        self.assertEqual(heights[1], 66)  # 72 - top pad
        self.assertEqual(heights[0], 16.5)  # 10/40 of 66
        self.assertEqual([b["pk"] for b in chart["bars"]], [r.pk for r in runs])
        # Bars march left → right.
        xs = [b["x"] for b in chart["bars"]]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(chart["slowest_pk"], runs[1].pk)
        self.assertEqual(chart["max_label"], "40.0s")
        self.assertEqual(chart["last_label"], "20.0s")
        self.assertEqual(chart["avg_label"], "23.3s")

    def test_average_line_sits_between_bars(self):
        chart = duration_chart([_FakeRun(10), _FakeRun(30)], height=72)
        ys = [b["y"] for b in chart["bars"]]
        self.assertTrue(min(ys) < chart["avg_y"] < max(ys))

    def test_zero_durations_still_render_a_sliver(self):
        chart = duration_chart([_FakeRun(0), _FakeRun(0)])
        self.assertTrue(all(b["h"] == 2 for b in chart["bars"]))
        self.assertEqual(chart["avg_label"], "0.0s")

    def test_single_run_bar_is_capped_in_width_and_centred(self):
        chart = duration_chart([_FakeRun(3)], width=320)
        bar = chart["bars"][0]
        self.assertEqual(bar["w"], 24)
        self.assertEqual(bar["x"], 148)  # (320 - 24) / 2

    def test_caps_to_the_newest_bars(self):
        runs = [_FakeRun(i + 1) for i in range(DURATION_CHART_MAX_BARS + 5)]
        chart = duration_chart(runs)
        self.assertEqual(chart["count"], DURATION_CHART_MAX_BARS)
        self.assertEqual(chart["bars"][-1]["pk"], runs[-1].pk)

    def test_label_carries_duration_status_and_time(self):
        when = datetime(2026, 10, 5, 14, 2, tzinfo=dt_timezone.utc)
        chart = duration_chart([_FakeRun(90, status="failed", started_at=when)])
        self.assertEqual(chart["bars"][0]["label"], "1m 30s · Oct 05, 14:02 · failed")
        self.assertEqual(chart["bars"][0]["status"], "failed")


class DurationChartPageTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="chart@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)
        env = Environment.objects.create(name="e-chart", path="p-chart")
        self.script = Script.objects.create(
            name="charted", code="pass", environment=env, workspace=self.ws
        )

    def _run(self, seconds, status=Run.Status.SUCCESS):
        start = timezone.now() - timedelta(minutes=10)
        return Run.objects.create(
            script=self.script, workspace=self.ws, status=status,
            started_at=start, ended_at=start + timedelta(seconds=seconds),
        )

    def _page(self):
        resp = self.client.get(reverse("cpanel:script_detail", args=[self.script.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_empty_state_without_finished_runs(self):
        Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.RUNNING)
        body = self._page().content.decode()
        self.assertIn("No finished runs yet", body)
        self.assertNotIn("<rect", body)

    def test_bars_link_to_runs_and_colour_by_status(self):
        ok = self._run(5)
        bad = self._run(12, status=Run.Status.FAILED)
        resp = self._page()
        body = resp.content.decode()
        self.assertIn(reverse("cpanel:run_detail", args=[ok.pk]), body)
        self.assertIn(reverse("cpanel:run_detail", args=[bad.pk]), body)
        self.assertIn("text-fail hover:opacity-70", body)
        self.assertIn("text-ok hover:opacity-70", body)
        self.assertIn("slowest", body)
        self.assertEqual(resp.context["duration_chart"]["count"], 2)
        self.assertEqual(resp.context["duration_chart"]["slowest_pk"], bad.pk)

    def test_only_the_newest_thirty_finished_runs_oldest_first(self):
        runs = [self._run(i + 1) for i in range(DURATION_CHART_MAX_BARS + 3)]
        chart = self._page().context["duration_chart"]
        self.assertEqual(chart["count"], DURATION_CHART_MAX_BARS)
        pks = [b["pk"] for b in chart["bars"]]
        self.assertEqual(pks, [r.pk for r in runs[-DURATION_CHART_MAX_BARS:]])
