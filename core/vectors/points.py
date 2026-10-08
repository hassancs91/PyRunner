"""
Point validation shared by every vector backend.

A point is ``{"id": str, "vector": [float, ...], "payload": {...}?, "text": str?}``.
The Local and Qdrant stores validate through these helpers so a bad point fails
with the same message whatever the backend, and raise the same error classes so
``VectorService`` maps them onto one set of ``VectorError`` codes (the
classes live in ``core.vectors.errors``).

Payload keys starting with ``RESERVED_PREFIX`` are refused on every backend: the
Qdrant adapter keeps the caller's string id and text there (Qdrant point ids
must be integers or UUIDs), and a contract that accepted them on Local only
would break the day a collection moves to Qdrant.
"""

import json
import math

from core.vectors.errors import DimensionMismatch, StoreError

MAX_ID_LENGTH = 256
RESERVED_PREFIX = "_pyrun_"


def check_id(pid) -> str:
    if not isinstance(pid, str) or not pid or len(pid) > MAX_ID_LENGTH:
        raise StoreError(f"point id must be a non-empty string of at most {MAX_ID_LENGTH} chars")
    return pid


def check_payload(pid, payload) -> tuple[dict, str]:
    """Return ``(payload, payload_json)`` for a point, refusing reserved keys."""
    payload = payload or {}
    if not isinstance(payload, dict):
        raise StoreError(f"point {pid!r}: payload must be an object")
    for key in payload:
        if isinstance(key, str) and key.startswith(RESERVED_PREFIX):
            raise StoreError(
                f"point {pid!r}: payload keys starting with {RESERVED_PREFIX!r} are reserved"
            )
    try:
        payload_json = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise StoreError(f"point {pid!r}: payload is not JSON-serializable: {e}") from e
    return payload, payload_json


def check_text(pid, text):
    if text is not None and not isinstance(text, str):
        raise StoreError(f"point {pid!r}: text must be a string")
    return text


def check_vector(vector, dim: int) -> list:
    """A plain-Python vector check (the Local store does the same with numpy)."""
    if isinstance(vector, (str, bytes)) or not hasattr(vector, "__len__"):
        raise StoreError("vector must be a list of numbers")
    try:
        values = [float(x) for x in vector]
    except (TypeError, ValueError) as e:
        raise StoreError(f"vector must be a list of numbers: {e}") from e
    if len(values) != dim:
        raise DimensionMismatch(f"vector has {len(values)} dimensions, collection expects {dim}")
    if not all(math.isfinite(x) for x in values):
        raise StoreError("vector contains NaN or infinity")
    return values
