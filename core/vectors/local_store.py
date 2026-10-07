"""
Local vector backend: one SQLite file per collection + exact numpy search.

Why this shape (locked in docs/PLAN_vector_db.md): zero infrastructure, exact
results, fine to ~50k points per collection (the console suggests Qdrant above
``SOFT_LIMIT_POINTS``; ``HARD_LIMIT_POINTS`` is refused), and numpy is the only
new dependency. FAISS/hnswlib, sqlite-vec and pickle persistence were rejected.

File layout — ``data/vectors/<collection-uuid>.sqlite``, WAL journal:

    meta(id=1, dim, metric, version, schema)     one row; ``version`` bumps on
                                                  every write, in the write's
                                                  own transaction
    points(id TEXT PK, vector BLOB, payload TEXT, text TEXT)
                                                  vector = ``dim`` little-endian
                                                  float32s, header-less

Search filters in SQL first (``json_extract`` over ``payload``), then does a dot
product over the candidate rows of a per-process numpy matrix that is reloaded
whenever ``meta.version`` changed and kept under ``PYRUNNER_VECTOR_CACHE_MB``
across collections (LRU). Cosine = vectors normalized at write time, dot at
query time; euclid scores are negated so "higher is better" holds for every
metric.

Single-container only: SQLite WAL on a shared or network volume is unsafe, so a
multi-container deployment uses the Qdrant backend instead.
"""

import json
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from django.conf import settings

from core.vectors import filters

METRICS = ("cosine", "dot", "euclid")
SOFT_LIMIT_POINTS = 50_000
HARD_LIMIT_POINTS = 200_000
SCHEMA_VERSION = 1
MAX_ID_LENGTH = 256
DTYPE = np.dtype("<f4")  # float32, little-endian, always — never the native order
# SQLite binds at most 32 766 parameters per statement; id lists are chunked.
_ID_CHUNK = 900


def _chunks(items, size=_ID_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class LocalStoreError(Exception):
    """Base class for local store failures (user-facing messages)."""


class LocalStoreFull(LocalStoreError):
    """The write would push the collection past ``HARD_LIMIT_POINTS``."""


class DimensionMismatch(LocalStoreError):
    """A vector's length does not match the collection's pinned dimension."""


def cache_budget_bytes() -> int:
    return int(getattr(settings, "PYRUNNER_VECTOR_CACHE_MB", 256)) * 1024 * 1024


class _Cached:
    __slots__ = ("version", "ids", "index", "matrix")

    def __init__(self, version, ids, matrix):
        self.version = version
        self.ids = ids
        self.index = {pid: row for row, pid in enumerate(ids)}
        self.matrix = matrix

    @property
    def nbytes(self):
        return int(self.matrix.nbytes)


_CACHE: "OrderedDict[str, _Cached]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            _CACHE.move_to_end(key)
        return entry


def _cache_put(key, entry):
    budget = cache_budget_bytes()
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
        _CACHE[key] = entry
        total = sum(e.nbytes for e in _CACHE.values())
        while total > budget and len(_CACHE) > 1:
            _, evicted = _CACHE.popitem(last=False)
            total -= evicted.nbytes


def _cache_drop(key):
    with _CACHE_LOCK:
        _CACHE.pop(key, None)


def clear_cache():
    """Drop every cached matrix (tests, and after a restore replaced files)."""
    with _CACHE_LOCK:
        _CACHE.clear()


class LocalStore:
    """One collection's SQLite file. Cheap to construct; connections are per call."""

    def __init__(self, path, dim: int, metric: str):
        self.path = Path(path)
        self.dim = int(dim)
        self.metric = metric
        self._key = str(self.path.resolve())

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, path, dim: int, metric: str) -> "LocalStore":
        if metric not in METRICS:
            raise LocalStoreError(f"unknown metric {metric!r}")
        if dim <= 0:
            raise LocalStoreError("dimension must be positive")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise LocalStoreError(f"store already exists: {path.name}")
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dim INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    schema INTEGER NOT NULL
                );
                CREATE TABLE points (
                    id TEXT PRIMARY KEY,
                    vector BLOB NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    text TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO meta (id, dim, metric, version, schema) VALUES (1, ?, ?, 0, ?)",
                (dim, metric, SCHEMA_VERSION),
            )
            conn.commit()
        finally:
            conn.close()
        return cls(path, dim, metric)

    @classmethod
    def open(cls, path) -> "LocalStore":
        """Open an existing file, reading dim/metric from its meta row."""
        path = Path(path)
        if not path.exists():
            raise LocalStoreError(f"store file is missing: {path.name}")
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT dim, metric, schema FROM meta WHERE id = 1").fetchone()
        except sqlite3.DatabaseError as e:
            raise LocalStoreError(f"not a vector store file: {e}") from e
        finally:
            conn.close()
        if row is None:
            raise LocalStoreError("store file has no meta row")
        dim, metric, schema = row
        if schema != SCHEMA_VERSION:
            raise LocalStoreError(f"unsupported store schema {schema}")
        return cls(path, dim, metric)

    def delete_files(self) -> None:
        """Remove the database and its WAL sidecars; forget the cached matrix."""
        _cache_drop(self._key)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(f"{self.path}{suffix}")
            except OSError:
                pass
        return total

    def checkpoint(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    # ------------------------------------------------------------------ backup

    def export_bytes(self) -> bytes:
        """A consistent snapshot of the whole database as one SQLite image.

        ``Connection.serialize`` copies the pages the connection currently sees,
        so committed WAL content is included and no sidecar is ever needed.
        """
        conn = self._connect()
        try:
            image = bytearray(conn.serialize())
        finally:
            conn.close()
        # Header bytes 18/19 are the write/read format versions: 2 = WAL, 1 = legacy
        # rollback journal. An in-memory database cannot use WAL, so a WAL-flagged
        # image fails to deserialize ("unable to open database file"); mark the
        # image legacy — import re-enables WAL on the file it writes.
        if len(image) >= 20:
            image[18] = 1
            image[19] = 1
        return bytes(image)

    @classmethod
    def import_bytes(cls, path, data: bytes) -> "LocalStore":
        """Write a serialized image to ``path`` (replacing any file there)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{path}{suffix}")
            except FileNotFoundError:
                pass
        # Validate the image in memory before anything touches the disk: a
        # corrupt or foreign payload must fail here, not leave a broken file.
        probe = sqlite3.connect(":memory:")
        try:
            try:
                probe.deserialize(data)
                row = probe.execute("SELECT dim, metric, schema FROM meta WHERE id = 1").fetchone()
            except sqlite3.DatabaseError as e:
                raise LocalStoreError(f"backup payload is not a vector store image: {e}") from e
        finally:
            probe.close()
        if row is None or row[2] != SCHEMA_VERSION:
            raise LocalStoreError("backup payload is not a vector store image (no meta row)")
        # ``serialize()`` is the main database file byte for byte, so the image is
        # written as-is; opening it once re-asserts WAL mode for the new file.
        with open(path, "wb") as fh:
            fh.write(data)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        store = cls.open(path)
        _cache_drop(store._key)
        return store

    # ------------------------------------------------------------------ writes

    def upsert(self, points) -> int:
        """Insert or replace points: ``[{"id", "vector", "payload"?, "text"?}, ...]``.

        One transaction; bumps ``meta.version`` so every cached matrix reloads.
        Refuses the whole batch if it would exceed ``HARD_LIMIT_POINTS``.
        """
        rows = [self._encode_point(p) for p in points]
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                existing = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
                new_ids = list({r[0] for r in rows})
                already = sum(
                    conn.execute(
                        f"SELECT COUNT(*) FROM points WHERE id IN ({', '.join('?' * len(chunk))})",
                        chunk,
                    ).fetchone()[0]
                    for chunk in _chunks(new_ids)
                )
                if existing - already + len(new_ids) > HARD_LIMIT_POINTS:
                    raise LocalStoreFull(
                        f"collection would exceed {HARD_LIMIT_POINTS:,} points; "
                        "use a Qdrant connection for collections this large"
                    )
                conn.executemany(
                    "INSERT OR REPLACE INTO points (id, vector, payload, text) VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._bump(conn)
        finally:
            conn.close()
        return len(rows)

    def delete(self, ids=None, flt=None) -> int:
        """Delete by ids or by filter (exactly one). Returns the number removed."""
        if (ids is None) == (flt is None):
            raise LocalStoreError("delete takes ids or a filter, not both")
        conn = self._connect()
        try:
            with conn:
                if ids is not None:
                    ids = [str(i) for i in ids]
                    if not ids:
                        return 0
                    removed = sum(
                        conn.execute(
                            f"DELETE FROM points WHERE id IN ({', '.join('?' * len(chunk))})",
                            chunk,
                        ).rowcount
                        for chunk in _chunks(ids)
                    )
                else:
                    where, params = filters.to_sqlite(flt)
                    removed = conn.execute(f"DELETE FROM points WHERE {where}", params).rowcount
                if removed:
                    self._bump(conn)
        finally:
            conn.close()
        return removed

    # ------------------------------------------------------------------ reads

    def count(self, flt=None) -> int:
        where, params = filters.to_sqlite(flt)
        conn = self._connect()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM points WHERE {where}", params).fetchone()[0]
        finally:
            conn.close()

    def get(self, ids, include_vectors=False):
        ids = [str(i) for i in ids]
        if not ids:
            return []
        conn = self._connect()
        try:
            rows = []
            for chunk in _chunks(ids):
                rows.extend(
                    conn.execute(
                        "SELECT id, payload, text, vector FROM points "
                        f"WHERE id IN ({', '.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
                )
        finally:
            conn.close()
        by_id = {r[0]: r for r in rows}
        return [self._decode_row(by_id[i], include_vectors) for i in ids if i in by_id]

    def scroll(self, cursor=None, limit=100, flt=None, include_vectors=False):
        """Page through points ordered by id. Returns ``(items, next_cursor)``."""
        limit = max(1, min(int(limit), 1000))
        where, params = filters.to_sqlite(flt)
        sql = f"SELECT id, payload, text, vector FROM points WHERE {where}"
        if cursor:
            sql += " AND id > ?"
            params = [*params, str(cursor)]
        sql += " ORDER BY id LIMIT ?"
        conn = self._connect()
        try:
            rows = conn.execute(sql, [*params, limit + 1]).fetchall()
        finally:
            conn.close()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [self._decode_row(r, include_vectors) for r in rows]
        return items, (rows[-1][0] if more else None)

    def search(self, vector, k=10, flt=None):
        """Exact top-k: SQL filter → candidate rows → dot product → sort."""
        k = max(1, min(int(k), 1000))
        query = self._prepare_vector(vector, normalize=self.metric == "cosine")
        conn = self._connect()
        try:
            entry = self._cached(conn)
            if entry.matrix.shape[0] == 0:
                return []
            if flt is not None and flt != {}:
                where, params = filters.to_sqlite(flt)
                candidate_ids = [
                    r[0] for r in conn.execute(f"SELECT id FROM points WHERE {where}", params)
                ]
                rows = np.fromiter(
                    (entry.index[i] for i in candidate_ids if i in entry.index), dtype=np.int64
                )
                if rows.size == 0:
                    return []
                sub = entry.matrix[rows]
            else:
                rows = None
                sub = entry.matrix

            if self.metric == "euclid":
                scores = -np.sqrt(((sub - query) ** 2).sum(axis=1))
            else:
                scores = sub @ query

            k = min(k, scores.shape[0])
            top = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] else np.arange(k)
            top = top[np.argsort(-scores[top], kind="stable")]
            hit_rows = top if rows is None else rows[top]
            hit_ids = [entry.ids[int(r)] for r in hit_rows]

            meta_rows = []
            for chunk in _chunks(hit_ids):
                meta_rows.extend(
                    conn.execute(
                        "SELECT id, payload, text FROM points "
                        f"WHERE id IN ({', '.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
                )
        finally:
            conn.close()
        meta = {r[0]: r for r in meta_rows}
        results = []
        for pid, score in zip(hit_ids, scores[top]):
            _, payload, text = meta.get(pid, (pid, "{}", None))
            results.append(
                {"id": pid, "score": float(score), "payload": json.loads(payload), "text": text}
            )
        return results

    def version(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()[0]
        finally:
            conn.close()

    # ------------------------------------------------------------------ internals

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @staticmethod
    def _bump(conn):
        conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")

    def _cached(self, conn) -> _Cached:
        version = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()[0]
        entry = _cache_get(self._key)
        if entry is not None and entry.version == version:
            return entry
        rows = conn.execute("SELECT id, vector FROM points ORDER BY rowid").fetchall()
        matrix = np.empty((len(rows), self.dim), dtype=DTYPE)
        ids = []
        for row, (pid, blob) in enumerate(rows):
            if len(blob) != self.dim * 4:
                raise DimensionMismatch(
                    f"point {pid!r} holds {len(blob) // 4} floats, collection dimension is {self.dim}"
                )
            matrix[row] = np.frombuffer(blob, dtype=DTYPE)
            ids.append(pid)
        entry = _Cached(version, ids, matrix)
        _cache_put(self._key, entry)
        return entry

    def _prepare_vector(self, vector, *, normalize: bool) -> np.ndarray:
        try:
            arr = np.asarray(vector, dtype=DTYPE)
        except (TypeError, ValueError) as e:
            raise LocalStoreError(f"vector must be a list of numbers: {e}") from e
        if arr.ndim != 1 or arr.shape[0] != self.dim:
            raise DimensionMismatch(
                f"vector has {arr.size} dimensions, collection expects {self.dim}"
            )
        if not np.all(np.isfinite(arr)):
            raise LocalStoreError("vector contains NaN or infinity")
        if normalize:
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                arr = (arr / norm).astype(DTYPE)
        return arr

    def _encode_point(self, point):
        if not isinstance(point, dict):
            raise LocalStoreError("each point must be an object")
        pid = point.get("id")
        if not isinstance(pid, str) or not pid or len(pid) > MAX_ID_LENGTH:
            raise LocalStoreError(f"point id must be a non-empty string of at most {MAX_ID_LENGTH} chars")
        if "vector" not in point:
            raise LocalStoreError(f"point {pid!r} has no vector")
        vector = self._prepare_vector(point["vector"], normalize=self.metric == "cosine")
        payload = point.get("payload") or {}
        if not isinstance(payload, dict):
            raise LocalStoreError(f"point {pid!r}: payload must be an object")
        try:
            payload_json = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise LocalStoreError(f"point {pid!r}: payload is not JSON-serializable: {e}") from e
        text = point.get("text")
        if text is not None and not isinstance(text, str):
            raise LocalStoreError(f"point {pid!r}: text must be a string")
        return (pid, vector.tobytes(), payload_json, text)

    @staticmethod
    def _decode_row(row, include_vectors):
        pid, payload, text, blob = row
        item = {"id": pid, "payload": json.loads(payload), "text": text}
        if include_vectors:
            item["vector"] = np.frombuffer(blob, dtype=DTYPE).tolist()
        return item
