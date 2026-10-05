"""
Server-rendered chart geometry for the console.

Templates draw inline SVG from the numbers computed here, so there is no
charting dependency and no JavaScript: the view calls a function, the
template loops over bars. Keep these pure (no ORM, no request) so they are
trivial to unit-test.
"""

from core.formatting import format_duration

DURATION_CHART_WIDTH = 320
DURATION_CHART_HEIGHT = 72
DURATION_CHART_MAX_BARS = 30
# Space kept above the tallest bar so it never touches the top edge.
_TOP_PAD = 6
# A zero-second run still gets a visible sliver.
_MIN_BAR_HEIGHT = 2
# Bars widen to fill the slot but stop here so two runs don't become slabs.
_MAX_BAR_WIDTH = 24


def duration_chart(runs, *, width=DURATION_CHART_WIDTH, height=DURATION_CHART_HEIGHT):
    """Bar geometry for a run-duration chart.

    ``runs`` are finished runs (``duration`` not None) in **chronological**
    order; the caller limits them to ``DURATION_CHART_MAX_BARS``. Returns a dict
    the template renders directly: one entry per bar with its rectangle, the
    run's status and pk, and a tooltip label; plus the average line's y and
    the summary figures. An empty input yields ``count == 0`` and no bars.
    """
    runs = [r for r in runs if r.duration is not None][-DURATION_CHART_MAX_BARS:]
    if not runs:
        return {"count": 0, "bars": [], "width": width, "height": height}

    durations = [r.duration for r in runs]
    max_seconds = max(durations)
    avg_seconds = sum(durations) / len(durations)
    usable = height - _TOP_PAD

    def bar_height(seconds):
        if max_seconds <= 0:
            return _MIN_BAR_HEIGHT
        return max(_MIN_BAR_HEIGHT, round(seconds / max_seconds * usable, 1))

    slot = width / len(runs)
    bar_width = min(round(slot * 0.7, 1), _MAX_BAR_WIDTH)
    bars = []
    for index, run in enumerate(runs):
        h = bar_height(run.duration)
        started = run.started_at.strftime("%b %d, %H:%M") if run.started_at else ""
        bars.append(
            {
                "pk": run.pk,
                "status": run.status,
                "x": round(index * slot + (slot - bar_width) / 2, 1),
                "y": round(height - h, 1),
                "w": bar_width,
                "h": h,
                "label": f"{format_duration(run.duration)} · {started} · {run.status}".strip(" ·"),
            }
        )

    slowest = max(runs, key=lambda r: r.duration)
    return {
        "count": len(runs),
        "bars": bars,
        "width": width,
        "height": height,
        "avg_y": round(height - bar_height(avg_seconds), 1),
        "avg_label": format_duration(avg_seconds),
        "max_label": format_duration(max_seconds),
        "last_label": format_duration(durations[-1]),
        "slowest_pk": slowest.pk,
    }
