"""
API views for datastore access.
"""

import logging

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import DataStore, DataStoreEntry
from core.views.api.decorators import api_token_required, add_cors_headers

logger = logging.getLogger(__name__)

# Pagination settings
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def list_datastores(request: HttpRequest) -> JsonResponse:
    """
    List all datastores accessible to the token.

    GET /api/v1/datastores/

    Returns:
        {
            "datastores": [
                {
                    "name": "store_name",
                    "description": "...",
                    "entry_count": 42,
                    "created_at": "2026-01-15T10:30:00Z",
                    "updated_at": "2026-02-10T14:22:00Z"
                }
            ],
            "count": 1
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    token = request.api_token

    if token.datastore:
        # Token restricted to single datastore
        datastores = [token.datastore]
    else:
        # Global token - list all datastores
        datastores = DataStore.objects.all()

    data = {
        "datastores": [
            {
                "name": ds.name,
                "description": ds.description,
                "entry_count": ds.entry_count,
                "created_at": ds.created_at.isoformat(),
                "updated_at": ds.updated_at.isoformat(),
            }
            for ds in datastores
        ],
        "count": len(datastores) if token.datastore else datastores.count(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def get_datastore(request: HttpRequest, name: str) -> JsonResponse:
    """
    Get datastore metadata.

    GET /api/v1/datastores/<name>/

    Returns:
        {
            "name": "store_name",
            "description": "...",
            "entry_count": 42,
            "created_at": "2026-01-15T10:30:00Z",
            "updated_at": "2026-02-10T14:22:00Z"
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore  # Error response

    data = {
        "name": datastore.name,
        "description": datastore.description,
        "entry_count": datastore.entry_count,
        "created_at": datastore.created_at.isoformat(),
        "updated_at": datastore.updated_at.isoformat(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def list_entries(request: HttpRequest, name: str) -> JsonResponse:
    """
    List entries in a datastore with pagination.

    GET /api/v1/datastores/<name>/entries/
    Query params:
        - page: Page number (default: 1)
        - page_size: Items per page (default: 50, max: 100)

    Returns:
        {
            "entries": [
                {
                    "key": "config",
                    "value": {"retries": 3},
                    "created_at": "2026-01-20T08:00:00Z",
                    "updated_at": "2026-02-01T12:00:00Z"
                }
            ],
            "count": 42,
            "page": 1,
            "page_size": 50,
            "total_pages": 1
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore  # Error response

    # Parse pagination params
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    try:
        page_size = min(MAX_PAGE_SIZE, max(1, int(request.GET.get("page_size", DEFAULT_PAGE_SIZE))))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    # Get total count and calculate pagination
    total_count = datastore.entries.count()
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    # Ensure page is within bounds
    page = min(page, total_pages)

    # Get paginated entries
    offset = (page - 1) * page_size
    entries = datastore.entries.all()[offset:offset + page_size]

    data = {
        "entries": [
            {
                "key": entry.key,
                "value": entry.get_value(),
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in entries
        ],
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }

    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def get_entry(request: HttpRequest, name: str, key: str) -> JsonResponse:
    """
    Get a single entry by key.

    GET /api/v1/datastores/<name>/entries/<key>/

    Returns:
        {
            "key": "config",
            "value": {"retries": 3},
            "created_at": "2026-01-20T08:00:00Z",
            "updated_at": "2026-02-01T12:00:00Z"
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore  # Error response

    try:
        entry = DataStoreEntry.objects.get(datastore=datastore, key=key)
    except DataStoreEntry.DoesNotExist:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Entry '{key}' not found"}},
            status=404,
        )
        return add_cors_headers(response)

    data = {
        "key": entry.key,
        "value": entry.get_value(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response)


def _get_authorized_datastore(request: HttpRequest, name: str):
    """
    Get datastore if token has access.

    Returns the DataStore object or a JsonResponse error.
    """
    token = request.api_token

    try:
        datastore = DataStore.objects.get(name=name)
    except DataStore.DoesNotExist:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Datastore '{name}' not found"}},
            status=404,
        )
        return add_cors_headers(response)

    # Check token access
    if token.datastore and token.datastore != datastore:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have access to this datastore"}},
            status=403,
        )
        return add_cors_headers(response)

    return datastore
