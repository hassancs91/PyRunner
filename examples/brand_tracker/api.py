"""
Brand Tracker — external API handlers (/api/v1/plugins/brand_tracker/…).

Core's dispatcher owns auth, rate limits, and workspace scoping; these handlers
just read the owned DataStore ``brand_tracker:state`` for the workspace the
token resolves to and return plain dicts. Import-light lane: this module
imports ONLY ``core.plugins.api`` (see CLAUDE.md contract #2).

Resources (declared in plugin.json "provides.api_resources"):
- ``mentions`` — the deduped mention feed, filterable + paginated.
- ``stats``    — run/source counters the worker maintains.
"""

from core.plugins.api import APIError, DataStoreAPI, page, resource

OWNER = "brand_tracker"
STORE_KEY = "state"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
RUNS_LIMIT = 25  # matches the dashboard's run-history table

# Stable public shape for a mention — new internal fields don't leak until
# they're deliberately added here.
_MENTION_FIELDS = (
    "keyword", "title", "url", "snippet",
    "source", "source_type", "sentiment", "found_at",
)


def _state_store(req):
    store = DataStoreAPI(owner=OWNER, workspace=req.workspace).get(STORE_KEY)
    if store is None:
        raise APIError(
            "Brand Tracker is not configured yet", code="NOT_CONFIGURED", status=409
        )
    return store


def _int_param(req, name, default, *, minimum=1, maximum=None):
    try:
        value = int(req.params.get(name, default))
    except (TypeError, ValueError):
        raise APIError(f"'{name}' must be an integer", code="BAD_PARAM")
    value = max(minimum, value)
    return min(maximum, value) if maximum else value


@resource("mentions")
def mentions(req):
    """The mention feed, newest first.

    Filters (query params): ``keyword``, ``source`` (web|news|hackernews|reddit),
    ``sentiment`` (positive|neutral|negative), ``since`` (ISO date — mentions
    found on/after it). Pagination: ``page`` / ``page_size`` (max 100).
    """
    store = _state_store(req)
    items = store.get("mentions", []) or []
    feed = [m for m in reversed(items) if isinstance(m, dict)]  # newest first

    keyword = req.params.get("keyword")
    if keyword:
        feed = [m for m in feed if m.get("keyword") == keyword]
    source = req.params.get("source")
    if source:
        feed = [m for m in feed if m.get("source") == source]
    sentiment = req.params.get("sentiment")
    if sentiment:
        feed = [m for m in feed if m.get("sentiment") == sentiment]
    since = req.params.get("since")
    if since:
        # found_at is ISO 8601, so string comparison IS date comparison.
        feed = [m for m in feed if (m.get("found_at") or "") >= since]

    page = _int_param(req, "page", 1)
    page_size = _int_param(req, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    total = len(feed)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size

    return {
        "mentions": [
            {f: m.get(f, "") for f in _MENTION_FIELDS}
            for m in feed[start:start + page_size]
        ],
        "count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@resource("stats")
def stats(req):
    """Run/source counters: feed totals, per-keyword/source breakdowns, and the
    recent run history (newest first, same window as the dashboard)."""
    store = _state_store(req)
    data = store.get("stats", {}) or {}
    runs = [r for r in reversed(store.get("runs", []) or []) if isinstance(r, dict)]
    return {
        "window_total": data.get("window_total", 0),
        "total_all_time": data.get("total_all_time", 0),
        "last_run": data.get("last_run", ""),
        "by_keyword": data.get("by_keyword", {}) or {},
        "by_source": data.get("by_source", {}) or {},
        "runs": runs[:RUNS_LIMIT],
    }


# --------------------------------------------------------------------------- #
# Public page — the shareable mentions report (/p/<token>/)
# --------------------------------------------------------------------------- #

REPORT_MENTION_LIMIT = 100  # newest mentions rendered on the public report


@page("report")
def report(req):
    """Shareable read-only mentions report.

    Rendered via a Django template ONLY (auto-escape): the feed is scraped web
    content, and a poisoned page title must never become markup. Core serves
    the result script-free (strict CSP), so the template uses no JS and only
    inline styles.
    """
    from django.template.loader import render_to_string

    store = DataStoreAPI(owner=OWNER, workspace=req.workspace).get(STORE_KEY)
    context = {"configured": store is not None, "mentions": [], "stats": {}}
    if store is not None:
        items = store.get("mentions", []) or []
        context["mentions"] = [
            m for m in reversed(items) if isinstance(m, dict)
        ][:REPORT_MENTION_LIMIT]
        context["stats"] = store.get("stats", {}) or {}
        config = store.get("config", {}) or {}
        context["keywords"] = config.get("keywords", [])
    return render_to_string("brand_tracker/public_report.html", context)
