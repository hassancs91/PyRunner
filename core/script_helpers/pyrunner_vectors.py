"""
PyRun vector collections for scripts.

Store texts or vectors in a collection and search them by meaning — server-side,
so your script never handles an embedding API key or a Qdrant credential and
needs nothing installed. Mirrors ``pyrunner_storage``: standard library only,
talking to PyRun's internal loopback API with the run's signed token.

Collections are created on the **Vectors** page (or provisioned by a plugin).
A plugin's collection is available to that plugin's scripts only and is named
``"<plugin>:<key>"``; every other collection is available to every script in
its workspace.

Usage:
    from pyrunner_vectors import get_collection

    docs = get_collection("docs")
    docs.add_texts([
        {"id": "p1", "text": "Schedule a script with cron", "payload": {"tier": "public"}},
    ])
    for hit in docs.search("how do I run something every morning?", k=5,
                           filter={"tier": "public"}):
        print(hit["id"], round(hit["score"], 3), hit["text"])

    docs.add_vectors([{"id": "v1", "vector": [0.1, 0.2, ...], "payload": {}}])
    docs.search_vector([0.1, 0.2, ...], k=5)
    docs.get(["p1"])                       # points by id (missing ids skipped)
    docs.count(filter={"tier": "public"})
    docs.delete(ids=["p1"])                # or delete(filter={...})
    page, cursor = docs.scroll(limit=100)  # pass cursor back for the next page
    vectors = docs.embed(["some text"])    # the collection's provider, counted
    creds = docs.credentials()             # Qdrant only, when the operator allows it

Filters (JSON, top-level payload fields): ``{"field": value}``,
``{"field": {"$in": [...]}}``, ``$gt`` / ``$gte`` / ``$lt`` / ``$lte``,
``{"field": {"$prefix": "x"}}``, ``{"must": [...], "must_not": [...]}``. Types
are strict, an array field matches on any element, ``{"field": None}`` means
missing or null. Scores are always "higher is better".

PyRun brakes each run at 2 000 embedding calls, 20 MB of embedded text and
200 000 points written or deleted; past that a call raises ``VectorError`` with
code ``run_budget_exceeded``.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Items per request — well under the server's 1 000-item / 32 MB ceilings, so a
# long list is sent in several calls instead of one huge body.
TEXT_BATCH = 64
VECTOR_BATCH = 256
ID_BATCH = 1000


class VectorError(Exception):
    """A vector operation failed. ``code`` is stable (``invalid``,
    ``unsupported_filter``, ``run_budget_exceeded``, ``embedding_failed``, …)."""

    def __init__(self, message, code="error", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class CollectionNotFound(VectorError, LookupError):
    """The collection does not exist, or this script may not use it."""


def _request(name: str, op: str, payload: dict) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise VectorError("PyRun vector collections are not available in this run context.")
    url = f"{base.rstrip('/')}/internal/vectors/{urllib.parse.quote(name, safe='')}/{op}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        code, message = "error", raw.decode("utf-8", "ignore")
        try:
            err = (json.loads(raw) if raw else {}).get("error") or {}
            code = err.get("code") or code
            message = err.get("message") or message
        except ValueError:
            pass
        if e.code == 404 and code == "not_found":
            raise CollectionNotFound(message, code, e.code) from None
        raise VectorError(f"{op} failed (HTTP {e.code}): {message}", code, e.code) from None
    except urllib.error.URLError as e:
        raise VectorError(f"Could not reach PyRun: {e}") from None


def _batches(items, size):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Collection:
    """One vector collection. Get it with ``get_collection(name)``."""

    def __init__(self, name: str, info: dict):
        self.name = name
        self.dimension = info.get("dimension")
        self.metric = info.get("metric")
        self.embedding_model = info.get("embedding_model") or ""
        self.has_embeddings = bool(info.get("has_embeddings"))
        self.backend = info.get("backend")

    def __repr__(self):
        return f"<Collection {self.name!r} dim={self.dimension} metric={self.metric}>"

    def _call(self, op, **payload):
        return _request(self.name, op, payload)

    # ---------------------------------------------------------------- writes

    def add_texts(self, items) -> dict:
        """Embed and store ``[{"id", "text", "payload"?}]`` (upsert by id).

        Returns ``{"added", "tokens", "truncated"}`` summed over the batches;
        texts over 8 000 characters are truncated before embedding and counted.
        """
        total = {"added": 0, "tokens": 0, "truncated": 0}
        for batch in _batches(items, TEXT_BATCH):
            result = self._call("add_texts", items=batch)
            for key in total:
                total[key] += int(result.get(key) or 0)
        return total

    def add_vectors(self, items) -> dict:
        """Store ``[{"id", "vector", "payload"?, "text"?}]`` as given (upsert by id)."""
        added = 0
        for batch in _batches(items, VECTOR_BATCH):
            added += int(self._call("add_vectors", items=batch).get("added") or 0)
        return {"added": added}

    def delete(self, ids=None, filter=None) -> dict:  # noqa: A002 — the contract's name
        """Delete by ``ids`` or by ``filter`` (exactly one). Returns ``{"deleted"}``."""
        if (ids is None) == (filter is None):
            raise VectorError("delete() takes ids or filter, not both", "invalid")
        if filter is not None:
            return {"deleted": int(self._call("delete", filter=filter).get("deleted") or 0)}
        deleted = 0
        for batch in _batches(ids, ID_BATCH):
            deleted += int(self._call("delete", ids=batch).get("deleted") or 0)
        return {"deleted": deleted}

    # ---------------------------------------------------------------- reads

    def search(self, query: str, k: int = 5, filter=None) -> list:  # noqa: A002
        """Embed ``query`` with the collection's provider; the ``k`` nearest points as
        ``[{"id", "score", "payload", "text"}]``, best first."""
        return self._call("search", query=query, k=k, filter=filter).get("results", [])

    def search_vector(self, vector, k: int = 5, filter=None) -> list:  # noqa: A002
        """The ``k`` nearest points to your own ``vector``, best first."""
        return self._call("search_vector", vector=list(vector), k=k, filter=filter).get("results", [])

    def get(self, ids, include_vectors: bool = False) -> list:
        """Points by id, in the order asked; ids that don't exist are skipped."""
        points = []
        for batch in _batches(ids, ID_BATCH):
            points.extend(
                self._call("get", ids=batch, include_vectors=include_vectors).get("points", [])
            )
        return points

    def count(self, filter=None) -> int:  # noqa: A002
        return int(self._call("count", filter=filter).get("count") or 0)

    def scroll(self, cursor=None, limit: int = 100, filter=None, include_vectors: bool = False):  # noqa: A002
        """One page of points: ``(items, next_cursor)``; ``next_cursor`` is None at the end."""
        result = self._call(
            "scroll", cursor=cursor, limit=limit, filter=filter, include_vectors=include_vectors
        )
        return result.get("points", []), result.get("next_cursor")

    def embed(self, texts, input_type: str = "document") -> list:
        """Embed ``texts`` with the collection's provider (nothing is stored).
        ``input_type="query"`` for search queries."""
        vectors = []
        for batch in _batches(texts, VECTOR_BATCH):
            vectors.extend(self._call("embed", texts=batch, input_type=input_type).get("vectors", []))
        return vectors

    def credentials(self) -> dict:
        """The Qdrant URL, API key and physical collection name, for ``qdrant-client``.

        Only for a collection on a Qdrant connection whose operator enabled
        "Share credentials with runs" — raises ``VectorError`` otherwise. The
        key is server-wide: treat it like a password.
        """
        return self._call("credentials").get("credentials", {})


def get_collection(name: str) -> Collection:
    """Open a collection by name; raises ``CollectionNotFound`` if it is missing
    or not available to this script."""
    info = _request(name, "info", {}).get("collection", {})
    return Collection(name, info)
