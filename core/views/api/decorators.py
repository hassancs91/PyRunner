"""
API authentication decorators.
"""

import logging
from functools import wraps

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from core.models import APIToken
from core.ratelimit import rate_limit_exceeded

logger = logging.getLogger(__name__)

# Rate limiting settings
API_RATE_LIMIT = getattr(settings, "API_RATE_LIMIT", 60)  # requests per minute
API_RATE_WINDOW = 60  # seconds


def api_token_required(view_func):
    """
    Decorator to require API token authentication.

    Validates the token, checks expiration, enforces rate limiting,
    and attaches the token to the request for view access.

    Token can be provided via:
    - Authorization: Bearer <token>
    - X-API-Key: <token>

    Answers OPTIONS pre-auth and puts CORS headers on its error responses:
    browsers never send auth headers on a preflight, so gating OPTIONS behind
    the token check (the original decorator-order bug) made every cross-origin
    browser call to the datastore API impossible.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # CORS preflight — must succeed tokenless, before any auth.
        if request.method == "OPTIONS":
            return cors_preflight_response()

        # Extract token from headers
        token = _extract_token(request)
        if not token:
            return add_cors_headers(JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "API token required"}},
                status=401,
            ))

        # Validate token
        try:
            api_token = APIToken.objects.select_related("datastore").get(
                token=token,
                is_active=True,
            )
        except APIToken.DoesNotExist:
            logger.warning(f"API request with invalid token: {token[:8]}...")
            return add_cors_headers(JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Invalid API token"}},
                status=401,
            ))

        # Check expiration
        if api_token.expires_at and api_token.expires_at < timezone.now():
            logger.info(f"API request with expired token: {api_token.name}")
            return add_cors_headers(JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "API token has expired"}},
                status=401,
            ))

        # Plugin-scoped tokens are for /api/v1/plugins/… only — least privilege,
        # never datastore reach. (Such tokens didn't exist before the scope
        # field, so nothing previously working changes here.)
        if api_token.scope == APIToken.Scope.PLUGIN:
            logger.info(f"Plugin-scoped token rejected on datastore API: {api_token.name}")
            return add_cors_headers(JsonResponse(
                {
                    "error": {
                        "code": "SCOPE_MISMATCH",
                        "message": "This token is scoped to a plugin API, not datastores",
                    }
                },
                status=403,
            ))

        # Rate limiting by token (fixed window, shared helper)
        if rate_limit_exceeded(
            f"api_rate_{api_token.id}", API_RATE_LIMIT, API_RATE_WINDOW
        ):
            logger.warning(f"API rate limit exceeded for token: {api_token.name}")
            return add_cors_headers(JsonResponse(
                {"error": {"code": "RATE_LIMITED", "message": "Rate limit exceeded. Try again later."}},
                status=429,
            ))

        # Update last used timestamp (async-safe, won't block)
        APIToken.objects.filter(id=api_token.id).update(
            last_used_at=timezone.now()
        )

        # Attach token to request for view access
        request.api_token = api_token

        return view_func(request, *args, **kwargs)

    return wrapper


def _extract_token(request):
    """
    Extract API token from request headers.

    Supports:
    - Authorization: Bearer <token>
    - X-API-Key: <token>
    """
    # Try Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    # Try X-API-Key: <token>
    return request.headers.get("X-API-Key")


# Loopback addresses the internal datastore endpoint will accept. The endpoint
# is reached only over loopback (the worker calling the co-located web process),
# never from off-host.
_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1"})


def internal_datastore_token_required(view_func):
    """
    Auth gate for the INTERNAL datastore endpoint (Seam 1).

    Distinct from ``api_token_required`` on purpose:
    - It authenticates a stateless, signed per-run token (HMAC over SECRET_KEY,
      see ``core.services.datastore_token``), not a DB-backed ``APIToken``.
    - It is **loopback-only** (worker -> co-located web process).
    - It is **NOT rate-limited**: scripts do unbounded local datastore I/O today
      (raw SQLite), so a 60/min cap would be a behavior regression.

    On success, attaches ``request.datastore_run`` (the decoded token payload).
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Lazy import keeps this module importable during the settings-time
        # light-import pre-check (mirrors the rest of the codebase's discipline).
        from core.services.datastore_token import verify_datastore_token

        remote_addr = request.META.get("REMOTE_ADDR", "")
        if remote_addr not in _LOOPBACK_ADDRS:
            logger.warning("Internal datastore request from non-loopback %s", remote_addr)
            return JsonResponse(
                {"error": {"code": "FORBIDDEN", "message": "Internal endpoint is loopback-only"}},
                status=403,
            )

        token = _extract_token(request)
        payload = verify_datastore_token(token)
        if not payload:
            return JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Invalid or expired datastore token"}},
                status=401,
            )

        request.datastore_run = payload

        # Derive the run's workspace SERVER-SIDE (tenancy Stage 2), so by-name
        # datastore resolution is scoped to it. The token carries only a signed
        # run_id, so the workspace cannot be forged by the running script —
        # unlike the SQLite helper's env var. None ⇒ the resolver falls back to
        # the default workspace.
        ws_id = None
        try:
            from core.models import Run

            ws_id = (
                Run.objects.filter(id=payload.get("run_id"))
                .values_list("workspace_id", flat=True)
                .first()
            )
        except Exception:
            ws_id = None
        request.datastore_workspace = ws_id

        return view_func(request, *args, **kwargs)

    return wrapper


def cors_preflight_response():
    """The one CORS preflight answer for every external API surface (204 + CORS).

    A preflight carries no auth header, so it must be produced BEFORE any token
    check — both the datastore decorator and the plugin dispatcher use this.
    """
    return add_cors_headers(HttpResponse(status=204))


def add_cors_headers(response):
    """
    Add CORS headers to API response.

    For self-hosted deployments, we allow all origins by default.
    This can be configured via API_CORS_ORIGINS setting.
    """
    cors_origins = getattr(settings, "API_CORS_ORIGINS", "*")

    response["Access-Control-Allow-Origin"] = cors_origins
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Authorization, X-API-Key, Content-Type"
    response["Access-Control-Max-Age"] = "86400"

    return response
