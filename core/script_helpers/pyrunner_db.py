"""
PyRunner Databases API for scripts.

Connect to a managed SQL database (a Postgres schema provisioned in the
PyRunner UI and granted to this script). Unlike ``pyrunner_datastore`` — a
simple key-value store — this hands you a real PostgreSQL connection: joins,
indexes, transactions, and the whole Python DB ecosystem work as normal.

How it works: this module asks PyRunner's internal API (loopback, signed
per-run token) for the database's scoped credentials, then connects DIRECTLY
to the data server as the database's own role. That role owns exactly one
schema, so your queries live in your database's namespace automatically
(``search_path`` is preset) — and cannot touch anything else.

Usage:
    import pyrunner_db

    # Best with a context manager: commits on success, rolls back on error
    with pyrunner_db.connect("crm") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS leads (id serial PRIMARY KEY, email text)")
        conn.execute("INSERT INTO leads (email) VALUES (%s)", ("a@b.co",))
        rows = conn.execute("SELECT * FROM leads").fetchall()

    # Or take the DSN / SQLAlchemy URL and use any client you like
    dsn = pyrunner_db.dsn("crm")                       # postgresql://...
    engine_url = pyrunner_db.sqlalchemy_url("crm")     # postgresql+psycopg://...

    # Which databases has this script been granted?
    names = pyrunner_db.list_databases()

Requirements: the PyRunner instance must have a data server attached
(PYRUNNER_DATA_DB_URL), the database must exist, and it must be granted to
this script on its page in the PyRunner UI.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple


class PyRunnerDbError(RuntimeError):
    """A database-credential operation failed against PyRunner's internal API.

    Raised for server/auth/config failures (5xx, 401/403, 409 not-ready, 503
    not-configured, unreachable API). A missing or ungranted database stays a
    ``ValueError``, mirroring ``pyrunner_datastore``'s missing-store contract.
    """


# Scoped-DSN cache: several connect() calls in one run shouldn't re-resolve.
_dsn_cache: Dict[str, str] = {}


def _api_base() -> Tuple[str, str]:
    url = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not url or not token:
        raise PyRunnerDbError(
            "PyRunner database access is not configured (no PYRUNNER_INTERNAL_URL/"
            "PYRUNNER_INTERNAL_TOKEN). This module must be run from PyRunner."
        )
    return url.rstrip("/"), token


def _request(path: str) -> Tuple[int, Any]:
    base, token = _api_base()
    url = f"{base}{path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return e.code, payload
    except urllib.error.URLError as e:
        raise PyRunnerDbError(
            f"Could not reach PyRunner's internal API (GET {url}): {e.reason}"
        ) from e


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
    return fallback


def dsn(name: str) -> str:
    """Return the scoped PostgreSQL DSN for a granted database.

    Args:
        name: The database's name in PyRunner.

    Raises:
        ValueError: The database does not exist or is not granted to this script.
        PyRunnerDbError: The data server is not attached, the database is not
            ready, or the internal API failed.
    """
    if name in _dsn_cache:
        return _dsn_cache[name]

    status, payload = _request(f"/internal/databases/{urllib.parse.quote(name)}/dsn")
    if status == 404:
        raise ValueError(
            _error_message(
                payload,
                f"Database '{name}' does not exist or is not granted to this script.",
            )
        )
    if status != 200:
        raise PyRunnerDbError(
            _error_message(
                payload,
                f"Could not resolve database '{name}': internal API returned HTTP {status}.",
            )
        )
    _dsn_cache[name] = payload["dsn"]
    return _dsn_cache[name]


def connect(name: str, **kwargs):
    """Open a psycopg connection to a granted database.

    Extra keyword arguments go straight to ``psycopg.connect`` (for example
    ``autocommit=True`` or ``row_factory=psycopg.rows.dict_row``).
    """
    try:
        import psycopg
    except ImportError as e:
        raise PyRunnerDbError(
            "The 'psycopg' package is not installed in this script's environment. "
            "Add 'psycopg[binary]' under Environments -> (this environment) -> "
            "Packages, then re-run. (pyrunner_db.dsn() / sqlalchemy_url() work "
            "without it if you bring your own driver.)"
        ) from e

    return psycopg.connect(dsn(name), **kwargs)


def sqlalchemy_url(name: str) -> str:
    """The database's DSN in SQLAlchemy form (``postgresql+psycopg://...``).

    Feed it to ``sqlalchemy.create_engine`` or ``pandas.read_sql``. This module
    never imports SQLAlchemy itself.
    """
    return dsn(name).replace("postgresql://", "postgresql+psycopg://", 1)


def list_databases() -> List[str]:
    """Names of the databases granted to this script."""
    status, payload = _request("/internal/databases")
    if status != 200:
        raise PyRunnerDbError(
            _error_message(
                payload, f"Could not list databases: internal API returned HTTP {status}."
            )
        )
    return [d["name"] for d in payload.get("databases", [])]
