"""
Qdrant vector backend: a thin adapter on Qdrant's REST API.

No ``qdrant-client`` dependency — the handful of calls PyRun needs are plain
JSON over ``requests``:

    GET    /collections/{c}                       does it exist, what shape
    PUT    /collections/{c}                       create (size + distance)
    DELETE /collections/{c}                       drop
    PUT    /collections/{c}/points?wait=true      upsert
    POST   /collections/{c}/points                retrieve by id
    POST   /collections/{c}/points/query          nearest neighbours (Query API, Qdrant >= 1.10)
    POST   /collections/{c}/points/count          exact count
    POST   /collections/{c}/points/scroll         page through
    POST   /collections/{c}/points/delete?wait=true

The contract is the Local store's, and the contract test-suite runs the same
cases against both:

- **Point ids.** Qdrant takes only integers and UUIDs, so a caller's string id is
  stored as ``uuid5(POINT_NAMESPACE, id)`` and the original id and text ride in
  the payload under ``_pyrun_id`` / ``_pyrun_text`` (reserved on every backend),
  stripped on the way out.
- **Scores** are higher-is-better: cosine and dot come back that way, euclid
  comes back as a distance and is negated.
- **Filters** compile through ``filters.to_qdrant``. When the compiled filter is
  a superset (``$prefix``, string ranges, null equality) every hit is re-checked
  with ``filters.matches``, and search pages through the score-ordered superset
  until it has ``k`` exact hits — the top-k stays exact, at the cost of more
  server work for those filters.
- **Writes** use ``wait=true`` so a read straight after a write sees it.
- **Every request**: 10 s timeout, 3 retries on 429 / 5xx / connection errors
  with a short backoff. The URL is netguard-checked when the store is built.
  Error messages never carry the server URL: they reach scripts, the URL is the
  operator's.
"""

import logging
import time
import uuid
from urllib.parse import quote

import requests

from core.netguard import is_safe_endpoint_url
from core.vectors import filters
from core.vectors.errors import BackendUnavailable, StoreError
from core.vectors.points import (
    RESERVED_PREFIX,
    check_id,
    check_payload,
    check_text,
    check_vector,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
RETRY_BACKOFF = (0.5, 1.0, 2.0)
UPSERT_BATCH = 256
ID_BATCH = 1000
SCAN_BATCH = 256
MAX_LIMIT = 1000
# Fixed forever: changing it would orphan every point already stored.
POINT_NAMESPACE = uuid.UUID("9c6b0d5e-3f4a-5b8c-9d1e-2a7f6c4b8e01")
ID_KEY = f"{RESERVED_PREFIX}id"
TEXT_KEY = f"{RESERVED_PREFIX}text"
DISTANCES = {"cosine": "Cosine", "dot": "Dot", "euclid": "Euclid"}


def point_uuid(pid: str) -> str:
    """The Qdrant id a caller's string id is stored under."""
    return str(uuid.uuid5(POINT_NAMESPACE, pid))


def make_session():
    """The HTTP session a store uses (the seam the tests replace)."""
    return requests.Session()


class QdrantRejected(StoreError):
    """Qdrant refused the request as invalid (a 4xx other than auth / missing)."""


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _error_message(response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "")[:200]
    status = data.get("status") if isinstance(data, dict) else None
    if isinstance(status, dict) and status.get("error"):
        return str(status["error"])[:200]
    return ""


class QdrantStore:
    """One Qdrant collection. Cheap to construct; one HTTP session per instance."""

    def __init__(self, url, name, dim, metric, *, api_key="", session=None):
        url = (url or "").rstrip("/")
        if not url:
            raise BackendUnavailable("the Qdrant connection has no URL")
        ok, why = is_safe_endpoint_url(url)
        if not ok:
            raise BackendUnavailable(f"the Qdrant URL is not allowed: {why}")
        if metric not in DISTANCES:
            raise StoreError(f"unknown metric {metric!r}")
        self.url = url
        self.name = name
        self.dim = int(dim)
        self.metric = metric
        self._api_key = api_key or ""
        self._session = session or make_session()
        self._base = f"/collections/{quote(name, safe='')}"

    # ------------------------------------------------------------------ lifecycle

    def info(self):
        """The collection's ``result`` block, or ``None`` when it does not exist."""
        body = self._request("GET", self._base, allow_404=True)
        return None if body is None else (body.get("result") or {})

    def create(self) -> "QdrantStore":
        """Create the collection, or adopt an existing EMPTY one of the same shape.

        Only a row that has never had a working store calls this (a new
        collection, or Retry on a row in status=error), so a server collection
        that already holds points is somebody else's — or the leftover of a
        collection whose drop failed — and attaching to it would expose or mix
        those points. It is refused with a message saying what to do.
        """
        existing = self.info()
        if existing is not None:
            self._check_shape(existing)
            if int(existing.get("points_count") or 0) > 0:
                raise StoreError(
                    f"Qdrant already has a collection named {self.name!r} with points in it; "
                    "delete it on the server or pick another collection name"
                )
            return self
        self._request(
            "PUT",
            self._base,
            json={"vectors": {"size": self.dim, "distance": DISTANCES[self.metric]}},
        )
        return self

    def drop(self) -> None:
        self._request("DELETE", self._base, allow_404=True)

    def _check_shape(self, info) -> None:
        vectors = ((info.get("config") or {}).get("params") or {}).get("vectors") or {}
        if "size" not in vectors:
            raise StoreError(
                f"Qdrant collection {self.name!r} uses named vectors; PyRun manages "
                "single-vector collections only"
            )
        size, distance = vectors.get("size"), vectors.get("distance")
        if size != self.dim or distance != DISTANCES[self.metric]:
            raise StoreError(
                f"Qdrant collection {self.name!r} is size={size} distance={distance}, "
                f"but this collection is pinned to size={self.dim} "
                f"distance={DISTANCES[self.metric]}"
            )

    # ------------------------------------------------------------------ writes

    def upsert(self, points) -> int:
        rows = [self._encode_point(p) for p in points]
        for chunk in _chunks(rows, UPSERT_BATCH):
            self._request("PUT", f"{self._base}/points", params={"wait": "true"}, json={"points": chunk})
        return len(rows)

    def delete(self, ids=None, flt=None) -> int:
        """Delete by ids or by filter (exactly one). Returns the number removed."""
        if (ids is None) == (flt is None):
            raise StoreError("delete takes ids or a filter, not both")
        if ids is not None:
            uuids = list(dict.fromkeys(point_uuid(str(i)) for i in ids))
            present = []
            for chunk in _chunks(uuids, ID_BATCH):
                present.extend(raw["id"] for raw in self._retrieve(chunk, with_vector=False))
            self._delete_ids(present)
            return len(present)

        compiled = filters.to_qdrant(flt)
        if compiled.matches_nothing:
            return 0
        if compiled.exact:
            removed = self._count(compiled.filter)
            if removed:
                self._post("points/delete", {"filter": compiled.filter or {}}, wait=True)
            return removed
        doomed = [
            raw["id"]
            for raw in self._scan(compiled.filter)
            if filters.matches(self._user_payload(raw), flt)
        ]
        self._delete_ids(doomed)
        return len(doomed)

    def _delete_ids(self, uuids) -> None:
        for chunk in _chunks(list(uuids), ID_BATCH):
            self._post("points/delete", {"points": chunk}, wait=True)

    # ------------------------------------------------------------------ reads

    def count(self, flt=None) -> int:
        compiled = filters.to_qdrant(flt)
        if compiled.matches_nothing:
            return 0
        if compiled.exact:
            return self._count(compiled.filter)
        return sum(
            1 for raw in self._scan(compiled.filter) if filters.matches(self._user_payload(raw), flt)
        )

    def get(self, ids, include_vectors=False):
        ids = [str(i) for i in ids]
        if not ids:
            return []
        found = {}
        for chunk in _chunks(list(dict.fromkeys(ids)), ID_BATCH):
            for raw in self._retrieve([point_uuid(i) for i in chunk], with_vector=include_vectors):
                item = self._decode(raw, include_vectors)
                found[item["id"]] = item
        return [dict(found[i]) for i in ids if i in found]

    def scroll(self, cursor=None, limit=100, flt=None, include_vectors=False):
        """Page through points. Returns ``(items, next_cursor)``.

        The cursor is opaque (a Qdrant point id) and the order is Qdrant's id
        order. A non-``None`` cursor always has at least one more point behind
        it, exactly like the Local store.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        compiled = filters.to_qdrant(flt)
        if compiled.matches_nothing:
            return [], None
        if compiled.exact:
            body = {"limit": limit, "with_payload": True, "with_vector": include_vectors}
            if compiled.filter:
                body["filter"] = compiled.filter
            if cursor:
                body["offset"] = cursor
            result = self._post("points/scroll", body).get("result") or {}
            items = [self._decode(raw, include_vectors) for raw in result.get("points") or []]
            return items, result.get("next_page_offset")

        items, next_cursor = [], None
        for raw in self._scan(compiled.filter, offset=cursor, with_vector=include_vectors):
            if not filters.matches(self._user_payload(raw), flt):
                continue
            if len(items) == limit:
                next_cursor = raw["id"]  # Qdrant offsets are inclusive
                break
            items.append(self._decode(raw, include_vectors))
        return items, next_cursor

    def search(self, vector, k=10, flt=None):
        k = max(1, min(int(k), MAX_LIMIT))
        query = check_vector(vector, self.dim)
        compiled = filters.to_qdrant(flt)
        if compiled.matches_nothing:
            return []
        if compiled.exact:
            return [self._hit(raw) for raw in self._query(query, compiled.filter, k, 0)]

        hits, offset, page = [], 0, min(max(k * 4, 64), MAX_LIMIT)
        while True:
            batch = self._query(query, compiled.filter, page, offset)
            for raw in batch:
                if filters.matches(self._user_payload(raw), flt):
                    hits.append(self._hit(raw))
                    if len(hits) == k:
                        return hits
            if len(batch) < page:
                return hits
            offset += page

    # ------------------------------------------------------------------ internals

    def _encode_point(self, point):
        if not isinstance(point, dict):
            raise StoreError("each point must be an object")
        pid = check_id(point.get("id"))
        if "vector" not in point:
            raise StoreError(f"point {pid!r} has no vector")
        vector = check_vector(point["vector"], self.dim)
        payload, _ = check_payload(pid, point.get("payload"))
        text = check_text(pid, point.get("text"))
        stored = dict(payload)
        stored[ID_KEY] = pid
        if text is not None:
            stored[TEXT_KEY] = text
        return {"id": point_uuid(pid), "vector": vector, "payload": stored}

    @staticmethod
    def _user_payload(raw) -> dict:
        payload = raw.get("payload") or {}
        return {k: v for k, v in payload.items() if not k.startswith(RESERVED_PREFIX)}

    def _decode(self, raw, include_vectors):
        payload = dict(raw.get("payload") or {})
        pid = payload.pop(ID_KEY, None) or str(raw.get("id"))
        text = payload.pop(TEXT_KEY, None)
        item = {"id": pid, "payload": payload, "text": text}
        if include_vectors:
            item["vector"] = [float(x) for x in raw.get("vector") or []]
        return item

    def _hit(self, raw):
        item = self._decode(raw, False)
        score = float(raw.get("score") or 0.0)
        return {
            "id": item["id"],
            "score": -score if self.metric == "euclid" else score,
            "payload": item["payload"],
            "text": item["text"],
        }

    def _retrieve(self, uuids, *, with_vector):
        body = {"ids": list(uuids), "with_payload": True, "with_vector": with_vector}
        return self._post("points", body).get("result") or []

    def _count(self, qfilter) -> int:
        body = {"exact": True}
        if qfilter:
            body["filter"] = qfilter
        return int((self._post("points/count", body).get("result") or {}).get("count") or 0)

    def _query(self, vector, qfilter, limit, offset):
        body = {"query": vector, "limit": limit, "with_payload": True}
        if offset:
            body["offset"] = offset
        if qfilter:
            body["filter"] = qfilter
        return (self._post("points/query", body).get("result") or {}).get("points") or []

    def _scan(self, qfilter, *, offset=None, with_vector=False):
        """Every point the server filter selects, page by page (id order)."""
        while True:
            body = {"limit": SCAN_BATCH, "with_payload": True, "with_vector": with_vector}
            if qfilter:
                body["filter"] = qfilter
            if offset:
                body["offset"] = offset
            result = self._post("points/scroll", body).get("result") or {}
            yield from result.get("points") or []
            offset = result.get("next_page_offset")
            if not offset:
                return

    def _post(self, op, body, *, wait=False):
        params = {"wait": "true"} if wait else None
        return self._request("POST", f"{self._base}/{op}", params=params, json=body)

    def _request(self, method, path, *, params=None, json=None, allow_404=False):
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        url = f"{self.url}{path}"
        last_error = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            try:
                response = self._session.request(
                    method, url, params=params, json=json, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except requests.RequestException as e:
                logger.warning("Qdrant %s %s failed: %s", method, path, e)
                last_error = BackendUnavailable(
                    f"could not reach the Qdrant server ({e.__class__.__name__})"
                )
            else:
                status = response.status_code
                if status != 429 and status < 500:
                    return self._parse(response, allow_404)
                last_error = BackendUnavailable(
                    f"Qdrant answered HTTP {status}: {_error_message(response) or 'server error'}"
                )
            if attempt < len(RETRY_BACKOFF):
                time.sleep(RETRY_BACKOFF[attempt])
        raise last_error

    def _parse(self, response, allow_404):
        status = response.status_code
        if status == 404:
            if allow_404:
                return None
            raise BackendUnavailable(
                f"the Qdrant collection {self.name!r} does not exist on the server "
                "(Retry on the collection's page recreates it)"
            )
        if status in (401, 403):
            raise BackendUnavailable("Qdrant rejected the API key")
        if status >= 400:
            raise QdrantRejected(
                f"Qdrant rejected the request (HTTP {status}): "
                f"{_error_message(response) or 'bad request'}"
            )
        try:
            body = response.json()
        except ValueError as e:
            raise BackendUnavailable("Qdrant returned a non-JSON response") from e
        return body if isinstance(body, dict) else {}
