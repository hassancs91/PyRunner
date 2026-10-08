"""
Internal vector-collections API (loopback-only) — the door ``pyrunner_vectors`` uses.

A run has no database access, no encryption key and no embedding or Qdrant
credentials, so the helper POSTs here and PyRun does the work server-side,
through the same ``VectorService`` the console and the plugin SDK use.

Authenticated by the signed per-run token (``internal_datastore_token_required``);
everything that scopes the call is derived SERVER-SIDE from the run:

- the **workspace** — the collection is resolved by name inside the run's
  workspace, so a script cannot reach another tenant's collection;
- the **access rule** — a plugin-owned collection is reachable from that
  plugin's scripts only; an unowned collection from any script in the
  workspace. A collection the run may not use answers exactly like a missing
  one (404), so its existence does not leak.

Per-run brakes (accidents, not adversaries) live HERE, not in the helper — the
helper is code a script can replace: ``RUN_EMBED_CALLS`` provider calls,
``RUN_TEXT_BYTES`` of text sent to the embedding provider and ``RUN_POINTS``
points written or deleted per run. Reads are not braked.

POST /internal/vectors/<name>/<op>, ``op`` one of:

    info          {}                                         -> {"collection": {...}}
    add_texts     {"items": [{"id", "text", "payload"?}]}    -> {"added", "tokens", "truncated"}
    add_vectors   {"items": [{"id", "vector", "payload"?, "text"?}]} -> {"added"}
    search        {"query", "k"?, "filter"?}                 -> {"results": [...]}
    search_vector {"vector", "k"?, "filter"?}                -> {"results": [...]}
    get           {"ids", "include_vectors"?}                -> {"points": [...]}
    delete        {"ids"} | {"filter"}                       -> {"deleted"}
    count         {"filter"?}                                -> {"count"}
    scroll        {"cursor"?, "limit"?, "filter"?, "include_vectors"?} -> {"points", "next_cursor"}
    embed         {"texts", "input_type"?}                   -> {"vectors": [...]}
    credentials   {}                                         -> {"credentials": {...}}

Errors: ``{"error": {"code", "message"}}`` with the ``VectorError`` code, mapped
by ``STATUS_FOR_CODE`` (``not_found`` 404, ``invalid`` / ``unsupported_filter``
400, ``collection_full`` 413, ``embedding_failed`` 502, …).
"""

import json
import logging
import math

from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.views.api.decorators import internal_datastore_token_required

logger = logging.getLogger(__name__)

# One request carries at most this much JSON (a batch of 1 000 vectors of 1 536
# floats is ~15 MB). The helper batches well below it.
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_ITEMS = 1000

RUN_EMBED_CALLS = 2_000
RUN_TEXT_BYTES = 20 * 1024 * 1024
RUN_POINTS = 200_000
# The counters outlive the 24 h run token, so a run can never reset its budget.
_BUDGET_TTL = 25 * 60 * 60

STATUS_FOR_CODE = {
    "not_found": 404,
    "invalid": 400,
    "unsupported_filter": 400,
    "dimension_mismatch": 400,
    "unsupported": 400,
    "credentials_not_allowed": 403,
    "model_mismatch": 409,
    "not_ready": 409,
    "collection_full": 413,
    "run_budget_exceeded": 429,
    "embedding_failed": 502,
    "backend_unavailable": 503,
}


def _error(code: str, message: str) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "message": message}}, status=STATUS_FOR_CODE.get(code, 500)
    )


def _payload(request):
    """The JSON body under an explicit bound (see storage_internal._payload for why
    the stream is read directly instead of ``request.body``)."""
    raw = request.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return None, JsonResponse(
            {"error": {"code": "too_large", "message": f"Request exceeds {MAX_BODY_BYTES} bytes."}},
            status=413,
        )
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return None, _error("invalid", "Body must be valid JSON.")
    if not isinstance(data, dict):
        return None, _error("invalid", "Body must be a JSON object.")
    return data, None


def _resolve(request, name):
    """The collection this run may use, or ``(None, error_response)``."""
    from core.models import Run, VectorCollection

    run = (
        Run.objects.filter(id=request.datastore_run.get("run_id"))
        .values("script_id", "script__owner_plugin")
        .first()
    )
    missing = _error(
        "not_found",
        f"Collection '{name}' does not exist or is not available to this script.",
    )
    if not run or run["script_id"] is None:
        return None, missing
    try:
        collection = VectorCollection.resolve_for_workspace(name, request.datastore_workspace)
    except VectorCollection.DoesNotExist:
        return None, missing
    # Plugin-owned ⇒ that plugin's scripts only; unowned ⇒ any script in the
    # workspace. Answered like "missing" so ownership can't be probed.
    if collection.owner_plugin and collection.owner_plugin != run["script__owner_plugin"]:
        return None, missing
    if not collection.is_ready:
        return None, _error(
            "not_ready",
            f"Collection '{name}' is in an error state ({collection.last_error or 'unknown'}); "
            "use Retry on its page in PyRun.",
        )
    return collection, None


# --------------------------------------------------------------------------- brakes


def _budget_key(request, kind):
    return f"vectors_run_budget:{request.datastore_run.get('run_id')}:{kind}"


def _over_budget(request, **costs):
    """The first brake ``costs`` would exceed, as an error response, else None.

    ``costs`` maps kind → amount for this request (``embed_calls``, ``text_bytes``,
    ``points``). Checked before the work, charged after it succeeded
    (``_charge``); two concurrent requests may both pass — fine for a brake.
    Fail-open on a cache error, like ``core.ratelimit``.
    """
    limits = {"embed_calls": RUN_EMBED_CALLS, "text_bytes": RUN_TEXT_BYTES, "points": RUN_POINTS}
    labels = {
        "embed_calls": "embedding calls",
        "text_bytes": "bytes of embedded text",
        "points": "points written or deleted",
    }
    for kind, amount in costs.items():
        try:
            used = cache.get(_budget_key(request, kind)) or 0
        except Exception:
            logger.warning("vector run budget check failed (cache)", exc_info=True)
            return None
        if amount and used + amount > limits[kind]:
            return _error(
                "run_budget_exceeded",
                f"This run would exceed its brake of {limits[kind]:,} {labels[kind]} "
                f"({used:,} used, this call needs {amount:,}). The brake guards against "
                "runaway loops; split the work across runs.",
            )
    return None


def _charge(request, **costs):
    for kind, amount in costs.items():
        if not amount:
            continue
        key = _budget_key(request, kind)
        try:
            cache.add(key, 0, _BUDGET_TTL)
            try:
                cache.incr(key, amount)
            except ValueError:  # culled between add() and incr()
                cache.set(key, amount, _BUDGET_TTL)
        except Exception:
            logger.warning("vector run budget charge failed (cache)", exc_info=True)


def _embed_calls(collection, n_texts):
    """Provider calls ``n_texts`` will cost — EmbeddingClient batches by ``max_batch``."""
    provider = collection.embedding_provider
    if provider is None or n_texts <= 0:
        return 0
    return math.ceil(n_texts / max(1, int(provider.max_batch or 1)))


def _text_bytes(texts):
    return sum(len(t.encode("utf-8")) for t in texts if isinstance(t, str))


# --------------------------------------------------------------------------- ops


def _list_param(data, key, *, required=True):
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, list):
        raise _BadRequest(f"'{key}' must be a list")
    if len(value) > MAX_ITEMS:
        raise _BadRequest(f"'{key}' takes at most {MAX_ITEMS} entries per call")
    return value


def _int_param(data, key, default):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequest(f"'{key}' must be a whole number")
    return value


class _BadRequest(Exception):
    pass


def _op_info(request, collection, handle, data):
    return {
        "collection": {
            "name": collection.name,
            "dimension": collection.dimension,
            "metric": collection.metric,
            "points": collection.points_count,
            "embedding_model": collection.embedding_model,
            "has_embeddings": collection.has_embeddings,
            "backend": collection.connection.provider_type,
        }
    }


def _op_add_texts(request, collection, handle, data):
    items = _list_param(data, "items")
    texts = [i.get("text") for i in items if isinstance(i, dict)]
    costs = {
        "embed_calls": _embed_calls(collection, len(items)),
        "text_bytes": _text_bytes(texts),
        "points": len(items),
    }
    over = _over_budget(request, **costs)
    if over:
        return over
    result = handle.add_texts(items)
    _charge(request, **costs)
    return result


def _op_add_vectors(request, collection, handle, data):
    items = _list_param(data, "items")
    over = _over_budget(request, points=len(items))
    if over:
        return over
    result = handle.add_vectors(items)
    _charge(request, points=len(items))
    return result


def _op_search(request, collection, handle, data):
    query = data.get("query")
    costs = {
        "embed_calls": _embed_calls(collection, 1),
        "text_bytes": _text_bytes([query]),
    }
    over = _over_budget(request, **costs)
    if over:
        return over
    results = handle.search(query, k=_int_param(data, "k", 5), filter=data.get("filter"))
    _charge(request, **costs)
    return {"results": results}


def _op_search_vector(request, collection, handle, data):
    vector = data.get("vector")
    if not isinstance(vector, list):
        raise _BadRequest("'vector' must be a list of numbers")
    return {
        "results": handle.search_vector(
            vector, k=_int_param(data, "k", 5), filter=data.get("filter")
        )
    }


def _op_get(request, collection, handle, data):
    ids = _list_param(data, "ids")
    return {"points": handle.get(ids, include_vectors=bool(data.get("include_vectors")))}


def _op_delete(request, collection, handle, data):
    ids = _list_param(data, "ids", required=False)
    flt = data.get("filter")
    if (ids is None) == (flt is None):
        raise _BadRequest("delete takes 'ids' or 'filter', not both")
    if ids is not None:
        over = _over_budget(request, points=len(ids))
        if over:
            return over
        result = handle.delete(ids=ids)
    else:
        # The size of a filtered delete is known only afterwards: refuse once the
        # budget is spent, charge what was actually removed.
        over = _over_budget(request, points=1)
        if over:
            return over
        result = handle.delete(filter=flt)
    _charge(request, points=result["deleted"])
    return result


def _op_count(request, collection, handle, data):
    return {"count": handle.count(filter=data.get("filter"))}


def _op_scroll(request, collection, handle, data):
    cursor = data.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise _BadRequest("'cursor' must be the string a previous scroll returned")
    items, next_cursor = handle.scroll(
        cursor=cursor,
        limit=_int_param(data, "limit", 100),
        filter=data.get("filter"),
        include_vectors=bool(data.get("include_vectors")),
    )
    return {"points": items, "next_cursor": next_cursor}


def _op_embed(request, collection, handle, data):
    texts = _list_param(data, "texts")
    input_type = data.get("input_type", "document")
    if input_type not in ("document", "query"):
        raise _BadRequest("'input_type' is 'document' or 'query'")
    costs = {
        "embed_calls": _embed_calls(collection, len(texts)),
        "text_bytes": _text_bytes(texts),
    }
    over = _over_budget(request, **costs)
    if over:
        return over
    vectors = handle.embed(texts, input_type=input_type)
    _charge(request, **costs)
    return {"vectors": vectors}


def _op_credentials(request, collection, handle, data):
    from core.services import VectorService

    return {"credentials": VectorService.credentials_for(collection)}


_OPS = {
    "info": _op_info,
    "add_texts": _op_add_texts,
    "add_vectors": _op_add_vectors,
    "search": _op_search,
    "search_vector": _op_search_vector,
    "get": _op_get,
    "delete": _op_delete,
    "count": _op_count,
    "scroll": _op_scroll,
    "embed": _op_embed,
    "credentials": _op_credentials,
}
# info and credentials never touch the store; everything else opens it.
_NO_STORE = {"info", "credentials"}


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def collection_op(request: HttpRequest, name: str, op: str) -> JsonResponse:
    from core.services import VectorError, VectorService

    handler = _OPS.get(op)
    if handler is None:
        return _error("not_found", f"Unknown vector operation '{op}'.")
    data, err = _payload(request)
    if err:
        return err
    collection, err = _resolve(request, name)
    if err:
        return err
    try:
        handle = None if op in _NO_STORE else VectorService.handle(collection)
        result = handler(request, collection, handle, data)
    except _BadRequest as e:
        return _error("invalid", str(e))
    except VectorError as e:
        return _error(e.code, str(e))
    except Exception:
        # The helper expects JSON whatever happens; the trace stays server-side.
        logger.exception("Vector loopback %s on %r failed", op, name)
        return _error("error", f"The vector operation '{op}' failed inside PyRun; see the server log.")
    if isinstance(result, JsonResponse):
        return result
    return JsonResponse({"ok": True, **result})
