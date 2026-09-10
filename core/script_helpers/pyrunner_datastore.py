"""
PyRun DataStore API for scripts.

This module provides a simple key-value data store interface. Data stores must
be created through the PyRun web interface before they can be used in scripts.

The backend is chosen automatically from the environment PyRun injects:
- ``PYRUNNER_DB_PATH`` set (SQLite deployments) -> read the DB file directly.
  This is the original, web-server-independent path, byte-for-byte unchanged.
- otherwise ``PYRUNNER_INTERNAL_URL`` + ``PYRUNNER_INTERNAL_TOKEN`` (Postgres
  deployments, where there is no local DB file) -> a small loopback HTTP call to
  PyRun's internal API, authenticated by a signed per-run token.

Either way the surface below is identical, so scripts never change.

Usage:
    from pyrunner_datastore import DataStore

    # Open a data store (must exist in PyRun UI)
    store = DataStore("my_store")

    # Store values (any JSON-serializable type)
    store["key"] = "value"
    store["config"] = {"retries": 3, "timeout": 30}
    store["results"] = [1, 2, 3, 4, 5]

    # Retrieve values
    value = store["key"]
    value = store.get("key", default=None)

    # Check existence
    if "key" in store:
        print(store["key"])

    # Delete
    del store["key"]

    # Iterate
    for key, value in store.items():
        print(f"{key}: {value}")

    # Utilities
    store.keys()    # List all keys
    store.values()  # List all values
    store.clear()   # Delete all entries
    len(store)      # Entry count
"""

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, List, Optional, Tuple


class DataStoreError(RuntimeError):
    """A datastore operation failed against PyRun's internal API.

    Raised for server/auth failures (5xx, 401/403, unreachable API) so a failed
    write or read is never silently swallowed — matching the SQLite backend,
    which propagates its errors. Subclasses ``RuntimeError`` so existing
    ``except RuntimeError`` handlers still catch it. (A missing store/key stays a
    ``ValueError``/``KeyError`` as before.)
    """


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _SqliteBackend:
    """Direct SQLite access (original behavior; no web-server dependency)."""

    def __init__(self, name: str, db_path: str):
        self.name = name
        self._db_path = db_path
        self._store_id = self._get_store_id()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_store_id(self) -> Optional[str]:
        # Tenancy Stage 2: names are unique per workspace, so scope the lookup to
        # the run's workspace (injected as PYRUNNER_WORKSPACE_ID, the 32-char hex
        # of the workspace UUID — how Django stores the FK on SQLite). Fall back to
        # a still-unassigned (NULL-workspace) store for stores created before
        # scoping, so a single-workspace instance is unchanged.
        ws_hex = os.environ.get("PYRUNNER_WORKSPACE_ID")
        with self._conn() as conn:
            if ws_hex:
                row = conn.execute(
                    "SELECT id FROM datastores WHERE name = ? AND workspace_id = ?",
                    (self.name, ws_hex),
                ).fetchone()
                if row is not None:
                    return row["id"]
            row = conn.execute(
                "SELECT id FROM datastores WHERE name = ? AND workspace_id IS NULL",
                (self.name,),
            ).fetchone()
            return row["id"] if row else None

    def store_exists(self) -> bool:
        return self._store_id is not None

    def get(self, key: str) -> Any:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value_json FROM datastore_entries "
                "WHERE datastore_id = ? AND key = ?",
                (self._store_id, key),
            ).fetchone()
            if row is None:
                raise KeyError(key)
            return json.loads(row["value_json"])

    def set(self, key: str, value: Any) -> None:
        value_json = json.dumps(value)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO datastore_entries (id, datastore_id, key, value_json, created_at, updated_at)
                VALUES (lower(hex(randomblob(16))), ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(datastore_id, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = datetime('now')
                """,
                (self._store_id, key, value_json),
            )
            conn.commit()

    def delete(self, key: str) -> None:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM datastore_entries WHERE datastore_id = ? AND key = ?",
                (self._store_id, key),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise KeyError(key)

    def contains(self, key: str) -> bool:
        with self._conn() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM datastore_entries WHERE datastore_id = ? AND key = ?",
                    (self._store_id, key),
                ).fetchone()
                is not None
            )

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) as count FROM datastore_entries WHERE datastore_id = ?",
                (self._store_id,),
            ).fetchone()["count"]

    def keys(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,),
            ).fetchall()
            return [r["key"] for r in rows]

    def values(self) -> List[Any]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT value_json FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,),
            ).fetchall()
            return [json.loads(r["value_json"]) for r in rows]

    def items(self) -> List[Tuple[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value_json FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,),
            ).fetchall()
            return [(r["key"], json.loads(r["value_json"])) for r in rows]

    def clear(self) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM datastore_entries WHERE datastore_id = ?",
                (self._store_id,),
            )
            conn.commit()
            return cursor.rowcount


class _ApiBackend:
    """Loopback HTTP access to PyRun's internal datastore API (stdlib only)."""

    def __init__(self, name: str, base_url: str, token: str):
        self.name = name
        self._token = token
        self._ds_url = f"{base_url.rstrip('/')}/internal/datastores/{urllib.parse.quote(name)}"

    def _request(self, method: str, url: str, body: Any = None) -> Tuple[int, dict]:
        data = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status, payload = resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            status = e.code
        except urllib.error.URLError as e:
            # Connection-level failure (API down, DNS, timeout): never silent.
            raise DataStoreError(
                f"DataStore '{self.name}': could not reach PyRun's internal "
                f"API ({method} {url}): {e.reason}"
            ) from e

        # 2xx and 404 are meaningful to callers (404 => missing store/key, mapped
        # to ValueError/KeyError). Any other status is a server/auth failure that
        # must NOT pass silently (e.g. a 401 from an expired token on a long run,
        # or a 500) — otherwise a failed set()/clear() looks like success.
        if not (200 <= status < 300 or status == 404):
            detail = payload.get("error") or payload.get("detail") or ""
            raise DataStoreError(
                f"DataStore '{self.name}': internal API returned HTTP {status}"
                + (f" ({detail})" if detail else "")
                + f" for {method} {url}"
            )
        return status, payload

    def _entry_url(self, key: str) -> str:
        return f"{self._ds_url}/entry?key={urllib.parse.quote(str(key))}"

    def store_exists(self) -> bool:
        status, _ = self._request("GET", self._ds_url)
        return status == 200

    def get(self, key: str) -> Any:
        status, payload = self._request("GET", self._entry_url(key))
        if status == 404:
            raise KeyError(key)
        return payload["value"]

    def set(self, key: str, value: Any) -> None:
        self._request("PUT", f"{self._ds_url}/entry", body={"key": key, "value": value})

    def delete(self, key: str) -> None:
        status, _ = self._request("DELETE", self._entry_url(key))
        if status == 404:
            raise KeyError(key)

    def contains(self, key: str) -> bool:
        status, _ = self._request("GET", self._entry_url(key))
        return status == 200

    def _all(self) -> list:
        _, payload = self._request("GET", f"{self._ds_url}/entries")
        return payload.get("entries", [])

    def count(self) -> int:
        _, payload = self._request("GET", f"{self._ds_url}/entries")
        return int(payload.get("count", 0))

    def keys(self) -> List[str]:
        return [e["key"] for e in self._all()]

    def values(self) -> List[Any]:
        return [e["value"] for e in self._all()]

    def items(self) -> List[Tuple[str, Any]]:
        return [(e["key"], e["value"]) for e in self._all()]

    def clear(self) -> int:
        _, payload = self._request("DELETE", f"{self._ds_url}/entries")
        return int(payload.get("deleted", 0))


def _make_backend(name: str):
    """Pick the datastore backend from the environment PyRun injected.

    SQLite (``PYRUNNER_DB_PATH``) takes precedence so the default deployment is
    unchanged and web-server-independent; the loopback API is used when there is
    no local DB file (Postgres).
    """
    db_path = os.environ.get("PYRUNNER_DB_PATH")
    if db_path:
        return _SqliteBackend(name, db_path)

    api_url = os.environ.get("PYRUNNER_INTERNAL_URL")
    api_token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if api_url and api_token:
        return _ApiBackend(name, api_url, api_token)

    raise RuntimeError(
        "PyRun datastore is not configured (no PYRUNNER_DB_PATH or "
        "PYRUNNER_INTERNAL_URL). This module must be run from PyRun."
    )


class DataStore:
    """
    Simple key-value data store. Provides dict-like access to stored data.
    """

    def __init__(self, name: str):
        """
        Open a data store by name.

        Args:
            name: The name of the data store (must exist in PyRun)

        Raises:
            RuntimeError: If PyRun did not inject a datastore configuration
            ValueError: If the data store does not exist
        """
        self.name = name
        self._backend = _make_backend(name)
        if not self._backend.store_exists():
            raise ValueError(
                f"Data store '{name}' does not exist. Create it in the PyRun UI first."
            )

    def __getitem__(self, key: str) -> Any:
        """Get a value by key. Raises KeyError if the key does not exist."""
        return self._backend.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value. Creates or updates the entry."""
        self._backend.set(key, value)

    def __delitem__(self, key: str) -> None:
        """Delete a key. Raises KeyError if the key does not exist."""
        self._backend.delete(key)

    def __contains__(self, key: str) -> bool:
        """Check if a key exists."""
        return self._backend.contains(key)

    def __len__(self) -> int:
        """Return the number of entries."""
        return self._backend.count()

    def __iter__(self) -> Iterator[str]:
        """Iterate over keys."""
        return iter(self.keys())

    def __repr__(self) -> str:
        return f"DataStore('{self.name}')"

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value with a default if not found."""
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> List[str]:
        """Return all keys in the data store."""
        return self._backend.keys()

    def values(self) -> List[Any]:
        """Return all values in the data store."""
        return self._backend.values()

    def items(self) -> List[Tuple[str, Any]]:
        """Return all key-value pairs."""
        return self._backend.items()

    def clear(self) -> int:
        """Delete all entries in the data store. Returns the count deleted."""
        return self._backend.clear()

    def setdefault(self, key: str, default: Any = None) -> Any:
        """Get a value, setting it to default if it doesn't exist."""
        try:
            return self[key]
        except KeyError:
            self[key] = default
            return default

    def update(self, other: dict = None, **kwargs) -> None:
        """Update the data store with key-value pairs."""
        if other:
            for key, value in other.items():
                self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def pop(self, key: str, *default) -> Any:
        """Remove and return a value, or the default if provided."""
        try:
            value = self[key]
            del self[key]
            return value
        except KeyError:
            if default:
                return default[0]
            raise
