"""
/p/<token>/ — plugin-rendered public pages behind capability URLs.

The /webhook/<token>/ idiom applied to HTML: no session, no auth header — the
unguessable token IS the capability. Core owns the whole envelope; the plugin's
@page handler only turns a PageRequest into an HTML string.

Hard rules (docs/PLAN_plugin_api.md, Stage 5):
- Per-IP rate limit runs FIRST, before the DB token lookup — this is an
  unauthenticated surface, where token scanning is the whole attack, and a
  guess must not cost a DB query once the budget is burned.
- Script-free by design: the strict CSP (`default-src 'none'`) means a
  poisoned scraped title can never become stored XSS with script execution —
  the class of bug is defanged, not policed. Templates-only rendering
  (auto-escape) is the author-guide MUST this backs up.
- noindex / no-store / no-referrer on success; unknown, revoked, and expired
  tokens are an identical plain 404 (no existence disclosure); no cookies.
- ``last_accessed_at`` writes are throttled (≥60s apart): an anonymous public
  hit must not be a guaranteed DB write.
"""

import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import PluginPublicPage
from core.plugins.api import PageRequest
from core.ratelimit import client_ip, rate_limit_exceeded, seconds_to_window_reset
from core.views.api.plugins import _declared_page, _plugin_loaded, _plugin_pages

logger = logging.getLogger(__name__)

RATE_WINDOW = 60  # seconds
LAST_ACCESS_WRITE_INTERVAL = 60  # seconds between last_accessed_at updates

# Script-free by design: no JS can ever execute, styles must be inline,
# images may come from anywhere (scraped content links its own thumbnails).
_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src * data:"


def _plain(status, text):
    return HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")


@csrf_exempt
def plugin_public_page_view(request: HttpRequest, token: str) -> HttpResponse:
    # 1. Per-IP brake BEFORE any DB work (every request counts — this surface
    #    has no auth to distinguish scanners from readers).
    ip = client_ip(request)
    limit = getattr(settings, "PUBLIC_PAGE_IP_RATE_LIMIT", 60)
    if rate_limit_exceeded(f"public_page_ip_{ip}", limit, RATE_WINDOW):
        response = _plain(429, "Too many requests")
        response["Retry-After"] = str(seconds_to_window_reset(RATE_WINDOW))
        return response

    if request.method != "GET":
        response = _plain(405, "Method not allowed")
        response["Allow"] = "GET"
        return response

    # 2. Resolve the capability. Unknown, revoked, and expired are the SAME
    #    plain 404 — a dead link discloses nothing.
    row = (
        PluginPublicPage.objects.select_related("workspace")
        .filter(token=token, is_active=True)
        .first()
    )
    if row is None or row.is_expired:
        return _plain(404, "Not found")

    slug = row.plugin_slug

    # 3. Plugin loaded this boot ∧ page still declared in the manifest.
    if not _plugin_loaded(slug) or _declared_page(slug, row.page) is None:
        return _plain(404, "Not found")

    # 4. Registered @page handler (same lazy import + per-plugin isolation as
    #    the API lane).
    pages = _plugin_pages(slug)
    if isinstance(pages, Exception):
        return _plain(503, "This page is temporarily unavailable")
    handler = pages.get(row.page)
    if handler is None:
        logger.error(
            "Public page: %s declares page %r but registers no handler",
            slug,
            row.page,
        )
        return _plain(500, "This page could not be rendered")

    # 5. Throttled access stamp (at most one write per interval).
    now = timezone.now()
    if (
        row.last_accessed_at is None
        or (now - row.last_accessed_at).total_seconds() >= LAST_ACCESS_WRITE_INTERVAL
    ):
        PluginPublicPage.objects.filter(id=row.id).update(last_accessed_at=now)

    # 6. Render. Workspace comes from the share row, never the caller.
    page_request = PageRequest(
        workspace=row.workspace,
        page=row.page,
        params={k: v[0] for k, v in request.GET.lists() if v},
    )
    try:
        html = handler(page_request)
    except Exception:
        logger.exception("Public page: handler %s.%s crashed", slug, row.page)
        return _plain(500, "This page could not be rendered")

    if not isinstance(html, str):
        logger.error(
            "Public page: handler %s.%s returned %s, expected str",
            slug,
            row.page,
            type(html).__name__,
        )
        return _plain(500, "This page could not be rendered")

    max_bytes = getattr(settings, "PLUGIN_API_MAX_RESPONSE_BYTES", 1024 * 1024)
    if len(html.encode("utf-8")) > max_bytes:
        logger.error(
            "Public page: %s.%s exceeds %s bytes", slug, row.page, max_bytes
        )
        return _plain(500, "This page could not be rendered")

    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = _CSP
    response["X-Robots-Tag"] = "noindex"
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
