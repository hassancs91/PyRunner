"""
Brand Tracker plugin — views (web process).

Superuser-only. The page is a config form (Settings) that provisions an owned
Script + secrets + data store + weekly schedule through the SDK, plus a focused
dashboard (Mentions) built from the feed + stats the worker writes to the
``brand_tracker:state`` data store.

All persistence/orchestration goes through ``provisioning`` (which uses
``core.plugins.api``), so this module never imports core models/tasks directly.
"""

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import provisioning as prov
from .forms import BrandTrackerConfigForm

superuser_required = user_passes_test(lambda u: u.is_superuser)

FEED_LIMIT = 200    # mentions rendered in the feed
HISTORY_LIMIT = 25  # rows in the run-history table

# source -> (label, hex color). Inline colors so the Tailwind purge never drops them.
_SOURCE = {
    "web": ("Web", "#38bdf8"),
    "news": ("News", "#a78bfa"),
    "hackernews": ("Hacker News", "#fb923c"),
    "reddit": ("Reddit", "#f87171"),
}
_SENTIMENT = {
    "positive": ("Positive", "#34d399"),
    "neutral": ("Neutral", "#94a3b8"),
    "negative": ("Negative", "#f87171"),
}
_RUN_BADGE = {
    "success": ("Success", "bg-ok/10 text-ok", "bg-ok"),
    "partial": ("Partial", "bg-warn/10 text-warn", "bg-warn"),
    "failed": ("Failed", "bg-fail/10 text-fail", "bg-fail"),
}


def _mention_view(raw):
    src = raw.get("source", "")
    label, color = _SOURCE.get(src, (src or "—", "#94a3b8"))
    sentiment = raw.get("sentiment", "")
    s_label, s_color = _SENTIMENT.get(sentiment, ("", ""))
    return {
        "keyword": raw.get("keyword", ""),
        "title": raw.get("title", "(no title)"),
        "url": raw.get("url", "#"),
        "snippet": raw.get("snippet", ""),
        "source_label": label,
        "source_color": color,
        "source_type": raw.get("source_type", ""),
        "sentiment_label": s_label,
        "sentiment_color": s_color,
        "found_at": (raw.get("found_at", "") or "")[:10],
    }


def _run_view(raw):
    status = raw.get("status", "unknown")
    label, cls, dot = _RUN_BADGE.get(status, ("Unknown", "bg-panel-hi text-muted", "bg-muted"))
    by_source = raw.get("by_source", {}) or {}
    return {
        "ts": raw.get("ts", "—"),
        "new_count": raw.get("new_count", 0),
        "badge": {"label": label, "cls": cls, "dot": dot},
        "sources": ", ".join(f"{_SOURCE.get(k, (k, ''))[0]} {v}" for k, v in by_source.items()) or "—",
        "cap_reached": raw.get("cap_reached", False),
        "seeded_run": raw.get("seeded_run", False),
        "errors": raw.get("errors", []),
    }


def _dashboard_context():
    script = prov.get_script()
    cfg = prov.get_config()
    stats = prov.get_stats()
    credits = prov.get_credits()

    mentions = [m for m in prov.get_mentions() if isinstance(m, dict)]
    feed = [_mention_view(m) for m in reversed(mentions)][:FEED_LIMIT]
    runs = [r for r in prov.get_runs() if isinstance(r, dict)]
    history = [_run_view(r) for r in reversed(runs)][:HISTORY_LIMIT]

    by_keyword = sorted((stats.get("by_keyword", {}) or {}).items(), key=lambda kv: -kv[1])
    cap = cfg.get("monthly_credit_cap", 0) or 0
    used = credits.get("estimated_credits", 0) or 0
    return {
        "is_configured": script is not None,
        "has_data": bool(feed),
        "feed": feed,
        "feed_keywords": cfg.get("keywords", []),
        "feed_sources": sorted({m["source_label"] for m in feed}),
        "window_total": stats.get("window_total", len(mentions)),
        "total_all_time": stats.get("total_all_time", 0),
        "last_run": (stats.get("last_run", "") or "")[:16].replace("T", " "),
        "by_keyword": by_keyword,
        "history": history,
        "credit_used": used,
        "credit_cap": cap,
        "credit_pct": min(100, round(used / cap * 100)) if cap else 0,
        "credit_requests": credits.get("serper_requests", 0),
        "can_run": bool(script and script.can_run),
    }


def _build_form(request, data=None):
    return BrandTrackerConfigForm(
        data,
        initial=None if data else prov.initial_from_config(),
        environments=prov.list_environments(),
        configured_secrets=prov.configured_secret_keys(),
    )


def _render(request, form):
    ctx = {
        "form": form,
        "has_environments": bool(prov.list_environments()),
        "schedule": prov.schedule_summary(),
        "is_active": prov.live_status()["active"],
        "report_share": prov.report_share(),
    }
    ctx.update(_dashboard_context())
    return render(request, "brand_tracker/index.html", ctx)


@superuser_required
def index(request):
    return _render(request, _build_form(request))


@superuser_required
@require_POST
def save(request):
    form = _build_form(request, data=request.POST)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return _render(request, form)

    try:
        _, warnings = prov.provision(form.cleaned_data, created_by=request.user)
    except Exception as exc:
        messages.error(request, f"Could not save settings: {exc}")
        return _render(request, form)

    messages.success(request, "Settings saved — the tracker script, secrets and weekly schedule are provisioned.")
    for w in warnings:
        messages.warning(request, w)
    return redirect(reverse("brand_tracker:index") + "#settings")


@superuser_required
@require_POST
def run(request):
    _, error = prov.queue_run(triggered_by=request.user)
    if error:
        messages.error(request, error)
    else:
        messages.info(request, "Tracker queued — watch it run below.")
    return redirect(reverse("brand_tracker:index") + "#mentions")


@superuser_required
def status(request):
    """JSON snapshot for the page's live-status poller (run state + progress)."""
    return JsonResponse(prov.live_status())


@superuser_required
@require_POST
def stop(request):
    if prov.cancel_running():
        messages.info(request, "Stopping the running tracker…")
    else:
        messages.error(request, "There's no running tracker to stop.")
    return redirect(reverse("brand_tracker:index") + "#mentions")


@superuser_required
@require_POST
def test_serper(request):
    """Probe the Serper connection with the submitted (or saved) key."""
    return JsonResponse(prov.test_serper(request.POST))


@superuser_required
@require_POST
def share_report(request):
    """Create (or re-surface) the public report link."""
    url_path = prov.share_report(created_by=request.user)
    messages.success(
        request,
        f"Public report is live at {url_path} — anyone with the link can view it. "
        "Manage it under Settings → API Tokens.",
    )
    return redirect(reverse("brand_tracker:index") + "#mentions")


@superuser_required
@require_POST
def revoke_report(request):
    """Kill the public report link (permanent; re-sharing issues a new URL)."""
    if prov.revoke_report():
        messages.info(request, "Public report link revoked — that URL is permanently dead.")
    else:
        messages.error(request, "There is no public report link to revoke.")
    return redirect(reverse("brand_tracker:index") + "#mentions")
