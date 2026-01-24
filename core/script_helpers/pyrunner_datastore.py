"""
PyRunner DataStore API for scripts.

This module provides a simple key-value data store interface backed by
the PyRunner SQLite database. Data stores must be created through the
PyRunner web interface before they can be used in scripts.

Usage:
    from pyrunner_datastore import DataStore

    # Open a data store (must exist in PyRunner UI)
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
from typing import Any, Iterator, List, Optional, Tuple


class DataStore:
    """
    Simple key-value data store backed by SQLite.
    Provides dict-like access to stored data.
    """

    def __init__(self, name: str):
        """
        Open a data store by name.

        Args:
            name: The name of the data store (must exist in PyRunner)

        Raises:
            RuntimeError: If PYRUNNER_DB_PATH is not set
            ValueError: If the data store does not exist
        """
        self.name = name
        self._db_path = os.environ.get("PYRUNNER_DB_PATH")
        if not self._db_path:
            raise RuntimeError(
                "PYRUNNER_DB_PATH not set. This module must be run from PyRunner."
            )

        # Verify the data store exists
        self._store_id = self._get_store_id()
        if not self._store_id:
            raise ValueError(f"Data store '{name}' does not exist. Create it in the PyRunner UI first.")

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_store_id(self) -> Optional[str]:
        """Get the UUID of this data store."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id FROM datastores WHERE name = ?",
                (self.name,)
            )
            row = cursor.fetchone()
            return row["id"] if row else None

    def __getitem__(self, key: str) -> Any:
        """
        Get a value by key.

        Args:
            key: The key to retrieve

        Returns:
            The stored value (deserialized from JSON)

        Raises:
            KeyError: If the key does not exist
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT value_json FROM datastore_entries "
                "WHERE datastore_id = ? AND key = ?",
                (self._store_id, key)
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(key)
            return json.loads(row["value_json"])

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Set a value. Creates or updates the entry.

        Args:
            key: The key to set
            value: Any JSON-serializable value
        """
        value_json = json.dumps(value)
        with self._get_connection() as conn:
            # Use INSERT OR REPLACE for SQLite upsert
            conn.execute(
                """
                INSERT INTO datastore_entries (id, datastore_id, key, value_json, created_at, updated_at)
                VALUES (lower(hex(randomblob(16))), ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(datastore_id, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = datetime('now')
                """,
                (self._store_id, key, value_json)
            )
            conn.commit()

    def __delitem__(self, key: str) -> None:
        """
        Delete a key.

        Args:
            key: The key to delete

        Raises:
            KeyError: If the key does not exist
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM datastore_entries WHERE datastore_id = ? AND key = ?",
                (self._store_id, key)
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        """
        Check if a key exists.

        Args:
            key: The key to check

        Returns:
            True if the key exists, False otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM datastore_entries WHERE datastore_id = ? AND key = ?",
                (self._store_id, key)
            )
            return cursor.fetchone() is not None

    def __len__(self) -> int:
        """
        Return the number of entries.

        Returns:
            The count of entries in this data store
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM datastore_entries WHERE datastore_id = ?",
                (self._store_id,)
            )
            return cursor.fetchone()["count"]

    def __iter__(self) -> Iterator[str]:
        """
        Iterate over keys.

        Yields:
            Each key in the data store
        """
        return iter(self.keys())

    def __repr__(self) -> str:
        return f"DataStore('{self.name}')"

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a value with a default if not found.

        Args:
            key: The key to retrieve
            default: Value to return if key doesn't exist (default: None)

        Returns:
            The stored value or the default
        """
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> List[str]:
        """
        Return all keys in the data store.

        Returns:
            A list of all keys
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT key FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,)
            )
            return [row["key"] for row in cursor.fetchall()]

    def values(self) -> List[Any]:
        """
        Return all values in the data store.

        Returns:
            A list of all values (deserialized from JSON)
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT value_json FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,)
            )
            return [json.loads(row["value_json"]) for row in cursor.fetchall()]

    def items(self) -> List[Tuple[str, Any]]:
        """
        Return all key-value pairs.

        Returns:
            A list of (key, value) tuples
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT key, value_json FROM datastore_entries WHERE datastore_id = ? ORDER BY key",
                (self._store_id,)
            )
            return [(row["key"], json.loads(row["value_json"])) for row in cursor.fetchall()]

    def clear(self) -> int:
        """
        Delete all entries in the data store.

        Returns:
            The number of entries deleted
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM datastore_entries WHERE datastore_id = ?",
                (self._store_id,)
            )
            conn.commit()
            return cursor.rowcount

    def setdefault(self, key: str, default: Any = None) -> Any:
        """
        Get a value, setting it to default if it doesn't exist.

        Args:
            key: The key to get/set
            default: Value to set if key doesn't exist (default: None)

        Returns:
            The existing value or the newly set default
        """
        try:
            return self[key]
        except KeyError:
            self[key] = default
            return default

    def update(self, other: dict = None, **kwargs) -> None:
        """
        Update the data store with key-value pairs.

        Args:
            other: A dictionary of key-value pairs
            **kwargs: Additional key-value pairs
        """
        if other:
            for key, value in other.items():
                self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def pop(self, key: str, *default) -> Any:
        """
        Remove and return a value.

        Args:
            key: The key to remove
            default: Optional default value if key doesn't exist

        Returns:
            The removed value or the default

        Raises:
            KeyError: If the key doesn't exist and no default is provided
        """
        try:
            value = self[key]
            del self[key]
            return value
        except KeyError:
            if default:
                return default[0]
            raise
