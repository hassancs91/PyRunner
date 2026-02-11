"""
API authentication decorators.
"""

import logging
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone

from core.models import DataStoreAPIToken

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
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Extract token from headers
        token = _extract_token(request)
        if not token:
            return JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "API token required"}},
                status=401,
            )

        # Validate token
        try:
            api_token = DataStoreAPIToken.objects.select_related("datastore").get(
                token=token,
                is_active=True,
            )
        except DataStoreAPIToken.DoesNotExist:
            logger.warning(f"API request with invalid token: {token[:8]}...")
            return JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Invalid API token"}},
                status=401,
            )

        # Check expiration
        if api_token.expires_at and api_token.expires_at < timezone.now():
            logger.info(f"API request with expired token: {api_token.name}")
            return JsonResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "API token has expired"}},
                status=401,
            )

        # Rate limiting by token
        rate_key = f"api_rate_{api_token.id}"
        requests_count = cache.get(rate_key, 0)

        if requests_count >= API_RATE_LIMIT:
            logger.warning(f"API rate limit exceeded for token: {api_token.name}")
            return JsonResponse(
                {"error": {"code": "RATE_LIMITED", "message": "Rate limit exceeded. Try again later."}},
                status=429,
            )

        cache.set(rate_key, requests_count + 1, API_RATE_WINDOW)

        # Update last used timestamp (async-safe, won't block)
        DataStoreAPIToken.objects.filter(id=api_token.id).update(
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
