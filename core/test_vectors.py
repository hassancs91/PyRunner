"""
Vector seam Stage 1 — embedding providers, vector connections, local collections.

Covered without any network or real provider (the embedding client is mocked at
its HTTP seam):

- filter grammar: the SQLite compiler and the Python evaluator agree on random
  payloads; unsupported shapes are rejected by name
- local store: exact top-k against a numpy reference for every metric, with and
  without filters; dimension mismatch is a clean error; the hard cap; cache
  reload on version bump and the LRU budget; export/import image round trip
- embedding client: batching by max_batch, usage from the provider vs the
  len//4 estimate, truncation, the Voyage/Jina query hint, retry on 429, errors
- models: the seeded Local connection, the single-default rule, credentials
- VectorService: create (file + pinned model/dimension), add_texts counts usage,
  model-pin refusal, search, delete removes the file, retry after a lost file
- console: owner/member/outsider access, create → detail, search, backup
  toggle, typed-name delete, plugin-owned delete guard
- Services page: both cards render without leaking keys; provider save/test
  (dimension recorded); connection save honours the SSRF flag; PROTECT on delete
- backup 1.9.0: rows + Local image round-trip, the size cap, the opt-out,
  upsert-by-name for providers/connections
- plugin delete cleans owned collections (row + file); the system check
"""

import base64
import json
import sqlite3
import tempfile
import zlib
from pathlib import Path
from unittest import mock

import numpy as np
from cryptography.fernet import Fernet
from django.core.checks import Error
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import (
    EMBEDDING_PRESETS,
    EmbeddingProvider,
    Environment,
    GlobalSettings,

    User,
    VectorCollection,
    VectorConnection,
    Workspace,
    WorkspaceMembership,
)
from core.services import VectorError, VectorService
from core.services.backup_service import BackupService
from core.vectors import filters, local_store
from core.vectors.embeddings import MAX_TEXT_CHARS, EmbeddingClient, EmbeddingError
from core.vectors.local_store import DimensionMismatch, LocalStore, LocalStoreFull, clear_cache

_TEST_KEY = Fernet.generate_key().decode()
_TMP = Path(tempfile.mkdtemp(prefix="pyrun-vectors-"))

def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)

def _vec(text, dim=8):
    """Deterministic unit vector per text (stable across processes)."""
    rng = np.random.default_rng(zlib.crc32(text.encode("utf-8")))
    v = rng.normal(size=dim)
    return (v / np.linalg.norm(v)).tolist()

def _fake_embed_batch(self, batch, input_type):
    return [_vec(t) for t in batch], sum(max(1, len(t) // 4) for t in batch)

def fake_embeddings():
    """Patch the client's HTTP seam so embed() works offline and deterministically."""
    return mock.patch.object(EmbeddingClient, "_embed_batch", _fake_embed_batch)

def _provider(name="Emb", dimension=8, model="text-embedding-3-small", **extra):
    p = EmbeddingProvider(
        provider_type=EmbeddingProvider.ProviderType.OPENAI,
        name=name,
        base_url="https://api.openai.com",
        model=model,
        max_batch=4,
        dimension=dimension,
        **extra,
    )
    p.set_credential("sk-test-key")
    p.save()
    return p

class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason = "reason"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

# =============================================================================
# Filter grammar
# =============================================================================

class FilterGrammarTests(SimpleTestCase):
    def test_valid_shapes_pass(self):
        for flt in (
            {"tier": "public"},
            {"n": {"$gte": 3, "$lt": 10}},
            {"tag": {"$in": ["a", "b", None]}},
            {"url": {"$prefix": "https://"}},
            {"must": [{"tier": "public"}], "must_not": [{"kind": "draft"}]},
            {"flag": True},
            {"gone": None},
        ):
            self.assertIs(filters.validate(flt), flt)
        self.assertIsNone(filters.validate(None))
        self.assertIsNone(filters.validate({}))

    def test_unsupported_shapes_are_named(self):
        cases = {
            "nested path": {"a.b": 1},
            "regex op": {"a": {"$regex": "x"}},
            "list equality": {"a": [1, 2]},
            "non-object": ["a"],
            "empty condition": {"a": {}},
            "bad $in": {"a": {"$in": "x"}},
            "bad range": {"a": {"$gt": [1]}},
            "bad must": {"must": {"a": 1}},
            "empty must entry": {"must": [{}]},
        }
        for label, flt in cases.items():
            with self.assertRaises(filters.UnsupportedFilter, msg=label):
                filters.validate(flt)

    def test_sqlite_and_python_evaluators_agree(self):
        rng = np.random.default_rng(1)
        tiers, kinds = ["public", "agents", "private"], ["note", "take", "draft"]
        payloads = []
        for i in range(120):
            p = {
                "tier": tiers[int(rng.integers(3))],
                "kind": kinds[int(rng.integers(3))],
                "n": int(rng.integers(0, 50)),
                "url": f"https://{'a' if rng.random() < .5 else 'b'}.example/{i}",
                "flag": bool(rng.random() < 0.5),
            }
            if rng.random() < 0.2:
                p["opt"] = None
            if rng.random() < 0.3:
                del p["flag"]
            payloads.append(p)
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE points (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.executemany(
            "INSERT INTO points (id, payload) VALUES (?, ?)",
            [(i, json.dumps(p)) for i, p in enumerate(payloads)],
        )
        cases = [
            None,
            {"tier": "public"},
            {"n": {"$gte": 10, "$lt": 30}},
            {"kind": {"$in": ["note", "take"]}},
            {"url": {"$prefix": "https://a."}},
            {"flag": True},
            {"flag": False},
            {"opt": None},
            {"must": [{"tier": "public"}, {"n": {"$gt": 5}}], "must_not": [{"kind": "draft"}]},
            {"tier": {"$in": ["public", "agents"]}, "kind": "note"},
            {"must_not": [{"url": {"$prefix": "https://b."}}, {"n": {"$lte": 3}}]},
            {"n": {"$in": [1, 2, 3, 4, 5]}},
            {"tier": {"$in": []}},
        ]
        for flt in cases:
            where, params = filters.to_sqlite(flt)
            sql_ids = {r[0] for r in conn.execute(f"SELECT id FROM points WHERE {where}", params)}
            py_ids = {i for i, p in enumerate(payloads) if filters.matches(p, flt)}
            self.assertEqual(sql_ids, py_ids, flt)

    def test_prefix_escapes_like_wildcards(self):
        where, params = filters.to_sqlite({"u": {"$prefix": "50%_off\\"}})
        self.assertIn("LIKE ? ESCAPE", where)
        self.assertEqual(params, ["50\\%\\_off\\\\%"])
        self.assertTrue(filters.matches({"u": "50%_off\\x"}, {"u": {"$prefix": "50%_off\\"}}))
        self.assertFalse(filters.matches({"u": "50Xoff"}, {"u": {"$prefix": "50%_off\\"}}))

# =============================================================================
# Local store
# =============================================================================

class LocalStoreTests(SimpleTestCase):
    def setUp(self):
        clear_cache()
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))

    def _store(self, dim=16, metric="cosine", name="s"):
        return LocalStore.create(self.dir / f"{name}.sqlite", dim, metric)

    @staticmethod
    def _points(n, dim, rng):
        return [
            {
                "id": f"p{i:04d}",
                "vector": rng.normal(size=dim).tolist(),
                "payload": {"tier": "public" if i % 3 else "private", "n": i},
                "text": f"text {i}",
            }
            for i in range(n)
        ]

    def test_exact_top_k_matches_numpy_reference_for_every_metric(self):
        rng = np.random.default_rng(7)
        dim, n = 16, 300
        points = self._points(n, dim, rng)
        matrix = np.array([p["vector"] for p in points], dtype=np.float32)
        for metric in ("cosine", "dot", "euclid"):
            store = self._store(dim, metric, name=metric)
            self.assertEqual(store.upsert(points), n)
            for trial in range(3):
                q = rng.normal(size=dim).astype(np.float32)
                flt = None if trial == 0 else {"tier": "public", "n": {"$gte": 100}}
                if metric == "cosine":
                    mn = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
                    scores = mn @ (q / np.linalg.norm(q))
                elif metric == "dot":
                    scores = matrix @ q
                else:
                    scores = -np.sqrt(((matrix - q) ** 2).sum(axis=1))
                candidates = [
                    i for i, p in enumerate(points) if filters.matches(p["payload"], flt)
                ]
                ref = sorted(candidates, key=lambda i: -scores[i])[:7]
                hits = store.search(q, k=7, flt=flt)
                self.assertEqual([h["id"] for h in hits], [points[i]["id"] for i in ref], metric)
                for h, i in zip(hits, ref):
                    self.assertAlmostEqual(h["score"], float(scores[i]), places=4)
                    self.assertEqual(h["payload"], points[i]["payload"])
                    self.assertEqual(h["text"], points[i]["text"])
                # higher is always better, whatever the metric
                self.assertEqual([h["score"] for h in hits], sorted((h["score"] for h in hits), reverse=True))

    def test_dimension_mismatch_is_a_clean_error(self):
        store = self._store(dim=4)
        with self.assertRaises(DimensionMismatch):
            store.upsert([{"id": "a", "vector": [1, 2, 3]}])
        store.upsert([{"id": "a", "vector": [1, 2, 3, 4]}])
        with self.assertRaises(DimensionMismatch):
            store.search([1, 2, 3], k=1)
        # A corrupted blob on disk is reported, not turned into NaNs.
        conn = sqlite3.connect(store.path)
        conn.execute("UPDATE points SET vector = ? WHERE id = 'a'", (b"\x00" * 8,))
        conn.execute("UPDATE meta SET version = version + 1")
        conn.commit()
        conn.close()
        with self.assertRaises(DimensionMismatch):
            store.search([1, 2, 3, 4], k=1)

    def test_hard_cap_refuses_the_batch(self):
        store = self._store(dim=2)
        with mock.patch.object(local_store, "HARD_LIMIT_POINTS", 5):
            store.upsert([{"id": f"p{i}", "vector": [1, 0]} for i in range(5)])
            with self.assertRaises(LocalStoreFull):
                store.upsert([{"id": "p5", "vector": [1, 0]}])
            # Replacing existing ids is not growth.
            store.upsert([{"id": "p0", "vector": [0, 1]}])
        self.assertEqual(store.count(), 5)

    def test_cache_reloads_when_another_handle_writes(self):
        store = self._store(dim=2)
        store.upsert([{"id": "a", "vector": [1, 0]}])
        self.assertEqual([h["id"] for h in store.search([1, 0], k=5)], ["a"])
        other = LocalStore.open(store.path)
        other.upsert([{"id": "b", "vector": [1, 0.01]}])
        self.assertEqual({h["id"] for h in store.search([1, 0], k=5)}, {"a", "b"})
        other.delete(flt={"n": None})  # matches everything without "n"
        self.assertEqual(store.search([1, 0], k=5), [])
        self.assertEqual(store.version(), 3)

    @override_settings(PYRUNNER_VECTOR_CACHE_MB=0)
    def test_cache_budget_evicts_least_recent(self):
        a, b = self._store(dim=2, name="a"), self._store(dim=2, name="b")
        a.upsert([{"id": "x", "vector": [1, 0]}])
        b.upsert([{"id": "y", "vector": [0, 1]}])
        a.search([1, 0], k=1)
        b.search([0, 1], k=1)
        self.assertEqual(len(local_store._CACHE), 1)
        self.assertIn(str(b.path.resolve()), local_store._CACHE)

    def test_get_scroll_delete_and_validation(self):
        store = self._store(dim=2)
        store.upsert([{"id": f"p{i:02d}", "vector": [1, i], "payload": {"n": i}} for i in range(25)])
        self.assertEqual([x["id"] for x in store.get(["p03", "nope", "p01"])], ["p03", "p01"])
        page, cursor = store.scroll(limit=10)
        self.assertEqual(len(page), 10)
        self.assertEqual(cursor, "p09")
        rest, cursor2 = store.scroll(cursor=cursor, limit=100)
        self.assertEqual(len(rest), 15)
        self.assertIsNone(cursor2)
        self.assertEqual(store.delete(ids=["p00", "p01"]), 2)
        self.assertEqual(store.delete(flt={"n": {"$gte": 20}}), 5)
        self.assertEqual(store.count(), 18)
        with self.assertRaises(local_store.LocalStoreError):
            store.delete()
        with self.assertRaises(local_store.LocalStoreError):
            store.upsert([{"id": "", "vector": [1, 1]}])
        with self.assertRaises(local_store.LocalStoreError):
            store.upsert([{"id": "q", "vector": [float("nan"), 1]}])
        with self.assertRaises(filters.UnsupportedFilter):
            store.count({"a.b": 1})

    def test_large_id_lists_are_chunked_below_sqlite_parameter_limit(self):
        store = self._store(dim=2)
        n = local_store._ID_CHUNK * 2 + 17
        points = [{"id": f"p{i:05d}", "vector": [1, i % 7], "payload": {"n": i}} for i in range(n)]
        self.assertEqual(store.upsert(points), n)
        self.assertEqual(store.upsert(points[:5] + points[-5:]), 10)  # replace, no growth
        self.assertEqual(store.count(), n)
        ids = [p["id"] for p in points]
        self.assertEqual(len(store.get(ids)), n)
        self.assertEqual(len(store.search([1, 1], k=1000)), 1000)
        self.assertEqual(store.delete(ids=ids[: local_store._ID_CHUNK + 3]), local_store._ID_CHUNK + 3)
        self.assertEqual(store.count(), n - local_store._ID_CHUNK - 3)

    def test_export_import_round_trip(self):
        store = self._store(dim=3)
        store.upsert([{"id": "a", "vector": [1, 0, 0], "payload": {"k": 1}, "text": "hello"}])
        image = store.export_bytes()
        self.assertEqual(image[:16], b"SQLite format 3\x00")
        copy = LocalStore.import_bytes(self.dir / "copy.sqlite", image)
        self.assertEqual(copy.dim, 3)
        self.assertEqual(copy.get(["a"])[0]["text"], "hello")
        self.assertEqual([h["id"] for h in copy.search([1, 0, 0], k=1)], ["a"])
        with self.assertRaises(local_store.LocalStoreError):
            LocalStore.import_bytes(self.dir / "bad.sqlite", b"garbage")
        self.assertFalse((self.dir / "bad.sqlite").exists())
        store.delete_files()
        self.assertFalse(store.path.exists())

# =============================================================================
# Embedding client
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class EmbeddingClientTests(TestCase):
    def _client(self, provider_type="openai", max_batch=2, responses=None, model=None):
        preset = EMBEDDING_PRESETS[provider_type]
        provider = EmbeddingProvider(
            provider_type=provider_type,
            name="t",
            base_url=preset["base_url"] or "https://emb.example",
            model=model or preset["default_model"] or "m",
            max_batch=max_batch,
        )
        if preset["credential_required"]:
            provider.set_credential("k")
        session = mock.Mock()
        session.post.side_effect = responses
        return EmbeddingClient(provider, session=session), session

    @staticmethod
    def _ok(batch, usage=None, dim=3):
        payload = {"data": [{"index": i, "embedding": [float(i)] * dim} for i in range(len(batch))]}
        if usage is not None:
            payload["usage"] = usage
        return _Response(200, payload)

    def test_batches_by_max_batch_and_sums_provider_usage(self):
        def respond(url, json, headers, timeout):
            return self._ok(json["input"], usage={"total_tokens": 10})

        client, session = self._client(max_batch=2, responses=respond)
        result = client.embed(["a", "b", "c", "d", "e"])
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(len(result.vectors), 5)
        self.assertEqual(result.dimension, 3)
        self.assertEqual(result.tokens, 30)
        self.assertEqual(result.calls, 3)
        url = session.post.call_args_list[0].args[0]
        self.assertEqual(url, "https://api.openai.com/v1/embeddings")
        self.assertEqual(session.post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer k")

    def test_usage_estimated_when_provider_omits_it(self):
        texts = ["x" * 40, "y" * 7]
        client, session = self._client(
            provider_type="ollama", max_batch=10, responses=lambda url, **kw: self._ok(kw["json"]["input"])
        )
        result = client.embed(texts)
        self.assertEqual(result.tokens, 10 + 1)
        # No credential for a plain Ollama → no Authorization header at all.
        self.assertNotIn("Authorization", session.post.call_args.kwargs["headers"])

    def test_truncation_counted_not_rejected(self):
        seen = {}

        def respond(url, json, headers, timeout):
            seen["lens"] = [len(t) for t in json["input"]]
            return self._ok(json["input"], usage={"total_tokens": 1})

        client, _ = self._client(max_batch=10, responses=respond)
        result = client.embed(["a" * (MAX_TEXT_CHARS + 100), "short"])
        self.assertEqual(result.truncated, 1)
        self.assertEqual(seen["lens"], [MAX_TEXT_CHARS, 5])

    def test_query_hint_params_for_voyage_and_jina_and_zai_path(self):
        bodies = []

        def respond(url, json, headers, timeout):
            bodies.append((url, json))
            return self._ok(json["input"], usage={"total_tokens": 1})

        for ptype, param, doc, query in (
            ("voyage", "input_type", "document", "query"),
            ("jina", "task", "retrieval.passage", "retrieval.query"),
        ):
            client, _ = self._client(provider_type=ptype, max_batch=5, responses=respond)
            client.embed(["d"])
            client.embed(["q"], input_type="query")
            self.assertEqual(bodies[-2][1][param], doc, ptype)
            self.assertEqual(bodies[-1][1][param], query, ptype)
        client, _ = self._client(provider_type="zai", max_batch=5, responses=respond)
        client.embed(["z"])
        self.assertEqual(bodies[-1][0], "https://api.z.ai/api/paas/v4/embeddings")
        self.assertNotIn("input_type", bodies[-1][1])

    def test_retries_429_then_succeeds(self):
        answers = iter([_Response(429, {"error": {"message": "slow down"}}), None])

        def respond(url, json, headers, timeout):
            nxt = next(answers)
            return nxt if nxt is not None else self._ok(json["input"], usage={"total_tokens": 1})

        client, session = self._client(max_batch=5, responses=respond)
        with mock.patch("core.vectors.embeddings.time.sleep") as sleep:
            result = client.embed(["a"])
        self.assertEqual(len(result.vectors), 1)
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()

    def test_errors_are_user_facing(self):
        client, _ = self._client(max_batch=5, responses=lambda url, **kw: _Response(401, {"error": "nope"}))
        with self.assertRaisesRegex(EmbeddingError, "authentication failed"):
            client.embed(["a"])
        client, _ = self._client(max_batch=5, responses=lambda url, **kw: _Response(404, None, "nf"))
        with self.assertRaisesRegex(EmbeddingError, "endpoint not found"):
            client.embed(["a"])
        client, _ = self._client(
            max_batch=5, responses=lambda url, **kw: _Response(200, {"data": [{"embedding": [1.0]}]})
        )
        with self.assertRaisesRegex(EmbeddingError, "returned 1 embeddings for 2 inputs"):
            client.embed(["a", "b"])
        client, _ = self._client(
            max_batch=5, responses=lambda url, **kw: _Response(400, {"error": {"message": "bad model"}})
        )
        with self.assertRaisesRegex(EmbeddingError, "bad model"):
            client.embed(["a"])
        with self.assertRaisesRegex(EmbeddingError, "list of strings"):
            client.embed("not a list")

    def test_test_records_dimension(self):
        client, _ = self._client(
            max_batch=5, responses=lambda url, **kw: self._ok(kw["json"]["input"], usage={"total_tokens": 5}, dim=12)
        )
        self.assertEqual(client.test().dimension, 12)

# =============================================================================
# Models + migration seed
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ModelTests(TestCase):
    def test_migration_seeds_the_local_default(self):
        local = VectorConnection.objects.get(name="Local")
        self.assertEqual(local.provider_type, VectorConnection.ProviderType.LOCAL)
        self.assertTrue(local.enabled and local.is_default)
        self.assertEqual(VectorConnection.get_default(), local)
        self.assertEqual(VectorConnection.objects.count(), 1)

    def test_single_default_rule(self):
        local = VectorConnection.objects.get(name="Local")
        q = VectorConnection(
            provider_type="qdrant", name="Q", config={"url": "http://qdrant:6333"}, is_default=True
        )
        q.save()
        local.refresh_from_db()
        self.assertFalse(local.is_default)
        self.assertTrue(VectorConnection.objects.get(pk=q.pk).is_default)
        self.assertEqual(VectorConnection.objects.filter(is_default=True).count(), 1)

    def test_connection_and_provider_credentials_round_trip(self):
        q = VectorConnection(provider_type="qdrant", name="Q", config={"url": "http://q:6333"})
        q.set_credentials({"api_key": "secret", "blank": ""})
        q.save()
        self.assertEqual(VectorConnection.objects.get(pk=q.pk).get_credentials(), {"api_key": "secret"})
        self.assertNotIn("secret", q.credentials_encrypted)
        p = _provider()
        self.assertEqual(EmbeddingProvider.objects.get(pk=p.pk).get_credential(), "sk-test-key")
        self.assertNotIn("sk-test-key", p.credential_encrypted)
        self.assertEqual(p.endpoint_url, "https://api.openai.com/v1/embeddings")
        self.assertTrue(p.credential_required)
        self.assertTrue(p.is_tested)

# =============================================================================
# VectorService
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "svc")
class ServiceTests(TestCase):
    def setUp(self):
        clear_cache()
        self.ws = Workspace.objects.create(name="W")
        self.local = VectorConnection.objects.get(name="Local")
        self.provider = _provider()

    def _create(self, name="docs", **kw):
        kw.setdefault("embedding_provider", self.provider)
        return VectorService.create_collection(workspace=self.ws, name=name, **kw)

    def test_create_pins_model_and_dimension_and_creates_the_file(self):
        col = self._create()
        self.assertEqual(col.connection, self.local)
        self.assertEqual(col.embedding_model, "text-embedding-3-small")
        self.assertEqual(col.dimension, 8)
        self.assertEqual(col.backend_ref, f"vectors/{col.id}.sqlite")
        self.assertTrue(col.file_path.exists())
        self.assertEqual(col.file_path.parent, Path(_TMP / "svc"))
        self.assertTrue(col.is_ready)

    def test_create_validation(self):
        with self.assertRaisesRegex(VectorError, "lowercase"):
            self._create(name="Bad Name")
        untested = _provider(name="Untested", dimension=None)
        with self.assertRaisesRegex(VectorError, "not been tested"):
            self._create(embedding_provider=untested)
        with self.assertRaisesRegex(VectorError, "dimension is required"):
            self._create(embedding_provider=None)
        with self.assertRaisesRegex(VectorError, "does not match"):
            self._create(dimension=16)
        self._create()
        with self.assertRaisesRegex(VectorError, "already exists"):
            self._create()
        other = Workspace.objects.create(name="Other")
        VectorService.create_collection(workspace=other, name="docs", embedding_provider=self.provider)
        q = VectorConnection.objects.create(provider_type="qdrant", name="Q", config={"url": "http://q"})
        with self.assertRaisesRegex(VectorError, "not available yet"):
            self._create(name="onq", connection=q)
        only_vectors = self._create(name="raw", embedding_provider=None, dimension=3, metric="dot")
        self.assertEqual(only_vectors.embedding_model, "")

    def test_add_texts_search_and_usage_counters(self):
        col = self._create()
        with fake_embeddings():
            handle = VectorService.handle(col)
            out = handle.add_texts(
                [
                    {"id": "a", "text": "schedule a script every morning", "payload": {"tier": "public"}},
                    {"id": "b", "text": "rotate the api key", "payload": {"tier": "private"}},
                    {"id": "c", "text": "schedule nightly backups", "payload": {"tier": "public"}},
                ]
            )
            self.assertEqual(out["added"], 3)
            self.assertGreater(out["tokens"], 0)
            hits = handle.search("schedule a script every morning", k=2, filter={"tier": "public"})
        self.assertEqual(hits[0]["id"], "a")
        self.assertAlmostEqual(hits[0]["score"], 1.0, places=4)
        self.assertEqual({h["payload"]["tier"] for h in hits}, {"public"})
        col.refresh_from_db()
        self.provider.refresh_from_db()
        self.assertEqual(col.points_count, 3)
        self.assertEqual(col.embed_calls, 2)  # one add_texts batch (max_batch=4) + one query
        self.assertEqual(col.tokens_embedded, out["tokens"] + max(1, len("schedule a script every morning") // 4))
        self.assertEqual(self.provider.embed_calls, 2)
        self.assertEqual(self.provider.tokens_embedded, col.tokens_embedded)
        stats = handle.stats()
        self.assertEqual(stats["points"], 3)
        self.assertGreater(stats["size_bytes"], 0)

    def test_model_pin_refuses_a_changed_model(self):
        col = self._create()
        self.provider.model = "text-embedding-3-large"
        self.provider.save()
        col.refresh_from_db()
        with fake_embeddings():
            with self.assertRaises(VectorError) as ctx:
                VectorService.handle(col).add_texts([{"id": "a", "text": "x"}])
        self.assertEqual(ctx.exception.code, "model_mismatch")
        self.assertIn("Create a new collection", str(ctx.exception))

    def test_vectors_only_collection_and_error_codes(self):
        col = self._create(name="raw", embedding_provider=None, dimension=3)
        handle = VectorService.handle(col)
        handle.add_vectors([{"id": "a", "vector": [1, 0, 0]}, {"id": "b", "vector": [0, 1, 0]}])
        self.assertEqual(handle.search_vector([1, 0, 0], k=1)[0]["id"], "a")
        with self.assertRaises(VectorError) as ctx:
            handle.search("text query")
        self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(VectorError) as ctx:
            handle.search_vector([1, 0], k=1)
        self.assertEqual(ctx.exception.code, "dimension_mismatch")
        with self.assertRaises(VectorError) as ctx:
            handle.count(filter={"a.b": 1})
        self.assertEqual(ctx.exception.code, "unsupported_filter")
        self.assertEqual(handle.delete(ids=["a"])["deleted"], 1)
        col.refresh_from_db()
        self.assertEqual(col.points_count, 1)

    def test_delete_removes_the_file_and_retry_recreates_a_lost_one(self):
        col = self._create()
        path = col.file_path
        path.unlink()
        with self.assertRaises(VectorError) as ctx:
            VectorService.handle(col)
        self.assertEqual(ctx.exception.code, "backend_unavailable")
        VectorService.retry_backend(col)
        self.assertTrue(path.exists())
        VectorService.delete_collection(col)
        self.assertFalse(path.exists())
        self.assertFalse(VectorCollection.objects.filter(pk=col.pk).exists())

    def test_connection_tests(self):
        ok, message = VectorService.test_connection(self.local)
        self.assertTrue(ok, message)
        self.assertFalse(list(Path(_TMP / "svc").glob(".probe-*")))
        q = VectorConnection(provider_type="qdrant", name="Q", config={"url": "http://10.0.0.5:6333"})
        ok, message = VectorService.test_connection(q)
        self.assertFalse(ok)
        self.assertIn("PYRUNNER_ALLOW_PRIVATE_NETWORK", message)
        with mock.patch("core.vectors.service.requests.get") as get, override_settings(
            PYRUNNER_ALLOW_PRIVATE_NETWORK=True
        ):
            get.return_value = _Response(200, {"result": {"collections": [{"name": "x"}]}})
            ok, message = VectorService.test_connection(q)
        self.assertTrue(ok)
        self.assertIn("1 collection", message)

    def test_test_provider_records_dimension(self):
        p = _provider(name="Fresh", dimension=None)
        with fake_embeddings():
            result = VectorService.test_provider(p)
        self.assertEqual(result.dimension, 8)
        p.refresh_from_db()
        self.assertEqual(p.dimension, 8)
        self.assertIsNotNone(p.last_tested_at)
        self.assertEqual(p.embed_calls, 1)

# =============================================================================
# Console
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "console")
class ConsoleAccessTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        self.member = User.objects.create(email="member@example.com")
        self.outsider = User.objects.create(email="out@example.com")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER)
        WorkspaceMembership.objects.create(user=self.member, workspace=self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self.url = reverse("cpanel_ws:vector_list", args=[self.ws.id])

    def test_owner_200_member_403_outsider_404_superuser_200(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        boss = User.objects.create(email="root@example.com", is_superuser=True)
        self.client.force_login(boss)
        self.assertEqual(self.client.get(self.url).status_code, 200)

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "flow")
class ConsoleFlowTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        clear_cache()
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.provider = _provider()
        self.client.force_login(self.owner)

    def _url(self, name, *args):
        return reverse(f"cpanel_ws:{name}", args=[self.ws.id, *args])

    def test_create_search_toggle_delete_flow(self):
        local = VectorConnection.objects.get(name="Local")
        resp = self.client.post(
            self._url("vector_create"),
            {
                "name": "docs",
                "description": "site pages",
                "connection": str(local.pk),
                "embedding_provider": str(self.provider.pk),
                "metric": "cosine",
                "include_in_backup": "on",
            },
        )
        col = VectorCollection.objects.get(name="docs")
        self.assertEqual(col.workspace_id, self.ws.id)
        self.assertEqual(col.created_by, self.owner)
        self.assertRedirects(resp, reverse("cpanel:vector_detail", args=[col.pk]), fetch_redirect_response=False)
        self.assertTrue(col.file_path.exists())

        with fake_embeddings():
            VectorService.handle(col).add_texts(
                [{"id": "a", "text": "hello world", "payload": {"tier": "public"}}]
            )
            resp = self.client.post(
                self._url("vector_search", col.pk),
                {"query": "hello world", "k": "3", "filter": '{"tier": "public"}'},
            )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("score 1.0000", content)
        self.assertIn("hello world", content)

        resp = self.client.post(
            self._url("vector_search", col.pk), {"query": "x", "k": "3", "filter": "{not json"}
        )
        self.assertIn("not valid JSON", resp.content.decode())

        resp = self.client.get(self._url("vector_detail", col.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text-embedding-3-small", resp.content.decode())
        resp = self.client.get(self._url("vector_list"))
        self.assertIn("docs", resp.content.decode())

        self.client.post(self._url("vector_backup_toggle", col.pk))
        col.refresh_from_db()
        self.assertFalse(col.include_in_backup)

        resp = self.client.post(self._url("vector_delete", col.pk), {"confirm_name": "wrong"})
        self.assertTrue(VectorCollection.objects.filter(pk=col.pk).exists())
        path = col.file_path
        resp = self.client.post(self._url("vector_delete", col.pk), {"confirm_name": "docs"})
        self.assertRedirects(resp, reverse("cpanel:vector_list"), fetch_redirect_response=False)
        self.assertFalse(VectorCollection.objects.filter(pk=col.pk).exists())
        self.assertFalse(path.exists())

    def test_create_form_errors(self):
        local = VectorConnection.objects.get(name="Local")
        resp = self.client.post(
            self._url("vector_create"),
            {"name": "docs", "connection": str(local.pk), "metric": "cosine"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Give a dimension, or pick an embedding provider", resp.content.decode())
        untested = _provider(name="Untested", dimension=None)
        resp = self.client.post(
            self._url("vector_create"),
            {
                "name": "docs",
                "connection": str(local.pk),
                "embedding_provider": str(untested.pk),
                "metric": "cosine",
            },
        )
        self.assertIn("has not been tested yet", resp.content.decode())
        self.assertFalse(VectorCollection.objects.exists())

    def test_plugin_owned_collection_is_delete_guarded(self):
        col = VectorService.create_collection(
            workspace=self.ws, name="owned", embedding_provider=self.provider, owner_plugin="demo", owner_key="k"
        )
        resp = self.client.post(self._url("vector_delete", col.pk), {"confirm_name": "owned"})
        self.assertRedirects(resp, reverse("cpanel:vector_list"), fetch_redirect_response=False)
        self.assertTrue(VectorCollection.objects.filter(pk=col.pk).exists())
        boss = User.objects.create(email="root@example.com", is_superuser=True)
        self.client.force_login(boss)
        self.client.post(self._url("vector_delete", col.pk), {"confirm_name": "owned", "force": "1"})
        self.assertFalse(VectorCollection.objects.filter(pk=col.pk).exists())

    def test_list_without_an_enabled_store_explains(self):
        VectorConnection.objects.update(enabled=False)
        resp = self.client.get(self._url("vector_list"))
        self.assertIn("No vector store is enabled", resp.content.decode())
        resp = self.client.get(self._url("vector_create"))
        self.assertRedirects(resp, reverse("cpanel:vector_list"), fetch_redirect_response=False)

# =============================================================================
# Services page
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "services")
class ServicesPageTests(TestCase):
    def setUp(self):
        s = GlobalSettings.get_settings()
        s.setup_completed = True
        s.save()
        Environment.objects.get_or_create(name="default", defaults={"is_default": True, "python_version": "3.12"})
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.ensure(self.admin, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.admin)

    def test_page_renders_both_cards_without_leaking_keys(self):
        _provider(name="My Emb")
        resp = self.client.get(reverse("cpanel:services"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Embedding Providers", content)
        self.assertIn("Vector Stores", content)
        self.assertIn("My Emb", content)
        self.assertIn("Local", content)
        self.assertIn("embedding-providers-data", content)
        self.assertIn("vector-connections-data", content)
        self.assertNotIn("sk-test-key", content)

    def test_non_superuser_redirected(self):
        user = User.objects.create(email="u@example.com")
        self.client.force_login(user)
        resp = self.client.get(reverse("cpanel:services"))
        self.assertEqual(resp.status_code, 302)

    def test_embedding_provider_save_edit_and_test(self):
        resp = self.client.post(
            reverse("cpanel:embedding_provider_save"),
            {"provider_type": "openai", "name": "OpenAI", "credential": "sk-live", "enabled": "on"},
        )
        self.assertRedirects(resp, reverse("cpanel:services"), fetch_redirect_response=False)
        p = EmbeddingProvider.objects.get(name="OpenAI")
        self.assertEqual(p.base_url, "https://api.openai.com")  # preset fill
        self.assertEqual(p.model, "text-embedding-3-small")
        self.assertEqual(p.max_batch, 100)
        self.assertIsNone(p.dimension)
        self.assertEqual(p.get_credential(), "sk-live")

        # Test on the saved row records the dimension.
        with fake_embeddings():
            resp = self.client.post(
                reverse("cpanel:embedding_provider_test"),
                json.dumps({"provider_id": str(p.pk)}),
                content_type="application/json",
            )
        body = resp.json()
        self.assertTrue(body["success"], body)
        self.assertEqual(body["dimension"], 8)
        p.refresh_from_db()
        self.assertEqual(p.dimension, 8)

        # Editing with a blank key keeps the stored one; a changed model resets the dimension.
        self.client.post(
            reverse("cpanel:embedding_provider_save"),
            {
                "provider_id": str(p.pk),
                "provider_type": "openai",
                "name": "OpenAI",
                "model": "text-embedding-3-large",
                "enabled": "on",
            },
        )
        p.refresh_from_db()
        self.assertEqual(p.get_credential(), "sk-live")
        self.assertEqual(p.model, "text-embedding-3-large")
        self.assertIsNone(p.dimension)

        # Unsaved form values: typed credential, transient probe (nothing persisted).
        with fake_embeddings():
            resp = self.client.post(
                reverse("cpanel:embedding_provider_test"),
                json.dumps({"provider_type": "ollama", "model": "nomic-embed-text", "base_url": "https://ollama.example"}),
                content_type="application/json",
            )
        self.assertTrue(resp.json()["success"], resp.json())
        self.assertEqual(EmbeddingProvider.objects.count(), 1)

    def test_embedding_provider_form_requires_key_and_guards_url(self):
        resp = self.client.post(
            reverse("cpanel:embedding_provider_save"),
            {"provider_type": "openai", "name": "NoKey", "enabled": "on"},
            follow=True,
        )
        self.assertFalse(EmbeddingProvider.objects.filter(name="NoKey").exists())
        self.assertIn("API key is required", resp.content.decode())
        resp = self.client.post(
            reverse("cpanel:embedding_provider_save"),
            {"provider_type": "ollama", "name": "Local Ollama", "base_url": "http://localhost:11434", "enabled": "on"},
            follow=True,
        )
        self.assertFalse(EmbeddingProvider.objects.filter(name="Local Ollama").exists())
        self.assertIn("PYRUNNER_ALLOW_PRIVATE_NETWORK", resp.content.decode())
        with override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True):
            self.client.post(
                reverse("cpanel:embedding_provider_save"),
                {"provider_type": "ollama", "name": "Local Ollama", "base_url": "http://localhost:11434", "enabled": "on"},
            )
        self.assertTrue(EmbeddingProvider.objects.filter(name="Local Ollama").exists())

    def test_vector_connection_save_default_test_and_delete(self):
        resp = self.client.post(
            reverse("cpanel:vector_connection_save"),
            {"provider_type": "qdrant", "name": "Q", "url": "http://10.0.0.5:6333", "api_key": "k", "enabled": "on"},
            follow=True,
        )
        self.assertFalse(VectorConnection.objects.filter(name="Q").exists())
        self.assertIn("PYRUNNER_ALLOW_PRIVATE_NETWORK", resp.content.decode())

        self.client.post(
            reverse("cpanel:vector_connection_save"),
            {"provider_type": "qdrant", "name": "Q", "url": "https://qdrant.example/", "api_key": "k", "enabled": "on"},
        )
        q = VectorConnection.objects.get(name="Q")
        self.assertEqual(q.url, "https://qdrant.example")
        self.assertEqual(q.get_credentials(), {"api_key": "k"})
        self.assertFalse(q.is_default)

        self.client.post(reverse("cpanel:vector_connection_default", args=[q.pk]))
        self.assertTrue(VectorConnection.objects.get(pk=q.pk).is_default)
        self.assertFalse(VectorConnection.objects.get(name="Local").is_default)

        with mock.patch("core.vectors.service.requests.get") as get:
            get.return_value = _Response(200, {"result": {"collections": []}})
            resp = self.client.post(
                reverse("cpanel:vector_connection_test"),
                json.dumps({"connection_id": str(q.pk)}),
                content_type="application/json",
            )
        self.assertTrue(resp.json()["success"], resp.json())
        self.assertEqual(get.call_args.kwargs["headers"]["api-key"], "k")
        self.assertIsNotNone(VectorConnection.objects.get(pk=q.pk).last_tested_at)

        local = VectorConnection.objects.get(name="Local")
        resp = self.client.post(
            reverse("cpanel:vector_connection_test"),
            json.dumps({"connection_id": str(local.pk)}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["success"], resp.json())

        # PROTECT: a connection with collections cannot be deleted.
        VectorService.create_collection(workspace=Workspace.get_default(), name="c", dimension=3, connection=local)
        resp = self.client.post(reverse("cpanel:vector_connection_delete", args=[local.pk]), follow=True)
        self.assertTrue(VectorConnection.objects.filter(pk=local.pk).exists())
        self.assertIn("holds 1 collection", resp.content.decode())
        resp = self.client.post(reverse("cpanel:vector_connection_delete", args=[q.pk]), follow=True)
        self.assertFalse(VectorConnection.objects.filter(pk=q.pk).exists())
        self.assertIn("No default vector store", resp.content.decode())

# =============================================================================
# Backup 1.9.0
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "backup")
class BackupTests(TestCase):
    def setUp(self):
        clear_cache()
        self.user = User.objects.create(email="admin@example.com", is_superuser=True)
        self.ws = Workspace.get_default()
        self.provider = _provider()
        self.col = VectorService.create_collection(
            workspace=self.ws, name="docs", embedding_provider=self.provider, description="d"
        )
        VectorService.handle(self.col).add_vectors(
            [{"id": f"p{i}", "vector": _vec(str(i)), "payload": {"n": i}, "text": f"t{i}"} for i in range(5)]
        )
        self.col.refresh_from_db()

    def test_export_carries_rows_and_the_local_image(self):
        data = BackupService.create_backup(include_runs=False)
        version = tuple(int(p) for p in data["backup_metadata"]["version"].split("."))
        self.assertGreaterEqual(version, (1, 9, 0))
        self.assertEqual(data["embedding_providers"][0]["name"], "Emb")
        self.assertEqual(data["embedding_providers"][0]["credential_encrypted"], self.provider.credential_encrypted)
        self.assertEqual({c["name"] for c in data["vector_connections"]}, {"Local"})
        row = data["vector_collections"][0]
        self.assertEqual(row["connection_name"], "Local")
        self.assertEqual(row["embedding_provider_name"], "Emb")
        self.assertEqual(row["points_count"], 5)
        self.assertEqual(row["file_skipped_reason"], "")
        self.assertGreater(row["file_bytes"], 0)
        image = base64.b64decode(row["file_b64"])
        self.assertEqual(image[:16], b"SQLite format 3\x00")
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["vector_collections"], 1)

    def test_restore_round_trip_rewrites_the_file(self):
        data = BackupService.create_backup(include_runs=False)
        old_id = self.col.id
        # Mutate the live instance so the restore has something to replace.
        VectorService.delete_collection(self.col)
        self.provider.model = "changed"
        self.provider.save()
        local = VectorConnection.objects.get(name="Local")
        local.enabled = False
        local.save()

        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["vector_collections"], 1)
        self.assertEqual(result["counts"]["embedding_providers"], 1)
        self.assertEqual(result["counts"]["vector_connections"], 1)
        col = VectorCollection.objects.get(name="docs")
        self.assertEqual(col.id, old_id)
        self.assertEqual(col.connection.name, "Local")
        self.assertTrue(col.connection.enabled)  # upsert restored the backup's row
        self.assertEqual(col.embedding_provider.model, "text-embedding-3-small")
        self.assertEqual(col.points_count, 5)
        self.assertTrue(col.is_ready)
        handle = VectorService.handle(col)
        self.assertEqual(handle.count(), 5)
        self.assertEqual(handle.search_vector(_vec("3"), k=1)[0]["id"], "p3")
        self.assertEqual([w for w in result["warnings"] if "Vector" in w], [])

    @override_settings(PYRUNNER_VECTOR_BACKUP_MAX_MB=0)
    def test_size_cap_skips_the_image_and_restores_empty_with_a_warning(self):
        data = BackupService.create_backup(include_runs=False)
        row = data["vector_collections"][0]
        self.assertIsNone(row["file_b64"])
        self.assertIn("too_large", row["file_skipped_reason"])
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        col = VectorCollection.objects.get(name="docs")
        self.assertEqual(col.points_count, 0)
        self.assertTrue(col.is_ready)
        self.assertTrue(any("re-embed" in w for w in result["warnings"]), result["warnings"])

    def test_opt_out_and_missing_provider(self):
        self.col.include_in_backup = False
        self.col.save()
        data = BackupService.create_backup(include_runs=False)
        self.assertEqual(data["vector_collections"][0]["file_skipped_reason"], "excluded")
        data["embedding_providers"] = []
        data["vector_collections"][0]["embedding_provider_name"] = "Gone"
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        col = VectorCollection.objects.get(name="docs")
        self.assertIsNone(col.embedding_provider)
        self.assertTrue(any("'Gone' not found" in w for w in result["warnings"]), result["warnings"])

    def test_pre_1_9_backup_is_a_no_op_for_vectors(self):
        data = BackupService.create_backup(include_runs=False)
        for key in ("embedding_providers", "vector_connections", "vector_collections"):
            data.pop(key)
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertFalse(VectorCollection.objects.exists())  # full replace, nothing to restore
        self.assertTrue(VectorConnection.objects.filter(name="Local").exists())
        self.assertTrue(EmbeddingProvider.objects.filter(name="Emb").exists())

# =============================================================================
# Plugin ownership cleanup + system check
# =============================================================================

@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "plugin")
class PluginCleanupTests(TestCase):
    def test_plugin_delete_removes_owned_collections_and_files(self):
        from core.services import PluginService

        ws = Workspace.get_default()
        owned = VectorService.create_collection(
            workspace=ws, name="demo-idx", dimension=3, owner_plugin="demo", owner_key="idx"
        )
        other = VectorService.create_collection(workspace=ws, name="mine", dimension=3)
        self.assertEqual(PluginService.owned_resource_counts("demo")["vector_collections"], 1)
        counts = PluginService._cleanup_owned_resources("demo")
        self.assertEqual(counts.get("vector_collections"), 1)
        self.assertFalse(VectorCollection.objects.filter(pk=owned.pk).exists())
        self.assertFalse(owned.file_path.exists())
        self.assertTrue(VectorCollection.objects.filter(pk=other.pk).exists())
        self.assertTrue(other.file_path.exists())
        # The delete-preview label table must know the new resource too.
        from core.models.plugin import RESOURCE_LABELS

        self.assertIn(("vector_collections", "vector collection", "vector collections"), RESOURCE_LABELS)

class SystemCheckTests(SimpleTestCase):
    def test_check_passes_here_and_reports_a_missing_json1(self):
        from core import checks

        self.assertEqual(checks.check_vector_store_prerequisites(None), [])
        with mock.patch.object(checks, "json1_available", return_value=False):
            findings = checks.check_vector_store_prerequisites(None)
        self.assertEqual([f.id for f in findings if isinstance(f, Error)], ["core.E001"])
