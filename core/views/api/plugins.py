"""
Plugin API dispatcher — /api/v1/plugins/<slug>/<resource>/[<item_id>/].

The ONE core-owned route for every plugin's external HTTP resources
(docs/PLAN_plugin_api.md). Core owns 100% of auth, rate limiting, and
workspace scoping; a plugin contributes only a declared manifest entry
(plugin.json "provides.api_resources") plus a marked handler in its api.py —
it never sees a raw request, a token, or a tenancy decision, and it has zero
URL surface of its own.

Dispatch order (each failure a distinct JSON error, datastore-API envelope;
CORS headers on EVERY response including errors):

1. OPTIONS → CORS preflight answered pre-auth (browsers never send auth
   headers on preflight).
2. Token auth (Bearer / X-API-Key). A failed lookup counts against the
   per-IP ``plugin_api_authfail_{ip}`` limiter, and an over-budget IP is
   rejected BEFORE the DB lookup — token scanning must not cost a query
   per guess (house precedent: webhook_trigger_view / channel_webhook_view).
3. Scope: token is plugin-scoped, for THIS slug, and its workspace exists.
   NULL workspace (workspace deleted) is FAIL-CLOSED → 403 — never the
   default-workspace fallback the legacy datastore tokens keep.
4. Plugin loaded this boot (settings.INSTALLED_PLUGINS) → else 404.
5. Rate limits: per-token, then per-plugin → 429 with Retry-After.
6. Resource declared in the manifest — read from DISK (dev-mode plugins have
   no Plugin DB row) and cached per process — and method ∈ declared methods.
7. Handler registered in <plugin>/api.py (lazy import, cached; broken import
   → 503, declared-but-unregistered → 500).
8. Build the frozen APIRequest (workspace derived server-side from the
   token), call the handler, enforce the response-size cap, respond.
   APIError → its 4xx (status clamped 400–499); anything else → generic 500,
   traceback logged, internals never leak.
"""

import importlib
import inspect
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import APIToken
from core.plugins import is_valid_plugin_slug
from core.plugins.api import API_PAGE_ATTR, API_RESOURCE_ATTR, APIError, APIRequest
from core.ratelimit import (
    client_ip,
    rate_limit_exceeded,
    rate_limit_over,
    seconds_to_window_reset,
)
from core.views.api.decorators import (
    _extract_token,
    add_cors_headers,
    cors_preflight_response,
)

logger = logging.getLogger(__name__)

RATE_WINDOW = 60  # seconds — all plugin-API limits are per-minute fixed windows

# Per-process caches. Both are keyed by slug and live for the process lifetime:
# plugins only change across a restart (install/activate restarts; dev mode
# runs under runserver's autoreloader, which restarts the process on edit).
_manifest_cache: dict = {}
_handlers_cache: dict = {}


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #

def _error(status, code, message, *, retry_after=None):
    """Uniform error envelope + CORS on every error (the datastore API's
    CORS-only-on-success is exactly what broke its browser story)."""
    response = JsonResponse({"error": {"code": code, "message": message}}, status=status)
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return add_cors_headers(response)


def _preflight():
    """Answer a CORS preflight pre-auth (a browser preflight carries no token)."""
    return cors_preflight_response()


def _rate_limited(message):
    return _error(
        429, "RATE_LIMITED", message, retry_after=seconds_to_window_reset(RATE_WINDOW)
    )


# --------------------------------------------------------------------------- #
# Manifest + handler resolution (disk is the truth for the code that's loaded)
# --------------------------------------------------------------------------- #

def _plugin_loaded(slug):
    return f"plugins.{slug}" in getattr(settings, "INSTALLED_PLUGINS", [])


def _plugin_manifest(slug):
    """Parse the loaded plugin's plugin.json from DISK, cached per process.

    Deliberately not the Plugin DB row: dev-mode plugins have none. The package
    directory of the imported ``plugins.<slug>`` module is identical for
    installed and dev plugins. Returns {} when the manifest is missing/broken
    (every declared-resource check then fails closed to 404).
    """
    if slug in _manifest_cache:
        return _manifest_cache[slug]
    manifest = {}
    try:
        module = importlib.import_module(f"plugins.{slug}")
        manifest_path = Path(module.__file__).parent / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
    except Exception:
        logger.exception("Plugin API: cannot read manifest for %r", slug)
        manifest = {}
    _manifest_cache[slug] = manifest
    return manifest


def _declared_resources(slug):
    """The manifest's api_resources list — entries that aren't dicts are ignored."""
    provides = _plugin_manifest(slug).get("provides") or {}
    resources = provides.get("api_resources") or []
    return [r for r in resources if isinstance(r, dict) and r.get("name")]


def _declared_resource(slug, resource_name):
    for entry in _declared_resources(slug):
        if entry.get("name") == resource_name:
            return entry
    return None


def _declared_public_pages(slug):
    """The manifest's public_pages list — entries that aren't dicts are ignored."""
    provides = _plugin_manifest(slug).get("provides") or {}
    pages = provides.get("public_pages") or []
    return [p for p in pages if isinstance(p, dict) and p.get("name")]


def _declared_page(slug, page_name):
    """The manifest's public_pages entry for ``page_name``, or None."""
    for entry in _declared_public_pages(slug):
        if entry.get("name") == page_name:
            return entry
    return None


def _load_handlers(slug):
    """Lazy-import ``plugins.<slug>.api`` and scan for marked handlers.

    Cached per process, INCLUDING failures — a broken api.py is a guarded,
    per-plugin 503, never a core outage, and stays quarantined until the
    plugin is fixed and the process restarts (same philosophy as the guarded
    UI mounts in pyrunner/urls.py). Returns
    ``{"resources": {name: func}, "pages": {name: func}}``, or an Exception
    instance when the import failed.
    """
    if slug in _handlers_cache:
        return _handlers_cache[slug]
    try:
        module = importlib.import_module(f"plugins.{slug}.api")
        result = {"resources": {}, "pages": {}}
        for _, func in inspect.getmembers(module, inspect.isfunction):
            resource_name = getattr(func, API_RESOURCE_ATTR, None)
            if resource_name:
                result["resources"][resource_name] = func
            page_name = getattr(func, API_PAGE_ATTR, None)
            if page_name:
                result["pages"][page_name] = func
    except ModuleNotFoundError as exc:
        if exc.name == f"plugins.{slug}.api":
            # No api.py at all — treat as "nothing registered" (declared
            # resources then 500 as unregistered; the doctor catches this
            # pre-ship). A missing DEPENDENCY of api.py is a real failure.
            result = {"resources": {}, "pages": {}}
        else:
            logger.exception("Plugin API: import of plugins.%s.api failed", slug)
            result = exc
    except Exception as exc:
        logger.exception("Plugin API: import of plugins.%s.api failed", slug)
        result = exc
    _handlers_cache[slug] = result
    return result


def _plugin_handlers(slug):
    """Resource-name → handler map (or the import Exception)."""
    loaded = _load_handlers(slug)
    return loaded if isinstance(loaded, Exception) else loaded["resources"]


def _plugin_pages(slug):
    """Page-name → handler map (or the import Exception)."""
    loaded = _load_handlers(slug)
    return loaded if isinstance(loaded, Exception) else loaded["pages"]


# --------------------------------------------------------------------------- #
# Auth + limits (shared by discovery and dispatch)
# --------------------------------------------------------------------------- #

def _authfail_limit():
    return getattr(settings, "PLUGIN_API_AUTHFAIL_RATE_LIMIT", 20)


def _authenticate(request):
    """Resolve the request's plugin-API token, or an error response.

    Returns ``(token, None)`` on success, ``(None, response)`` on failure.
    Only auth happens here — scope belongs to the caller (discovery accepts
    any plugin-scoped token; dispatch additionally pins the slug).
    """
    ip = client_ip(request)
    authfail_key = f"plugin_api_authfail_{ip}"

    # An IP over its auth-fail budget is cut off BEFORE the DB lookup (peek —
    # successful callers never consume this budget).
    if rate_limit_over(authfail_key, _authfail_limit(), RATE_WINDOW):
        return None, _rate_limited("Too many failed authentication attempts.")

    def _authfail(message):
        if rate_limit_exceeded(authfail_key, _authfail_limit(), RATE_WINDOW):
            return _rate_limited("Too many failed authentication attempts.")
        return _error(401, "UNAUTHORIZED", message)

    raw = _extract_token(request)
    if not raw:
        return None, _authfail("API token required")

    try:
        token = APIToken.objects.select_related("workspace").get(
            token=raw, is_active=True
        )
    except APIToken.DoesNotExist:
        logger.warning("Plugin API request with invalid token: %s...", raw[:8])
        return None, _authfail("Invalid API token")

    if token.expires_at and token.expires_at < timezone.now():
        logger.info("Plugin API request with expired token: %s", token.name)
        return None, _authfail("API token has expired")

    if token.scope != APIToken.Scope.PLUGIN:
        # Legacy datastore tokens must never gain plugin API access — that
        # would be silent scope expansion. (Doesn't count as an auth failure:
        # the credential is real, just not for this surface.)
        return None, _error(
            403, "SCOPE_MISMATCH", "This token is not scoped to a plugin API"
        )

    if token.workspace_id is None:
        # Fail-closed: the token's workspace was deleted. Falling back to the
        # default workspace would silently redirect external consumers to
        # another tenant's plugin data.
        return None, _error(
            403, "SCOPE_MISMATCH", "This token's workspace no longer exists"
        )

    return token, None


def _enforce_token_limit(token):
    """Per-token budget — same setting/key family as the datastore API."""
    limit = getattr(settings, "API_RATE_LIMIT", 60)
    if rate_limit_exceeded(f"api_rate_{token.id}", limit, RATE_WINDOW):
        logger.warning("Plugin API rate limit exceeded for token: %s", token.name)
        return _rate_limited("Rate limit exceeded. Try again later.")
    return None


def _enforce_plugin_limit(slug):
    """Per-plugin ceiling — slowness and load are per-plugin, not per-caller."""
    limit = getattr(settings, "PLUGIN_API_PLUGIN_RATE_LIMIT", 300)
    if rate_limit_exceeded(f"plugin_api_plugin_{slug}", limit, RATE_WINDOW):
        logger.warning("Plugin API per-plugin rate limit exceeded: %s", slug)
        return _rate_limited("Plugin rate limit exceeded. Try again later.")
    return None


def _touch_last_used(token):
    APIToken.objects.filter(id=token.id).update(last_used_at=timezone.now())


def _resource_descriptor(entry):
    """The public shape of a declared resource (discovery responses)."""
    return {
        "name": entry.get("name"),
        "summary": entry.get("summary", ""),
        "methods": entry.get("methods") or ["GET"],
    }


def _plugin_descriptor(slug):
    manifest = _plugin_manifest(slug)
    return {
        "slug": slug,
        "name": manifest.get("name", slug),
        "version": manifest.get("version", ""),
        "resources": [_resource_descriptor(e) for e in _declared_resources(slug)],
        # Declared shareable pages (metadata only — the actual URLs are the
        # per-share /p/<token>/ capabilities, never enumerated here).
        "public_pages": [
            {"name": e.get("name"), "summary": e.get("summary", "")}
            for e in _declared_public_pages(slug)
        ],
    }


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #

@csrf_exempt
def plugin_api_discovery(request: HttpRequest) -> HttpResponse:
    """GET /api/v1/plugins/ — the loaded plugins this token can reach.

    Plugin-scoped → exactly one (or an empty 200 list when its plugin isn't
    currently loaded — a token may outlive its plugin). The curl-first
    integration experience: slug, name, version, declared resources.
    """
    if request.method == "OPTIONS":
        return _preflight()
    if request.method != "GET":
        return _error(405, "METHOD_NOT_ALLOWED", "Only GET is supported")

    token, failure = _authenticate(request)
    if failure is not None:
        return failure
    limited = _enforce_token_limit(token)
    if limited is not None:
        return limited
    _touch_last_used(token)

    plugins = []
    if _plugin_loaded(token.plugin_slug):
        plugins.append(_plugin_descriptor(token.plugin_slug))
    return add_cors_headers(JsonResponse({"plugins": plugins, "count": len(plugins)}))


@csrf_exempt
def plugin_api_index(request: HttpRequest, slug: str) -> HttpResponse:
    """GET /api/v1/plugins/<slug>/ — the plugin's declared resource list."""
    if request.method == "OPTIONS":
        return _preflight()
    if request.method != "GET":
        return _error(405, "METHOD_NOT_ALLOWED", "Only GET is supported")

    token, failure = _authenticate(request)
    if failure is not None:
        return failure
    if token.plugin_slug != slug:
        return _error(403, "SCOPE_MISMATCH", "This token is not scoped to this plugin")
    if not is_valid_plugin_slug(slug) or not _plugin_loaded(slug):
        return _error(404, "NOT_FOUND", f"Plugin '{slug}' not found")
    limited = _enforce_token_limit(token) or _enforce_plugin_limit(slug)
    if limited is not None:
        return limited
    _touch_last_used(token)

    return add_cors_headers(JsonResponse(_plugin_descriptor(slug)))


@csrf_exempt
def plugin_api_dispatch(
    request: HttpRequest, slug: str, resource: str, item_id: str | None = None
) -> HttpResponse:
    """GET /api/v1/plugins/<slug>/<resource>/[<item_id>/] — call the handler."""
    if request.method == "OPTIONS":
        return _preflight()

    # 2. Auth (+ pre-auth per-IP auth-fail throttle inside).
    token, failure = _authenticate(request)
    if failure is not None:
        return failure

    # 3. Scope pins the slug (workspace/scope already verified in _authenticate).
    if token.plugin_slug != slug:
        return _error(403, "SCOPE_MISMATCH", "This token is not scoped to this plugin")

    # 4. Plugin loaded this boot. Deactivated/removed/never-existed are all 404.
    if not is_valid_plugin_slug(slug) or not _plugin_loaded(slug):
        return _error(404, "NOT_FOUND", f"Plugin '{slug}' not found")

    # 5. Rate limits (per-token, then per-plugin), Retry-After on both.
    limited = _enforce_token_limit(token) or _enforce_plugin_limit(slug)
    if limited is not None:
        return limited
    _touch_last_used(token)

    # 6. Declared in the manifest ∧ method allowed. Declared methods are the
    #    manifest's (doctor pins them to ["GET"] in v1); the shape is already
    #    POST-ready without a URL or envelope change.
    entry = _declared_resource(slug, resource)
    if entry is None:
        return _error(404, "NOT_FOUND", f"Resource '{resource}' not found")
    allowed = entry.get("methods") or ["GET"]
    if request.method not in allowed:
        return _error(
            405, "METHOD_NOT_ALLOWED", f"Method {request.method} is not allowed"
        )

    # 7. Registered handler.
    handlers = _plugin_handlers(slug)
    if isinstance(handlers, Exception):
        return _error(
            503, "PLUGIN_API_UNAVAILABLE", "This plugin's API failed to load"
        )
    handler = handlers.get(resource)
    if handler is None:
        # Declared but unregistered — a plugin bug the doctor catches pre-ship.
        logger.error(
            "Plugin API: %s declares resource %r but registers no handler",
            slug,
            resource,
        )
        return _error(500, "PLUGIN_ERROR", "The plugin failed to handle this request")

    # 8. Frozen context in (workspace derived server-side from the token,
    #    never from the caller), plain dict out.
    api_request = APIRequest(
        workspace=token.workspace,
        resource=resource,
        item_id=item_id,
        method=request.method,
        params={k: v[0] for k, v in request.GET.lists() if v},
        params_list={k: list(v) for k, v in request.GET.lists()},
    )
    try:
        result = handler(api_request)
    except APIError as exc:
        if not (400 <= exc.status <= 499):
            # A plugin must not fabricate redirects or fake 5xx.
            logger.error(
                "Plugin API: %s.%s raised APIError with out-of-range status %s",
                slug,
                resource,
                exc.status,
            )
            return _error(
                500, "PLUGIN_ERROR", "The plugin failed to handle this request"
            )
        return _error(exc.status, exc.code, exc.message)
    except Exception:
        logger.exception("Plugin API: handler %s.%s crashed", slug, resource)
        return _error(500, "PLUGIN_ERROR", "The plugin failed to handle this request")

    if not isinstance(result, dict):
        logger.error(
            "Plugin API: handler %s.%s returned %s, expected dict",
            slug,
            resource,
            type(result).__name__,
        )
        return _error(500, "PLUGIN_ERROR", "The plugin failed to handle this request")

    try:
        body = json.dumps(result, cls=DjangoJSONEncoder)
    except (TypeError, ValueError):
        logger.exception(
            "Plugin API: handler %s.%s returned a non-JSON-serializable dict",
            slug,
            resource,
        )
        return _error(500, "PLUGIN_ERROR", "The plugin failed to handle this request")

    max_bytes = getattr(settings, "PLUGIN_API_MAX_RESPONSE_BYTES", 1024 * 1024)
    if len(body.encode("utf-8")) > max_bytes:
        logger.error(
            "Plugin API: %s.%s response exceeds %s bytes — the handler must paginate",
            slug,
            resource,
            max_bytes,
        )
        return _error(
            500,
            "RESPONSE_TOO_LARGE",
            "The plugin response is too large; the handler must paginate",
        )

    return add_cors_headers(HttpResponse(body, content_type="application/json"))
