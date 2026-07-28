"""
Internal object-storage API (loopback-only).

The only way an in-run script can read or write objects: a sandboxed run has no
DB / encryption key / S3 credentials, so ``pyrunner_storage`` POSTs here and
PyRunner does the S3 call server-side. boto3 and the bucket credentials never
enter a script environment.

Authenticated by the same signed per-run token the internal datastore and
channels APIs use (loopback-only). Both scoping axes are derived SERVER-SIDE
from the signed ``run_id``:

- the owning plugin (``Run -> script.owner_plugin``), which fixes the
  ``apps/<owner>/`` prefix the script is confined to. The script never names its
  own prefix, so it cannot reach another plugin's objects — by construction, not
  by validation.
- v1 scope: a run whose script has NO ``owner_plugin`` (a user-created script) is
  refused. Non-plugin script storage is deliberately deferred; see
  docs/PLAN_storage_seam.md.

POST /internal/storage/put     {"key": "...", "data": "<base64>", "content_type": "..."}
POST /internal/storage/get     {"key": "..."}   -> {"data": "<base64>"}
POST /internal/storage/delete  {"key": "..."}
POST /internal/storage/list    {"prefix": "..."}
POST /internal/storage/url     {"key": "...", "expires_in": 3600}
"""

import base64
import binascii
import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.ratelimit import rate_limit_exceeded
from core.views.api.decorators import internal_datastore_token_required

# No logger here on purpose: every failure this module reports already carries a
# server-side trace from S3Service, and the response bodies name the cause.

# Per-request payload ceiling. Every byte crosses an HTTP body and sits in the
# web process's memory, so this is a "don't wedge the web process" limit rather
# than a storage quota. Backups do NOT route through here — they upload in
# process — so this costs them nothing. Large objects are a later concern: see
# the deferred `presigned_put_url()` escape hatch in the plan.
MAX_PUT_BYTES = 25 * 1024 * 1024

# base64 inflates by 4/3; the rest is JSON envelope slack (key, content_type).
_MAX_PUT_BODY_BYTES = MAX_PUT_BYTES * 4 // 3 + 8192

# Read-only endpoints carry a key and little else.
_MAX_SMALL_BODY_BYTES = 64 * 1024

# Per-run write brake: a runaway script can't fill a bucket (or a bill). Mirrors
# `_RUN_SEND_LIMIT` in channels_internal — the threat model is accidents, not
# adversaries. Reads are not braked; they cost nothing to rerun.
_RUN_WRITE_LIMIT = 200
_RUN_WRITE_WINDOW = 60  # seconds


def _bad_request(message: str) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": "BAD_REQUEST", "message": message}}, status=400
    )


def _error(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _payload(request, max_bytes):
    """Parse the JSON body under an explicit byte bound.

    Reads the stream directly instead of touching ``request.body``: that property
    enforces Django's GLOBAL ``DATA_UPLOAD_MAX_MEMORY_SIZE`` (2.5 MB by default),
    which would reject a legitimate put with an opaque 400 long before this
    module's own 25 MB cap could answer 413. Raising the global setting instead
    would widen the limit for every endpoint on the instance, so the bound lives
    here, where it is actually needed.

    Reads at most ``max_bytes + 1`` so "at the cap" is distinguishable from "over
    it", and never buffers more than that regardless of Content-Length.

    Returns (data, error_response).
    """
    raw = request.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None, _error("TOO_LARGE", f"Request exceeds {max_bytes} bytes.", 413)

    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return None, _bad_request("Body must be valid JSON")
    if not isinstance(data, dict):
        return None, _bad_request("Body must be a JSON object")
    return data, None


def _storage_for(request):
    """Build the run's owner- and workspace-scoped StorageAPI, or an error response.

    BOTH axes come from the signed run_id, never from the request body: the owner
    from ``Run -> script.owner_plugin``, and the workspace from the decorator's
    server-side derivation (``request.datastore_workspace``, None ⇒ the default
    workspace). A script therefore cannot address another plugin's prefix or
    another workspace's.
    """
    from core.models import Run
    from core.plugins.api import StorageAPI

    run_id = (getattr(request, "datastore_run", None) or {}).get("run_id")

    owner = (
        Run.objects.filter(id=run_id)
        .values_list("script__owner_plugin", flat=True)
        .first()
    )
    if not owner:
        return None, _error(
            "STORAGE_PLUGIN_ONLY",
            "Storage is available to plugin-owned scripts only.",
            403,
        )

    storage = StorageAPI(owner, workspace=getattr(request, "datastore_workspace", None))
    # Covers both axes (connection attached, workspace resolvable), so an
    # unusable seam answers 503 here rather than a misleading 502 later.
    if not storage.is_available():
        return None, _error(
            "STORAGE_UNAVAILABLE",
            "No assets storage connection is configured on this instance.",
            503,
        )
    return storage, None


def _write_braked(request) -> bool:
    run_id = (getattr(request, "datastore_run", None) or {}).get("run_id")
    return rate_limit_exceeded(
        f"storage_write_rate_{run_id}", _RUN_WRITE_LIMIT, _RUN_WRITE_WINDOW
    )


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def put(request: HttpRequest) -> JsonResponse:
    from core.plugins.api import StorageError, StorageKeyError

    if _write_braked(request):
        return _error("RATE_LIMITED", "Per-run storage write rate exceeded.", 429)

    data, err = _payload(request, _MAX_PUT_BODY_BYTES)
    if err:
        return err

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return _bad_request("Missing 'key'")

    encoded = data.get("data")
    if not isinstance(encoded, str):
        return _bad_request("Missing 'data' (base64-encoded)")

    # Check the ENCODED length before decoding: allocating the decoded copy of an
    # over-cap payload is exactly what this limit exists to prevent.
    if len(encoded) > MAX_PUT_BYTES * 4 // 3 + 16:
        return _error(
            "TOO_LARGE", f"Object exceeds the {MAX_PUT_BYTES} byte limit.", 413
        )

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _bad_request("'data' must be valid base64")

    if len(raw) > MAX_PUT_BYTES:
        return _error(
            "TOO_LARGE", f"Object exceeds the {MAX_PUT_BYTES} byte limit.", 413
        )

    content_type = data.get("content_type") or "application/octet-stream"
    if not isinstance(content_type, str):
        return _bad_request("'content_type' must be a string")

    storage, err = _storage_for(request)
    if err:
        return err

    try:
        stored_key = storage.put(key, raw, content_type=content_type)
    except StorageKeyError as e:
        return _bad_request(str(e))
    except StorageError as e:
        return _error("STORAGE_FAILED", str(e), 502)

    return JsonResponse({"ok": True, "key": stored_key, "size": len(raw)})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def get(request: HttpRequest) -> JsonResponse:
    from core.plugins.api import StorageError, StorageKeyError

    data, err = _payload(request, _MAX_SMALL_BODY_BYTES)
    if err:
        return err

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return _bad_request("Missing 'key'")

    storage, err = _storage_for(request)
    if err:
        return err

    try:
        raw = storage.get(key)
    except StorageKeyError as e:
        return _bad_request(str(e))
    except StorageError as e:
        return _error("STORAGE_FAILED", str(e), 502)

    if raw is None:
        return _error("NOT_FOUND", f"No object at {key!r}", 404)

    return JsonResponse(
        {"ok": True, "key": key, "data": base64.b64encode(raw).decode("ascii")}
    )


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def delete(request: HttpRequest) -> JsonResponse:
    from core.plugins.api import StorageError, StorageKeyError

    if _write_braked(request):
        return _error("RATE_LIMITED", "Per-run storage write rate exceeded.", 429)

    data, err = _payload(request, _MAX_SMALL_BODY_BYTES)
    if err:
        return err

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return _bad_request("Missing 'key'")

    storage, err = _storage_for(request)
    if err:
        return err

    try:
        ok = storage.delete(key)
    except StorageKeyError as e:
        return _bad_request(str(e))
    except StorageError as e:
        return _error("STORAGE_FAILED", str(e), 502)

    return JsonResponse({"ok": ok, "key": key})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def list_objects(request: HttpRequest) -> JsonResponse:
    from core.plugins.api import StorageError, StorageKeyError

    data, err = _payload(request, _MAX_SMALL_BODY_BYTES)
    if err:
        return err

    prefix = data.get("prefix") or ""
    if not isinstance(prefix, str):
        return _bad_request("'prefix' must be a string")

    storage, err = _storage_for(request)
    if err:
        return err

    try:
        objects = storage.list(prefix)
    except StorageKeyError as e:
        return _bad_request(str(e))
    except StorageError as e:
        return _error("STORAGE_FAILED", str(e), 502)

    return JsonResponse(
        {
            "ok": True,
            "objects": [
                {
                    "key": o["key"],
                    "size": o["size"],
                    # isoformat so the helper hands scripts a stable string
                    # rather than a boto3 datetime that JSON can't carry.
                    "last_modified": (
                        o["last_modified"].isoformat()
                        if hasattr(o["last_modified"], "isoformat")
                        else str(o["last_modified"])
                    ),
                    "etag": o.get("etag", ""),
                }
                for o in objects
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def url(request: HttpRequest) -> JsonResponse:
    from core.plugins.api import StorageError, StorageKeyError

    data, err = _payload(request, _MAX_SMALL_BODY_BYTES)
    if err:
        return err

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return _bad_request("Missing 'key'")

    expires_in = data.get("expires_in", 3600)
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        return _bad_request("'expires_in' must be an integer")

    storage, err = _storage_for(request)
    if err:
        return err

    try:
        result = storage.url(key, expires_in=expires_in)
    except StorageKeyError as e:
        return _bad_request(str(e))
    except StorageError as e:
        return _error("STORAGE_FAILED", str(e), 502)

    if result is None:
        return _error("STORAGE_FAILED", f"Could not build a URL for {key!r}", 502)

    return JsonResponse({"ok": True, "key": key, "url": result})

