"""
Vector seam Stage 2 — the Qdrant adapter, the loopback door, the helper and VectorAPI.

- ``FakeQdrant``: an in-memory server for the REST calls the adapter makes, with
  Qdrant's documented filter semantics (match value / any / text, range,
  is_empty, is_null, has_id, nested must / should / must_not, any-element on
  arrays). It is the adapter's HTTP session in every test here.
- **Contract suite** (``_ContractCases``): the same cases against the Local store
  and the Qdrant adapter — same results, same shapes, higher-is-better scores
  for every metric, every filter of the grammar (including the ones Qdrant can
  only answer as a superset). ``LiveQdrantContractTests`` runs the identical
  suite against a real server when ``PYRUNNER_TEST_QDRANT_URL`` is set:

      PYRUNNER_TEST_QDRANT_URL=http://localhost:6333 \\
          python manage.py test core.test_vectors_stage2.LiveQdrantContractTests

- the filter compiler: translation table, polarity under ``must_not``, and a
  three-way check (server evaluation ⊇ / = reference) on random payloads
- the adapter on the wire: create / adopt / refuse, uuid5 ids + reserved payload
  keys, wait=true, api-key header, retries, error mapping, no URL in messages
- VectorService + console on a Qdrant connection: physical names, ref clashes,
  disabled connections, leftovers, credentials gate, Services form
- the loopback ``/internal/vectors/<name>/<op>``: auth, the access rule, every
  op, VectorError → HTTP, the per-run brakes
- ``pyrunner_vectors`` end-to-end through the real loopback view
- ``VectorAPI``: idempotent provision on (owner, key), pinned shape, scoping
"""

import io
import json
import os
import tempfile
import urllib.error
import uuid
from pathlib import Path
from unittest import mock, skipUnless
from urllib.parse import unquote, urlparse

import numpy as np
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import (
    Environment,
    Run,
    Script,
    User,
    VectorCollection,
    VectorConnection,
    Workspace,
    WorkspaceMembership,
)
from core.services import VectorError, VectorService
from core.services.datastore_token import mint_datastore_token
from core.test_vectors import _TEST_KEY, _provider, _setup_wizard_off, _vec, fake_embeddings
from core.vectors import filters, local_store, qdrant_store
from core.vectors.embeddings import EmbeddingClient, EmbeddingError
from core.vectors.errors import BackendUnavailable, DimensionMismatch, StoreError
from core.vectors.local_store import LocalStore, clear_cache
from core.vectors.qdrant_store import POINT_NAMESPACE, QdrantRejected, QdrantStore, point_uuid

_TMP = Path(tempfile.mkdtemp(prefix="pyrun-vectors2-"))
QDRANT_URL = "http://localhost:6333"
LIVE_QDRANT_URL = os.environ.get("PYRUNNER_TEST_QDRANT_URL", "")
_MISSING = object()
_DISTANCE_METRIC = {"Cosine": "cosine", "Dot": "dot", "Euclid": "euclid"}


# =============================================================================
# A fake Qdrant server
# =============================================================================

class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _ok(result):
    return _Resp(200, {"result": result, "status": "ok", "time": 0.0})


def _same(element, target):
    """Qdrant's match: keywords, integers and booleans never cross-match."""
    if isinstance(target, bool) or isinstance(element, bool):
        return isinstance(target, bool) and isinstance(element, bool) and element == target
    if isinstance(target, str):
        return isinstance(element, str) and element == target
    return isinstance(element, int) and element == target


def _in_range(element, bounds):
    if isinstance(element, bool) or not isinstance(element, (int, float)):
        return False
    checks = {"gt": element.__gt__, "gte": element.__ge__, "lt": element.__lt__, "lte": element.__le__}
    return all(checks[op](value) for op, value in bounds.items() if value is not None)


def qdrant_condition(point_id, payload, cond):
    """Evaluate one Qdrant condition (or nested filter) against a stored point."""
    if any(key in cond for key in ("must", "should", "must_not")):
        return qdrant_filter_matches(point_id, payload, cond)
    if "has_id" in cond:
        return point_id in cond["has_id"]
    if "is_empty" in cond:
        value = payload.get(cond["is_empty"]["key"], _MISSING)
        return value is _MISSING or value is None or value == []
    if "is_null" in cond:
        key = cond["is_null"]["key"]
        return key in payload and payload[key] is None
    value = payload.get(cond["key"], _MISSING)
    if value is _MISSING:
        return False
    elements = value if isinstance(value, list) else [value]
    if "match" in cond:
        match = cond["match"]
        if "value" in match:
            return any(_same(e, match["value"]) for e in elements)
        if "any" in match:
            return any(_same(e, t) for e in elements for t in match["any"])
        if "text" in match:
            return any(isinstance(e, str) and match["text"] in e for e in elements)
    if "range" in cond:
        return any(_in_range(e, cond["range"]) for e in elements)
    raise AssertionError(f"FakeQdrant: unknown condition {cond!r}")


def qdrant_filter_matches(point_id, payload, flt):
    if not flt:
        return True
    if not all(qdrant_condition(point_id, payload, c) for c in flt.get("must", [])):
        return False
    if any(qdrant_condition(point_id, payload, c) for c in flt.get("must_not", [])):
        return False
    should = flt.get("should")
    if should and not any(qdrant_condition(point_id, payload, c) for c in should):
        return False
    return True


class FakeQdrant:
    """The adapter's HTTP session: ``request(method, url, params, json, headers, timeout)``.

    ``fail_next`` queues responses (status codes) or exceptions returned before
    the real handling — for the retry tests.
    """

    def __init__(self):
        self.collections = {}
        self.calls = []
        self.fail_next = []

    # -- helpers for tests
    def points(self, name):
        return self.collections[name]["points"]

    def paths(self):
        return [(method, path) for method, path, *_ in self.calls]

    # -- the session interface
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append((method, path, params, json, headers or {}))
        if self.fail_next:
            failure = self.fail_next.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return _Resp(failure, {"status": {"error": "injected failure"}})
        parts = path.strip("/").split("/")
        name, op = unquote(parts[1]), "/".join(parts[2:])
        collection = self.collections.get(name)
        if not op:
            return self._collection(method, name, collection, json)
        if collection is None:
            return _Resp(404, {"status": {"error": f"Not found: Collection `{name}` doesn't exist!"}})
        handler = {
            ("PUT", "points"): self._upsert,
            ("POST", "points"): self._retrieve,
            ("POST", "points/query"): self._query,
            ("POST", "points/count"): self._count,
            ("POST", "points/scroll"): self._scroll,
            ("POST", "points/delete"): self._delete,
        }[(method, op)]
        return handler(collection, json or {})

    def _collection(self, method, name, collection, body):
        if method == "GET":
            if collection is None:
                return _Resp(404, {"status": {"error": f"Collection `{name}` doesn't exist!"}})
            return _ok(
                {
                    "status": "green",
                    "points_count": len(collection["points"]),
                    "config": {"params": {"vectors": {"size": collection["size"], "distance": collection["distance"]}}},
                }
            )
        if method == "PUT":
            if collection is not None:
                return _Resp(409, {"status": {"error": f"Collection `{name}` already exists!"}})
            vectors = body["vectors"]
            self.collections[name] = {"size": vectors["size"], "distance": vectors["distance"], "points": {}}
            return _ok(True)
        if method == "DELETE":
            return _ok(self.collections.pop(name, None) is not None)
        raise AssertionError(method)

    @staticmethod
    def _matching(collection, flt):
        return [
            (pid, p) for pid, p in sorted(collection["points"].items())
            if qdrant_filter_matches(pid, p["payload"], flt)
        ]

    def _upsert(self, collection, body):
        for point in body["points"]:
            vector = np.asarray(point["vector"], dtype=float)
            if vector.shape[0] != collection["size"]:
                return _Resp(400, {"status": {"error": "Wrong input: Vector dimension error"}})
            if collection["distance"] == "Cosine":
                norm = np.linalg.norm(vector)
                vector = vector / norm if norm else vector
            uuid.UUID(point["id"])  # Qdrant refuses any id that is not a UUID or an integer
            collection["points"][point["id"]] = {"vector": vector, "payload": dict(point.get("payload") or {})}
        return _ok({"operation_id": 1, "status": "completed"})

    def _record(self, pid, point, with_vector, score=None):
        record = {"id": pid, "version": 0, "payload": dict(point["payload"])}
        if with_vector:
            record["vector"] = point["vector"].tolist()
        if score is not None:
            record["score"] = score
        return record

    def _retrieve(self, collection, body):
        found = [pid for pid in body["ids"] if pid in collection["points"]]
        # Qdrant does not promise request order: hand them back reversed.
        return _ok([self._record(pid, collection["points"][pid], body.get("with_vector")) for pid in reversed(found)])

    def _query(self, collection, body):
        query = np.asarray(body["query"], dtype=float)
        distance = collection["distance"]
        if distance == "Cosine":
            query = query / np.linalg.norm(query)
        scored = []
        for pid, point in self._matching(collection, body.get("filter")):
            if distance == "Euclid":
                scored.append((float(np.linalg.norm(point["vector"] - query)), pid, point))
            else:
                scored.append((float(point["vector"] @ query), pid, point))
        scored.sort(key=lambda s: s[0], reverse=distance != "Euclid")
        offset, limit = body.get("offset", 0), body["limit"]
        page = scored[offset : offset + limit]
        return _ok({"points": [self._record(pid, point, False, score) for score, pid, point in page]})

    def _count(self, collection, body):
        return _ok({"count": len(self._matching(collection, body.get("filter")))})

    def _scroll(self, collection, body):
        matching = self._matching(collection, body.get("filter"))
        offset = body.get("offset")
        if offset:
            matching = [(pid, p) for pid, p in matching if pid >= offset]
        limit = body["limit"]
        page, rest = matching[:limit], matching[limit:]
        return _ok(
            {
                "points": [self._record(pid, p, body.get("with_vector")) for pid, p in page],
                "next_page_offset": rest[0][0] if rest else None,
            }
        )

    def _delete(self, collection, body):
        if "points" in body:
            doomed = body["points"]
        else:
            doomed = [pid for pid, _ in self._matching(collection, body["filter"])]
        for pid in doomed:
            collection["points"].pop(pid, None)
        return _ok({"operation_id": 2, "status": "completed"})


# =============================================================================
# Shared data
# =============================================================================

_TIERS = ("public", "agents", "private")


def _contract_points(n, dim, rng):
    points = []
    for i in range(n):
        payload = {
            "tier": _TIERS[i % 3],
            "n": i,
            "half": i / 2,
            "url": f"https://{'a' if i % 2 else 'b'}.example/{i:03d}",
            "topics": [t for j, t in enumerate(("rag", "agents", "evals")) if (i >> j) & 1],
            "flag": i % 4 == 0,
            "mixed": [None, 1, True, "1", 2.5][i % 5],
            "case": ("Alpha", "alpha", "beta")[i % 3],
        }
        if i % 5 == 0:
            payload["opt"] = None
        elif i % 7 == 0:
            payload["opt"] = "set"
        points.append(
            {
                "id": f"p{i:03d}",
                "vector": rng.normal(size=dim).astype(np.float32).astype(float).tolist(),
                "payload": payload,
                "text": f"text {i}" if i % 3 else None,
            }
        )
    return points


# Every shape of the grammar, the ones Qdrant answers exactly and the relaxed ones.
CONTRACT_FILTERS = [
    None,
    {"tier": "public"},
    {"n": {"$gte": 10, "$lt": 40}},
    {"half": 5.5},
    {"n": 7},
    {"flag": True},
    {"tier": {"$in": ["public", "agents"]}},
    {"url": {"$prefix": "https://a."}},
    {"url": {"$gt": "https://b.example/030"}},
    {"case": {"$prefix": "al"}},
    {"case": {"$lt": "a"}},
    {"opt": None},
    {"opt": {"$in": [None, "set"]}},
    {"topics": "rag"},
    {"topics": {"$in": ["agents", "evals"]}},
    {"topics": {"$prefix": "ev"}},
    {"must": [{"tier": "public"}], "must_not": [{"url": {"$prefix": "https://b."}}]},
    {"must_not": [{"opt": None}]},
    {"must_not": [{"url": {"$lt": "https://b"}}, {"n": {"$lte": 3}}]},
    {"tier": {"$in": []}},
    {"mixed": 1},
    {"mixed": True},
    {"mixed": {"$in": ["1", None]}},
    {"mixed": {"$gte": 1}},
]


def _reference_scores(points, query, metric):
    matrix = np.array([p["vector"] for p in points], dtype=np.float64)
    q = np.asarray(query, dtype=np.float64)
    if metric == "cosine":
        scores = (matrix / np.linalg.norm(matrix, axis=1, keepdims=True)) @ (q / np.linalg.norm(q))
    elif metric == "dot":
        scores = matrix @ q
    else:
        scores = -np.sqrt(((matrix - q) ** 2).sum(axis=1))
    return {p["id"]: float(s) for p, s in zip(points, scores)}


# =============================================================================
# Contract suite — one set of cases, every backend
# =============================================================================

class _ContractCases:
    DIM = 8
    N = 120

    def make_store(self, dim, metric):  # pragma: no cover — provided by subclasses
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.rng = np.random.default_rng(11)

    def _loaded(self, metric="cosine"):
        store = self.make_store(self.DIM, metric)
        points = _contract_points(self.N, self.DIM, self.rng)
        self.assertEqual(store.upsert(points), self.N)
        return store, points

    def assert_top_k(self, hits, ref, candidates, k, label):
        ranked = sorted(candidates, key=lambda pid: -ref[pid])
        self.assertEqual(len(hits), min(k, len(ranked)), label)
        for hit in hits:
            self.assertEqual(set(hit), {"id", "score", "payload", "text"}, label)
            self.assertAlmostEqual(hit["score"], ref[hit["id"]], places=3, msg=label)
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True), f"{label}: higher is better")
        if len(ranked) <= k or ref[ranked[k - 1]] - ref[ranked[k]] > 1e-4:
            self.assertEqual({h["id"] for h in hits}, set(ranked[:k]), label)

    def test_exact_top_k_for_every_metric_and_filter(self):
        for metric in ("cosine", "dot", "euclid"):
            store, points = self._loaded(metric)
            by_id = {p["id"]: p for p in points}
            query = self.rng.normal(size=self.DIM).tolist()
            ref = _reference_scores(points, query, metric)
            for flt in CONTRACT_FILTERS:
                candidates = [p["id"] for p in points if filters.matches(p["payload"], flt)]
                hits = store.search(query, k=6, flt=flt)
                self.assert_top_k(hits, ref, candidates, 6, f"{metric} {flt}")
                for hit in hits:
                    self.assertEqual(hit["payload"], by_id[hit["id"]]["payload"])
                    self.assertEqual(hit["text"], by_id[hit["id"]]["text"])

    def test_count_matches_the_reference_for_every_filter(self):
        store, points = self._loaded()
        for flt in CONTRACT_FILTERS:
            expected = sum(1 for p in points if filters.matches(p["payload"], flt))
            self.assertEqual(store.count(flt), expected, flt)

    def test_scroll_visits_every_matching_point_once(self):
        store, points = self._loaded()
        for flt in (None, {"tier": "public"}, {"url": {"$prefix": "https://a."}}, {"opt": None}):
            expected = {p["id"] for p in points if filters.matches(p["payload"], flt)}
            seen, cursor, pages = [], None, 0
            while True:
                items, cursor = store.scroll(cursor=cursor, limit=7, flt=flt)
                pages += 1
                self.assertLessEqual(len(items), 7)
                if cursor is not None:
                    self.assertEqual(len(items), 7, "a page with a cursor behind it is full")
                seen.extend(i["id"] for i in items)
                if cursor is None:
                    break
                self.assertLess(pages, 100)
            self.assertEqual(len(seen), len(set(seen)), flt)
            self.assertEqual(set(seen), expected, flt)

    def test_get_order_missing_and_vectors(self):
        store, points = self._loaded()
        got = store.get(["p005", "nope", "p001"])
        self.assertEqual([g["id"] for g in got], ["p005", "p001"])
        self.assertEqual(got[0]["payload"], points[5]["payload"])
        self.assertNotIn("vector", got[0])
        with_vec = store.get(["p002"], include_vectors=True)[0]["vector"]
        v = np.asarray(points[2]["vector"])
        np.testing.assert_allclose(with_vec, v / np.linalg.norm(v), atol=1e-5)  # cosine stores normalized
        self.assertEqual(store.get([]), [])

    def test_upsert_replaces_and_delete_by_ids_and_filter(self):
        store, points = self._loaded()
        store.upsert([{"id": "p000", "vector": points[0]["vector"], "payload": {"tier": "gone"}}])
        self.assertEqual(store.count(), self.N)
        self.assertEqual(store.get(["p000"])[0]["payload"], {"tier": "gone"})
        self.assertIsNone(store.get(["p000"])[0]["text"])
        self.assertEqual(store.delete(ids=["p000", "p001", "p001", "missing"]), 2)
        public = sum(1 for p in points[2:] if p["payload"]["tier"] == "public")
        self.assertEqual(store.delete(flt={"tier": "public"}), public)
        self.assertEqual(store.delete(flt={"url": {"$prefix": "https://b."}}), sum(
            1 for p in points[2:] if p["payload"]["tier"] != "public" and p["payload"]["url"].startswith("https://b.")
        ))
        self.assertEqual(store.delete(flt={"tier": {"$in": []}}), 0)
        self.assertEqual(store.count({"tier": "public"}), 0)
        with self.assertRaises(StoreError):
            store.delete()

    def test_validation_is_the_same_everywhere(self):
        store = self.make_store(4, "dot")
        with self.assertRaises(DimensionMismatch):
            store.upsert([{"id": "a", "vector": [1, 2, 3]}])
        with self.assertRaises(DimensionMismatch):
            store.search([1, 2, 3], k=1)
        for bad in (
            {"id": "", "vector": [1, 2, 3, 4]},
            {"id": "x" * 300, "vector": [1, 2, 3, 4]},
            {"id": "a", "vector": [float("nan"), 1, 1, 1]},
            {"id": "a", "vector": [1, 2, 3, 4], "payload": {"_pyrun_id": "spoof"}},
            {"id": "a", "vector": [1, 2, 3, 4], "payload": "not a dict"},
            {"id": "a", "vector": [1, 2, 3, 4], "text": 5},
            {"id": "a"},
        ):
            with self.assertRaises(StoreError, msg=bad):
                store.upsert([bad])
        with self.assertRaises(filters.UnsupportedFilter):
            store.count({"a.b": 1})
        self.assertEqual(store.count(), 0)

    def test_empty_collection_and_k_beyond_size(self):
        store = self.make_store(3, "cosine")
        self.assertEqual(store.search([1, 0, 0], k=5), [])
        self.assertEqual(store.scroll(limit=10), ([], None))
        store.upsert([{"id": "a", "vector": [1, 0, 0]}, {"id": "b", "vector": [0, 1, 0]}])
        hits = store.search([1, 0.1, 0], k=50)
        self.assertEqual([h["id"] for h in hits], ["a", "b"])
        self.assertEqual(hits[0]["payload"], {})
        self.assertIsNone(hits[0]["text"])


class LocalContractTests(_ContractCases, SimpleTestCase):
    def setUp(self):
        super().setUp()
        clear_cache()
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))

    def make_store(self, dim, metric):
        return LocalStore.create(self.dir / f"{uuid.uuid4().hex}.sqlite", dim, metric)


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class QdrantContractTests(_ContractCases, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.server = FakeQdrant()

    def make_store(self, dim, metric):
        return QdrantStore(QDRANT_URL, f"c_{uuid.uuid4().hex[:8]}", dim, metric, session=self.server).create()


@skipUnless(LIVE_QDRANT_URL, "set PYRUNNER_TEST_QDRANT_URL to run the contract against a real Qdrant")
@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class LiveQdrantContractTests(_ContractCases, SimpleTestCase):  # pragma: no cover — Hasan's live pass
    def make_store(self, dim, metric):
        store = QdrantStore(
            LIVE_QDRANT_URL,
            f"pyrun_contract_{uuid.uuid4().hex[:10]}",
            dim,
            metric,
            api_key=os.environ.get("PYRUNNER_TEST_QDRANT_API_KEY", ""),
        ).create()
        self.addCleanup(store.drop)
        return store


# =============================================================================
# Filter compiler
# =============================================================================

class QdrantFilterCompilerTests(SimpleTestCase):
    def test_exact_translations(self):
        cases = [
            ({"tier": "public"}, {"must": [{"key": "tier", "match": {"value": "public"}}]}),
            ({"flag": False}, {"must": [{"key": "flag", "match": {"value": False}}]}),
            ({"n": 3}, {"must": [{"key": "n", "range": {"gte": 3, "lte": 3}}]}),
            ({"n": {"$gt": 1, "$lte": 9}}, {"must": [
                {"key": "n", "range": {"gt": 1}}, {"key": "n", "range": {"lte": 9}},
            ]}),
            ({"t": {"$in": ["a", 2]}}, {"should": [
                {"key": "t", "match": {"value": "a"}}, {"key": "t", "range": {"gte": 2, "lte": 2}},
            ]}),
            ({"must_not": [{"tier": "x"}]}, {"must_not": [{"key": "tier", "match": {"value": "x"}}]}),
        ]
        for flt, expected in cases:
            compiled = filters.to_qdrant(flt)
            self.assertTrue(compiled.exact, flt)
            self.assertEqual(compiled.filter, expected, flt)
        self.assertEqual(filters.to_qdrant(None), filters.QdrantFilter(None, True))

    def test_relaxed_forms_flip_with_polarity(self):
        prefix = filters.to_qdrant({"u": {"$prefix": "ab"}})
        self.assertFalse(prefix.exact)
        self.assertEqual(prefix.filter, {"must": [{"key": "u", "match": {"text": "ab"}}]})
        not_prefix = filters.to_qdrant({"must_not": [{"u": {"$prefix": "ab"}}]})
        self.assertEqual(not_prefix.filter, {"must_not": [{"key": "u", "match": {"value": "ab"}}]})
        null = filters.to_qdrant({"o": None})
        self.assertEqual(null.filter, {"must": [{"is_empty": {"key": "o"}}]})
        not_null = filters.to_qdrant({"must_not": [{"o": None}]})
        self.assertEqual(not_null.filter, {"must_not": [{"is_null": {"key": "o"}}]})
        string_range = filters.to_qdrant({"s": {"$gt": "m"}, "tier": "a"})
        self.assertFalse(string_range.exact)
        self.assertEqual(string_range.filter, {"must": [{"key": "tier", "match": {"value": "a"}}]})
        # Under must_not a dropped condition must become "matches nothing", so the
        # negation stays a superset: no server filter at all.
        self.assertIsNone(filters.to_qdrant({"must_not": [{"s": {"$gt": "m"}}]}).filter)

    def test_matches_nothing_short_circuits(self):
        self.assertTrue(filters.to_qdrant({"t": {"$in": []}}).matches_nothing)
        self.assertTrue(filters.to_qdrant({"t": {"$in": []}, "u": {"$prefix": "x"}}).matches_nothing)
        self.assertFalse(filters.to_qdrant({"must_not": [{"t": {"$in": []}}]}).matches_nothing)

    def test_server_side_is_exact_or_a_superset_on_random_payloads(self):
        rng = np.random.default_rng(5)
        points = _contract_points(150, 4, rng)
        for flt in CONTRACT_FILTERS:
            compiled = filters.to_qdrant(flt)
            truth = {p["id"] for p in points if filters.matches(p["payload"], flt)}
            if compiled.matches_nothing:
                self.assertEqual(truth, set(), flt)
                continue
            server = {p["id"] for p in points if qdrant_filter_matches(p["id"], p["payload"], compiled.filter)}
            if compiled.exact:
                self.assertEqual(server, truth, flt)
            else:
                self.assertLessEqual(truth, server, flt)


# =============================================================================
# The adapter on the wire
# =============================================================================

@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class QdrantAdapterTests(SimpleTestCase):
    def setUp(self):
        self.server = FakeQdrant()
        sleep = mock.patch.object(qdrant_store.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def _store(self, name="docs", dim=3, metric="cosine", **kwargs):
        return QdrantStore(QDRANT_URL, name, dim, metric, session=self.server, **kwargs)

    def test_create_sends_size_distance_and_the_api_key(self):
        self._store(metric="euclid", api_key="k-123").create()
        method, path, _, body, headers = self.server.calls[-1]
        self.assertEqual((method, path), ("PUT", "/collections/docs"))
        self.assertEqual(body, {"vectors": {"size": 3, "distance": "Euclid"}})
        self.assertEqual(headers.get("api-key"), "k-123")

    def test_create_adopts_an_empty_twin_and_refuses_points_or_another_shape(self):
        self._store().create()
        self._store().create()  # empty, same shape: adopted
        self._store().upsert([{"id": "a", "vector": [1, 0, 0]}])
        with self.assertRaisesRegex(StoreError, "already has a collection"):
            self._store().create()
        with self.assertRaisesRegex(StoreError, "size=3"):
            self._store(dim=4).create()
        with mock.patch.object(self.server, "_collection", return_value=_ok(
            {"points_count": 0, "config": {"params": {"vectors": {"dense": {"size": 3, "distance": "Cosine"}}}}}
        )), self.assertRaisesRegex(StoreError, "named vectors"):
            self._store("named").create()

    def test_ids_are_uuid5_and_the_bookkeeping_keys_never_leak(self):
        store = self._store().create()
        store.upsert([{"id": "doc/1", "vector": [1, 0, 0], "payload": {"k": 1}, "text": "hi"}])
        method, path, params, body, _ = self.server.calls[-1]
        self.assertEqual((method, path, params), ("PUT", "/collections/docs/points", {"wait": "true"}))
        stored = body["points"][0]
        self.assertEqual(stored["id"], str(uuid.uuid5(POINT_NAMESPACE, "doc/1")))
        self.assertEqual(stored["id"], point_uuid("doc/1"))
        self.assertEqual(stored["payload"], {"k": 1, "_pyrun_id": "doc/1", "_pyrun_text": "hi"})
        hit = store.search([1, 0, 0], k=1)[0]
        self.assertEqual(hit, {"id": "doc/1", "score": mock.ANY, "payload": {"k": 1}, "text": "hi"})
        self.assertEqual(store.get(["doc/1"])[0]["payload"], {"k": 1})

    def test_euclid_distance_is_negated(self):
        store = self._store(metric="euclid").create()
        store.upsert([{"id": "near", "vector": [1, 0, 0]}, {"id": "far", "vector": [5, 5, 5]}])
        hits = store.search([1, 0, 0], k=2)
        self.assertEqual([h["id"] for h in hits], ["near", "far"])
        self.assertAlmostEqual(hits[0]["score"], 0.0)
        self.assertLess(hits[1]["score"], 0)

    def test_retries_5xx_and_429_then_gives_up(self):
        store = self._store().create()
        self.server.fail_next = [503, 429]
        self.assertEqual(store.count(), 0)
        self.server.fail_next = [500, 500, 500, 500]
        with self.assertRaisesRegex(BackendUnavailable, "HTTP 500"):
            store.count()
        self.assertEqual(qdrant_store.time.sleep.call_count, 5)

    def test_connection_errors_never_carry_the_url(self):
        import requests

        store = self._store().create()
        boom = requests.ConnectionError("HTTPConnectionPool(host='qdrant.internal', port=6333): refused")
        self.server.fail_next = [boom] * 4
        with self.assertRaises(BackendUnavailable) as ctx:
            store.count()
        self.assertNotIn("qdrant.internal", str(ctx.exception))
        self.assertNotIn("localhost", str(ctx.exception))

    def test_error_mapping(self):
        store = self._store()
        with self.assertRaisesRegex(BackendUnavailable, "does not exist on the server"):
            store.count()
        store.create()
        self.server.fail_next = [401]
        with self.assertRaisesRegex(BackendUnavailable, "API key"):
            store.count()
        self.server.fail_next = [400]
        with self.assertRaisesRegex(QdrantRejected, "injected failure"):
            store.count()
        store.drop()
        store.drop()  # already gone: fine
        self.assertNotIn("docs", self.server.collections)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_netguard_applies(self):
        with self.assertRaisesRegex(BackendUnavailable, "PYRUNNER_ALLOW_PRIVATE_NETWORK"):
            self._store()
        with self.assertRaisesRegex(BackendUnavailable, "no URL"):
            QdrantStore("", "x", 3, "cosine", session=self.server)

    def test_relaxed_search_pages_until_k_exact_hits(self):
        store = self._store(dim=2).create()
        # 300 points whose url merely CONTAINS "/a"; only the last 3 START with it.
        points = [{"id": f"x{i:03d}", "vector": [1.0, i / 300], "payload": {"u": f"z/a{i}"}} for i in range(300)]
        points += [{"id": f"a{i}", "vector": [0.0, 1.0], "payload": {"u": f"/a{i}"}} for i in range(3)]
        store.upsert(points)
        hits = store.search([1.0, 0.0], k=2, flt={"u": {"$prefix": "/a"}})
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h["id"].startswith("a") for h in hits))
        queries = [c for c in self.server.calls if c[1].endswith("/points/query")]
        self.assertGreater(len(queries), 1, "the superset was paged through")


# =============================================================================
# VectorService + console on a Qdrant connection
# =============================================================================

class _QdrantServer:
    """Patch every QdrantStore built by the service onto one FakeQdrant."""

    def setUp(self):
        super().setUp()
        self.server = FakeQdrant()
        patcher = mock.patch.object(qdrant_store, "make_session", return_value=self.server)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _qdrant(self, name="Qdrant", prefix="", **config):
        connection = VectorConnection(
            provider_type=VectorConnection.ProviderType.QDRANT,
            name=name,
            config={"url": QDRANT_URL, "collection_prefix": prefix, **config},
        )
        connection.set_credentials({"api_key": "qk-secret"})
        connection.save()
        return connection


@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "svc", PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class QdrantServiceTests(_QdrantServer, TestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace.get_default()
        self.provider = _provider()
        self.conn = self._qdrant(prefix="team-")

    def _create(self, name="docs", **kwargs):
        kwargs.setdefault("embedding_provider", self.provider)
        return VectorService.create_collection(workspace=self.ws, name=name, connection=self.conn, **kwargs)

    def test_create_names_the_server_collection_and_works_end_to_end(self):
        col = self._create()
        self.assertEqual(col.backend_ref, f"team-pyrun_{self.ws.id.hex[:8]}_docs")
        self.assertIsNone(col.file_path)
        self.assertIn(col.backend_ref, self.server.collections)
        self.assertEqual(self.server.collections[col.backend_ref]["size"], 8)
        with fake_embeddings():
            handle = VectorService.handle(col)
            handle.add_texts([{"id": "p1", "text": "alpha"}, {"id": "p2", "text": "beta"}])
            self.assertEqual(handle.search("alpha", k=1)[0]["id"], "p1")
        col.refresh_from_db()
        self.assertEqual(col.points_count, 2)
        self.assertEqual(VectorService.size_bytes(col), 0)
        VectorService.delete_collection(col)
        self.assertNotIn(col.backend_ref, self.server.collections)

    def test_owned_names_and_ref_clashes(self):
        owned = self._create(name="demo:idx", owner_plugin="demo", owner_key="idx", embedding_provider=None, dimension=3)
        self.assertTrue(owned.backend_ref.endswith("_demo__idx"))
        clash = self._create(name="demo__idx", embedding_provider=None, dimension=3)
        self.assertEqual(clash.backend_ref, f"{owned.backend_ref}_{clash.id.hex[:8]}")
        with self.assertRaisesRegex(VectorError, "plugin collection"):
            self._create(name="other:idx", owner_plugin="demo", dimension=3, embedding_provider=None)
        with self.assertRaisesRegex(VectorError, "lowercase"):
            self._create(name="demo:idx2", dimension=3, embedding_provider=None)  # unowned: no colon

    def test_a_leftover_with_points_is_refused_and_retry_recreates_a_lost_one(self):
        ref = VectorCollection.qdrant_backend_ref("team-", self.ws.id, "docs")
        self.server.collections[ref] = {"size": 8, "distance": "Cosine", "points": {point_uuid("x"): {
            "vector": np.ones(8) / np.sqrt(8), "payload": {"_pyrun_id": "x"}}}}
        with self.assertRaisesRegex(VectorError, "already has a collection"):
            self._create()
        col = VectorCollection.objects.get(name="docs")
        self.assertEqual(col.status, VectorCollection.STATUS_ERROR)
        del self.server.collections[ref]
        VectorService.retry_backend(col)
        self.assertTrue(col.is_ready)
        self.assertIn(ref, self.server.collections)

    def test_disabled_connection_and_unreachable_server(self):
        col = self._create()
        self.conn.enabled = False
        self.conn.save()
        col.refresh_from_db()
        with self.assertRaises(VectorError) as ctx:
            VectorService.handle(col)
        self.assertEqual(ctx.exception.code, "backend_unavailable")
        self.conn.enabled = True
        self.conn.save()
        col.refresh_from_db()
        del self.server.collections[col.backend_ref]
        with self.assertRaises(VectorError) as ctx:
            VectorService.handle(col).count()
        self.assertEqual(ctx.exception.code, "backend_unavailable")

    def test_credentials_gate(self):
        local = VectorService.create_collection(workspace=self.ws, name="loc", dimension=3)
        with self.assertRaises(VectorError) as ctx:
            VectorService.credentials_for(local)
        self.assertEqual(ctx.exception.code, "unsupported")
        col = self._create()
        with self.assertRaises(VectorError) as ctx:
            VectorService.credentials_for(col)
        self.assertEqual(ctx.exception.code, "credentials_not_allowed")
        self.conn.config["expose_credentials"] = True
        self.conn.save()
        col.refresh_from_db()
        creds = VectorService.credentials_for(col)
        self.assertEqual(creds["url"], QDRANT_URL)
        self.assertEqual(creds["api_key"], "qk-secret")
        self.assertEqual(creds["collection"], col.backend_ref)
        self.assertEqual(creds["point_id_namespace"], str(POINT_NAMESPACE))

    def test_local_connection_never_exposes(self):
        local = VectorConnection.objects.get(name="Local")
        local.config = {"expose_credentials": True}
        self.assertFalse(local.exposes_credentials)


@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "console", PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class QdrantConsoleTests(_QdrantServer, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True, is_staff=True)
        self.ws = Workspace.get_default()
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.admin)
        self.conn = self._qdrant()

    def test_create_on_qdrant_and_the_detail_page(self):
        resp = self.client.post(
            reverse("cpanel:vector_create"),
            {"name": "kb", "connection": self.conn.pk, "dimension": 4, "metric": "dot", "include_in_backup": "on"},
        )
        col = VectorCollection.objects.get(name="kb")
        self.assertRedirects(resp, reverse("cpanel:vector_detail", kwargs={"pk": col.pk}), fetch_redirect_response=False)
        page = self.client.get(reverse("cpanel:vector_detail", kwargs={"pk": col.pk})).content.decode()
        self.assertIn(col.backend_ref, page)
        self.assertIn("on the server", page)
        self.assertIn("Settings only", page)
        self.assertNotIn(QDRANT_URL, page)
        listing = self.client.get(reverse("cpanel:vector_list")).content.decode()
        self.assertIn("on the server", listing)

    def test_services_form_saves_the_credentials_flag_and_guards_the_backend(self):
        resp = self.client.post(
            reverse("cpanel:vector_connection_save"),
            {"connection_id": self.conn.pk, "provider_type": "qdrant", "name": "Qdrant", "url": QDRANT_URL,
             "enabled": "on", "expose_credentials": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.exposes_credentials)
        self.assertEqual(self.conn.get_credentials(), {"api_key": "qk-secret"})  # blank kept it
        page = self.client.get(reverse("cpanel:services")).content.decode()
        self.assertIn("Key shared with runs", page)
        self.assertNotIn("qk-secret", page)

        VectorService.create_collection(workspace=self.ws, name="kb", connection=self.conn, dimension=3)
        self.client.post(
            reverse("cpanel:vector_connection_save"),
            {"connection_id": self.conn.pk, "provider_type": "local", "name": "Qdrant", "enabled": "on"},
        )
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.provider_type, "qdrant", "a backend holding collections can't change")


# =============================================================================
# Loopback door
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "loop", ALLOWED_HOSTS=["*"])
class LoopbackTests(TestCase):
    def setUp(self):
        clear_cache()
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="loop-env", path="/tmp/env")
        self.provider = _provider()
        self.user_run = self._run()
        self.plugin_run = self._run(owner="demo")
        self.other_plugin_run = self._run(owner="rival")
        self.docs = VectorService.create_collection(
            workspace=self.ws, name="docs", embedding_provider=self.provider
        )
        self.owned = VectorService.create_collection(
            workspace=self.ws, name="demo:kb", owner_plugin="demo", owner_key="kb", dimension=3
        )

    def _run(self, owner=None, workspace=None):
        script = Script.objects.create(
            name=f"s-{uuid.uuid4().hex[:6]}", code="print(1)", environment=self.env,
            owner_plugin=owner, workspace=workspace or self.ws,
        )
        return Run.objects.create(script=script, workspace=workspace or self.ws)

    def _call(self, name, op, payload=None, run=None, remote="127.0.0.1", token=None):
        token = token if token is not None else mint_datastore_token((run or self.user_run).id)
        return self.client.post(
            reverse("internal:vectors_op", kwargs={"name": name, "op": op}),
            data=json.dumps(payload if payload is not None else {}),
            content_type="application/json",
            REMOTE_ADDR=remote,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def _code(self, response):
        return response.json()["error"]["code"]

    def test_auth_is_the_loopback_token(self):
        self.assertEqual(self._call("docs", "info", token="nope").status_code, 401)
        self.assertEqual(self._call("docs", "info", remote="10.0.0.9").status_code, 403)
        self.assertEqual(self._call("docs", "frobnicate").status_code, 404)

    def test_access_rule(self):
        self.assertEqual(self._call("docs", "info").status_code, 200)
        self.assertEqual(self._call("docs", "info", run=self.plugin_run).status_code, 200)
        self.assertEqual(self._call("demo:kb", "info", run=self.plugin_run).status_code, 200)
        for run in (self.user_run, self.other_plugin_run):
            # Someone else's collection answers exactly like a missing one.
            hidden = self._call("demo:kb", "info", run=run)
            missing = self._call("nope", "info", run=run)
            self.assertEqual((hidden.status_code, missing.status_code), (404, 404))
            self.assertEqual(
                hidden.json()["error"]["message"].replace("demo:kb", "X"),
                missing.json()["error"]["message"].replace("nope", "X"),
            )
        other_ws = Workspace.objects.create(name="Other")
        stranger = self._run(workspace=other_ws)
        self.assertEqual(self._call("docs", "info", run=stranger).status_code, 404)
        theirs = VectorService.create_collection(workspace=other_ws, name="docs", dimension=5)
        info = self._call("docs", "info", run=stranger).json()["collection"]
        self.assertEqual(info["dimension"], theirs.dimension)

    def test_every_op_round_trips(self):
        with fake_embeddings():
            added = self._call("docs", "add_texts", {"items": [
                {"id": "a", "text": "alpha", "payload": {"tier": "public"}},
                {"id": "b", "text": "beta", "payload": {"tier": "private"}},
            ]}).json()
            self.assertEqual(added["added"], 2)
            hits = self._call("docs", "search", {"query": "alpha", "k": 1}).json()["results"]
            self.assertEqual(hits[0]["id"], "a")
            vectors = self._call("docs", "embed", {"texts": ["alpha"], "input_type": "query"}).json()["vectors"]
            self.assertEqual(len(vectors[0]), 8)
        self.assertEqual(
            self._call("docs", "search_vector", {"vector": _vec("beta"), "k": 1, "filter": {"tier": "private"}})
            .json()["results"][0]["id"], "b",
        )
        self.assertEqual(self._call("docs", "count", {"filter": {"tier": "public"}}).json()["count"], 1)
        got = self._call("docs", "get", {"ids": ["b", "zz"], "include_vectors": True}).json()["points"]
        self.assertEqual([p["id"] for p in got], ["b"])
        self.assertEqual(len(got[0]["vector"]), 8)
        page = self._call("docs", "scroll", {"limit": 1}).json()
        self.assertEqual(len(page["points"]), 1)
        rest = self._call("docs", "scroll", {"cursor": page["next_cursor"]}).json()
        self.assertIsNone(rest["next_cursor"])
        self.assertEqual(self._call("docs", "delete", {"ids": ["a"]}).json()["deleted"], 1)
        self.assertEqual(self._call("docs", "delete", {"filter": {"tier": "private"}}).json()["deleted"], 1)
        self.docs.refresh_from_db()
        self.assertEqual(self.docs.points_count, 0)
        self.assertGreater(self.docs.embed_calls, 0)

    def test_errors_map_to_statuses(self):
        cases = [
            (("docs", "count", {"filter": {"a.b": 1}}), 400, "unsupported_filter"),
            (("demo:kb", "add_vectors", {"items": [{"id": "a", "vector": [1, 2]}]}, self.plugin_run), 400, "dimension_mismatch"),
            (("docs", "add_texts", {"items": "nope"}), 400, "invalid"),
            (("docs", "get", {"ids": ["x"] * 1001}), 400, "invalid"),
            (("docs", "delete", {"ids": ["a"], "filter": {"t": 1}}), 400, "invalid"),
            (("demo:kb", "search", {"query": "x"}, self.plugin_run), 400, "invalid"),
            (("docs", "credentials", {}), 400, "unsupported"),
        ]
        for args, status, code in cases:
            resp = self._call(*args)
            self.assertEqual((resp.status_code, self._code(resp)), (status, code), args)
        bad = self.client.post(
            reverse("internal:vectors_op", kwargs={"name": "docs", "op": "count"}), data="{nope",
            content_type="application/json", REMOTE_ADDR="127.0.0.1",
            HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token(self.user_run.id)}",
        )
        self.assertEqual(bad.status_code, 400)

        self.provider.model = "text-embedding-3-large"
        self.provider.save()
        resp = self._call("docs", "add_texts", {"items": [{"id": "a", "text": "x"}]})
        self.assertEqual((resp.status_code, self._code(resp)), (409, "model_mismatch"))
        self.provider.model = "text-embedding-3-small"
        self.provider.save()

        with mock.patch.object(EmbeddingClient, "_embed_batch", side_effect=EmbeddingError("provider down")):
            resp = self._call("docs", "search", {"query": "x"})
        self.assertEqual((resp.status_code, self._code(resp)), (502, "embedding_failed"))

        with mock.patch.object(local_store, "HARD_LIMIT_POINTS", 1):
            resp = self._call("demo:kb", "add_vectors", {"items": [
                {"id": "a", "vector": [1, 0, 0]}, {"id": "b", "vector": [0, 1, 0]}]}, run=self.plugin_run)
        self.assertEqual((resp.status_code, self._code(resp)), (413, "collection_full"))

        self.docs.status = VectorCollection.STATUS_ERROR
        self.docs.save()
        resp = self._call("docs", "count")
        self.assertEqual((resp.status_code, self._code(resp)), (409, "not_ready"))

        from core.views.api import vectors_internal as vi

        self.docs.status = VectorCollection.STATUS_READY
        self.docs.save()
        boom = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.dict(vi._OPS, {"count": boom}), self.assertLogs(
            "core.views.api.vectors_internal", level="ERROR"
        ):
            resp = self._call("docs", "count")
        self.assertEqual((resp.status_code, self._code(resp)), (500, "error"))
        self.assertNotIn("boom", resp.json()["error"]["message"])

    def test_points_brake_refuses_without_charging_and_is_per_run(self):
        from core.views.api import vectors_internal as vi

        items = [{"id": f"p{i}", "vector": [1, 0, 0]} for i in range(4)]
        with mock.patch.object(vi, "RUN_POINTS", 5):
            self.assertEqual(self._call("demo:kb", "add_vectors", {"items": items}, run=self.plugin_run).status_code, 200)
            resp = self._call("demo:kb", "add_vectors", {"items": items[:2]}, run=self.plugin_run)
            self.assertEqual((resp.status_code, self._code(resp)), (429, "run_budget_exceeded"))
            self.assertIn("5", resp.json()["error"]["message"])
            # The refusal charged nothing: one more point still fits.
            self.assertEqual(
                self._call("demo:kb", "add_vectors", {"items": items[:1]}, run=self.plugin_run).status_code, 200
            )
            # Deletes are points touched too, charged by what was removed.
            resp = self._call("demo:kb", "delete", {"filter": {"x": None}}, run=self.plugin_run)
            self.assertEqual((resp.status_code, self._code(resp)), (429, "run_budget_exceeded"))
            # Another run of the same script has its own budget.
            fresh = Run.objects.create(script=self.plugin_run.script, workspace=self.ws)
            self.assertEqual(self._call("demo:kb", "delete", {"filter": {"x": None}}, run=fresh).json()["deleted"], 4)

    def test_embedding_brakes(self):
        from core.views.api import vectors_internal as vi

        with fake_embeddings():
            with mock.patch.object(vi, "RUN_EMBED_CALLS", 2):  # max_batch=4 ⇒ 5 texts = 2 calls
                items = [{"id": f"t{i}", "text": f"text {i}"} for i in range(5)]
                self.assertEqual(self._call("docs", "add_texts", {"items": items}).status_code, 200)
                resp = self._call("docs", "search", {"query": "one more"})
                self.assertEqual(self._code(resp), "run_budget_exceeded")
            with mock.patch.object(vi, "RUN_TEXT_BYTES", 10):
                run = self._run()
                resp = self._call("docs", "embed", {"texts": ["x" * 11]}, run=run)
                self.assertEqual(self._code(resp), "run_budget_exceeded")
                self.assertEqual(self._call("docs", "embed", {"texts": ["x" * 10]}, run=run).status_code, 200)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_credentials_on_qdrant(self):
        server = FakeQdrant()
        with mock.patch.object(qdrant_store, "make_session", return_value=server):
            conn = VectorConnection(provider_type="qdrant", name="Q", config={"url": QDRANT_URL})
            conn.set_credentials({"api_key": "qk"})
            conn.save()
            VectorService.create_collection(workspace=self.ws, name="remote", connection=conn, dimension=3)
            resp = self._call("remote", "credentials")
            self.assertEqual((resp.status_code, self._code(resp)), (403, "credentials_not_allowed"))
            conn.config = {**conn.config, "expose_credentials": True}
            conn.save()
            creds = self._call("remote", "credentials").json()["credentials"]
            self.assertEqual((creds["url"], creds["api_key"]), (QDRANT_URL, "qk"))
            self.assertEqual(self._call("remote", "add_vectors", {"items": [{"id": "a", "vector": [1, 0, 0]}]}).status_code, 200)
            self.assertEqual(self._call("remote", "count").json()["count"], 1)


# =============================================================================
# pyrunner_vectors, end to end through the real loopback view
# =============================================================================

class _Body:
    def __init__(self, content):
        self._content = content

    def read(self):
        return self._content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "helper", ALLOWED_HOSTS=["*"])
class HelperTests(TestCase):
    def setUp(self):
        from core.script_helpers import pyrunner_vectors

        clear_cache()
        self.pv = pyrunner_vectors
        self.ws = Workspace.get_default()
        env = Environment.objects.create(name="helper-env", path="/tmp/env")
        script = Script.objects.create(name="h", code="", environment=env, workspace=self.ws)
        self.run = Run.objects.create(script=script, workspace=self.ws)
        self.provider = _provider()
        VectorService.create_collection(workspace=self.ws, name="docs", embedding_provider=self.provider)
        self.calls = []
        env_patch = mock.patch.dict(os.environ, {
            "PYRUNNER_INTERNAL_URL": "http://127.0.0.1:8000",
            "PYRUNNER_INTERNAL_TOKEN": mint_datastore_token(self.run.id),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        route = mock.patch.object(self.pv.urllib.request, "urlopen", side_effect=self._route)
        route.start()
        self.addCleanup(route.stop)

    def _route(self, req, timeout=None):
        path = urlparse(req.full_url).path
        self.calls.append((path, json.loads(req.data)))
        resp = self.client.post(
            path, data=req.data, content_type="application/json", REMOTE_ADDR="127.0.0.1",
            HTTP_AUTHORIZATION=req.get_header("Authorization"),
        )
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, "error", {}, io.BytesIO(resp.content))
        return _Body(resp.content)

    def test_the_whole_surface(self):
        col = self.pv.get_collection("docs")
        self.assertEqual((col.dimension, col.metric, col.has_embeddings, col.backend), (8, "cosine", True, "local"))
        with fake_embeddings(), mock.patch.object(self.pv, "TEXT_BATCH", 2):
            result = col.add_texts([{"id": f"p{i}", "text": f"doc {i}", "payload": {"i": i}} for i in range(5)])
            self.assertEqual(result["added"], 5)
            self.assertGreater(result["tokens"], 0)
            self.assertEqual(sum(1 for path, _ in self.calls if path.endswith("/add_texts")), 3)
            self.assertEqual(col.search("doc 3", k=1)[0]["id"], "p3")
            self.assertEqual(len(col.embed(["a", "b"])), 2)
        self.assertEqual(col.search_vector(_vec("doc 1"), k=1, filter={"i": {"$lte": 1}})[0]["id"], "p1")
        self.assertEqual(col.add_vectors([{"id": "v", "vector": _vec("v")}]), {"added": 1})
        self.assertEqual(col.count(), 6)
        self.assertEqual([p["id"] for p in col.get(["p2", "nope"])], ["p2"])
        seen, cursor = [], None
        while True:
            items, cursor = col.scroll(cursor=cursor, limit=4)
            seen += [i["id"] for i in items]
            if cursor is None:
                break
        self.assertEqual(len(seen), 6)
        self.assertEqual(col.delete(ids=["p0", "p1"]), {"deleted": 2})
        self.assertEqual(col.delete(filter={"i": {"$gte": 3}}), {"deleted": 2})
        with self.assertRaises(self.pv.VectorError):
            col.delete()

    def test_errors_carry_codes(self):
        with self.assertRaises(self.pv.CollectionNotFound) as ctx:
            self.pv.get_collection("nope")
        self.assertIsInstance(ctx.exception, LookupError)
        col = self.pv.get_collection("docs")
        with self.assertRaises(self.pv.VectorError) as ctx:
            col.count(filter={"a.b": 1})
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("unsupported_filter", 400))
        with self.assertRaises(self.pv.VectorError) as ctx:
            col.credentials()
        self.assertEqual(ctx.exception.code, "unsupported")
        with mock.patch.dict(os.environ, {"PYRUNNER_INTERNAL_TOKEN": ""}):
            with self.assertRaisesRegex(self.pv.VectorError, "not available"):
                self.pv.get_collection("docs")

    def test_helper_is_stdlib_only(self):
        import ast
        import inspect
        import sys

        tree = ast.parse(inspect.getsource(self.pv))
        roots = {
            (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names))


# =============================================================================
# VectorAPI (SDK 2.6)
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "sdk")
class VectorAPITests(TestCase):
    def setUp(self):
        clear_cache()
        self.provider = _provider(name="Emb")

    def test_version_and_exports(self):
        from core.plugins import api as sdk

        self.assertEqual(sdk.API_VERSION, "2.6")
        self.assertIs(sdk.VectorError, VectorError)

    def test_provision_is_idempotent_and_pins_the_shape(self):
        from core.plugins.api import VectorAPI

        api = VectorAPI("demo")
        docs = api.provision("docs", embedding_provider="Emb", description="Help pages")
        row = docs.model
        self.assertEqual((row.name, row.owner_plugin, row.owner_key), ("demo:docs", "demo", "docs"))
        self.assertEqual((docs.dimension, docs.metric, docs.embedding_model), (8, "cosine", "text-embedding-3-small"))
        again = api.provision("docs", embedding_provider=self.provider)
        self.assertEqual(again.model.pk, row.pk)
        self.assertEqual(again.model.description, "Help pages")  # None keeps it
        api.provision("docs", embedding_provider="Emb", description="New text")
        row.refresh_from_db()
        self.assertEqual(row.description, "New text")
        for kwargs, what in (
            ({"embedding_provider": None, "dimension": 8}, "embedding provider"),
            ({"embedding_provider": "Emb", "metric": "dot"}, "metric"),
            ({"embedding_provider": "Emb", "dimension": 16}, "dimension"),
        ):
            with self.assertRaisesRegex(VectorError, what):
                api.provision("docs", **kwargs)
        self.provider.model = "text-embedding-3-large"
        self.provider.save()
        with self.assertRaisesRegex(VectorError, "embedding model"):
            api.provision("docs", embedding_provider="Emb")
        with self.assertRaisesRegex(VectorError, "No embedding provider named"):
            api.provision("other", embedding_provider="Missing")
        with self.assertRaisesRegex(VectorError, "plugin collection"):
            api.provision("Bad Key", dimension=3)
        self.assertEqual(VectorCollection.objects.count(), 1)

    def test_handle_ops_get_list_delete_and_scoping(self):
        from core.plugins.api import VectorAPI

        raw = VectorAPI("demo").provision("raw", dimension=3, metric="dot")
        self.assertEqual(raw.add_vectors([{"id": "a", "vector": [1, 0, 0], "payload": {"k": 1}}]), {"added": 1})
        self.assertEqual(raw.search_vector([1, 0, 0], k=1)[0]["id"], "a")
        self.assertEqual(raw.count(filter={"k": 1}), 1)
        self.assertEqual(raw.scroll(limit=5)[1], None)
        with fake_embeddings():
            docs = VectorAPI("demo").provision("docs", embedding_provider="Emb")
            docs.add_texts([{"id": "p", "text": "hello"}])
            self.assertEqual(docs.search("hello", k=1)[0]["id"], "p")
        self.assertEqual({h.name for h in VectorAPI("demo").list()}, {"demo:raw", "demo:docs"})
        self.assertEqual(VectorAPI("rival").list(), [])
        self.assertIsNone(VectorAPI("rival").get("raw"))
        mine = VectorAPI(None).provision("mine", dimension=3)
        self.assertIsNone(mine.model.owner_plugin)
        self.assertEqual([h.name for h in VectorAPI(None).list()], ["mine"])
        # The unowned lane re-provisions a console-made collection of the same name…
        VectorService.create_collection(workspace=Workspace.get_default(), name="taken", dimension=3)
        self.assertEqual(VectorAPI(None).provision("taken", dimension=3).name, "taken")
        # …but a plugin never adopts a row it doesn't own, even under its own name.
        VectorAPI("demo").provision("x", dimension=3)
        VectorCollection.objects.filter(name="demo:x").update(owner_plugin=None)
        with self.assertRaisesRegex(VectorError, "not owned by this plugin"):
            VectorAPI("demo").provision("x", dimension=3)
        other_ws = Workspace.objects.create(name="Other")
        self.assertEqual(VectorAPI("demo", workspace=other_ws).list(), [])
        self.assertTrue(VectorAPI("demo").delete("raw"))
        self.assertFalse(VectorAPI("demo").delete("raw"))
        self.assertTrue(VectorAPI("demo").is_available())

    def test_reprovision_heals_an_error_row(self):
        from core.plugins.api import VectorAPI

        handle = VectorAPI("demo").provision("raw", dimension=3)
        handle.model.file_path.unlink()
        VectorCollection.objects.filter(pk=handle.model.pk).update(status="error", last_error="lost")
        with self.assertRaises(VectorError) as ctx:
            VectorAPI("demo").get("raw").count()
        self.assertEqual(ctx.exception.code, "not_ready")
        healed = VectorAPI("demo").provision("raw", dimension=3)
        self.assertTrue(healed.model.is_ready)
        self.assertEqual(healed.count(), 0)
