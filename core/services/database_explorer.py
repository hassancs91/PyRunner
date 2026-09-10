"""
Database explorer service — the read paths behind the Databases Monitor + View
UI (Stage 3), deliberately split from ``database_service`` (lifecycle).

Two connection paths, by design:

- **View (structure + data grid)** connects as the database's OWN scoped role
  inside a ``READ ONLY`` transaction — the viewer physically cannot write, and
  cannot see outside its schema, because Postgres enforces both. ``execute()``
  is the seam the deferred Stage-5 SQL console will reuse with
  ``read_only=False``; the explorer itself only ever passes ``True``.

- **Monitor (activity / sizes / slow queries)** connects as the PROVISIONER
  (``pg_stat_*`` needs cross-role visibility) but every query filters on the
  role names of the ACTIVE WORKSPACE's databases — an Owner/Admin never sees
  another workspace's connections or query text. Query text additionally needs
  the provisioner to hold ``pg_read_all_stats`` (the compose init script grants
  it); without it Postgres masks other roles' text as ``<insufficient
  privilege>`` and the page says why.

Everything here degrades instead of raising: a down server, a missing
``pg_stat_statements`` extension, or a permission gap yields a result object
with ``ok=False``/availability flags so the pages render with a banner, never a
500.
"""

import logging
from dataclasses import dataclass, field

from core.services.database_service import DatabaseService

logger = logging.getLogger(__name__)

# The explorer's own leash, independent of the role's default statement
# timeout: viewer pages should feel instant or fail fast, never hold a slot.
VIEWER_TIMEOUT_MS = 15000
GRID_PAGE_ROWS = 50
CSV_ROW_CAP = 10000
LONG_RUNNING_SECONDS = 30


@dataclass
class QueryResult:
    """Result of ``execute()``: column names, rows, and a truncation flag."""

    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False


class DatabaseExplorerError(Exception):
    """A viewer/monitor read failed (server down, bad table, timeout)."""


class DatabaseExplorerService:
    # ------------------------------------------------------------------
    # The scoped-role execution seam (Stage 5 reuses with read_only=False)
    # ------------------------------------------------------------------

    @classmethod
    def _scoped_connect(cls, database):
        """Connection as the database's own role (patched in tests)."""
        import psycopg

        return psycopg.connect(DatabaseService.scoped_dsn(database), connect_timeout=10)

    @classmethod
    def execute(
        cls,
        database,
        query,
        params=None,
        *,
        read_only=True,
        row_cap=GRID_PAGE_ROWS,
        timeout_ms=VIEWER_TIMEOUT_MS,
    ) -> QueryResult:
        """Run one statement as the database's OWN role.

        ``read_only=True`` (the only mode the explorer uses) marks the session
        read-only BEFORE the first statement, so any write is rejected by
        Postgres itself — not by PyRun inspecting SQL. ``row_cap`` bounds
        the result (``truncated`` reports whether more rows existed);
        ``timeout_ms`` bounds the runtime via ``set_config`` inside the
        session. Raises ``DatabaseExplorerError`` on any server-side failure.
        """
        try:
            with cls._scoped_connect(database) as conn:
                conn.read_only = bool(read_only)
                with conn.cursor() as cur:
                    # set_config (not SET) so the value is parametrizable; it
                    # is legal inside a read-only transaction.
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (str(int(timeout_ms)),),
                    )
                    cur.execute(query, params)
                    if cur.description is None:
                        return QueryResult()
                    columns = [d.name for d in cur.description]
                    if row_cap:
                        rows = cur.fetchmany(int(row_cap) + 1)
                        truncated = len(rows) > int(row_cap)
                        if truncated:
                            rows = rows[: int(row_cap)]
                    else:
                        rows, truncated = cur.fetchall(), False
                    return QueryResult(columns, list(rows), truncated)
        except DatabaseExplorerError:
            raise
        except Exception as e:
            raise DatabaseExplorerError(str(e)) from e

    # ------------------------------------------------------------------
    # View — structure + data, all through the scoped role
    # ------------------------------------------------------------------

    @classmethod
    def tables(cls, database) -> list:
        """The database's tables with row estimates and on-disk sizes.

        ``reltuples`` is the planner's estimate (refreshed by ANALYZE/vacuum) —
        instant on any table size, clearly labeled '~' in the UI. -1 means the
        table has never been analyzed.
        """
        result = cls.execute(
            database,
            """
            SELECT c.relname,
                   GREATEST(c.reltuples, 0)::bigint,
                   pg_total_relation_size(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
            ORDER BY c.relname
            """,
            (database.schema_name,),
            row_cap=None,
        )
        from core.services import DatastoreService

        return [
            {
                "name": name,
                "row_estimate": rows,
                "size_bytes": size,
                "size_display": DatastoreService.format_size(size),
            }
            for name, rows, size in result.rows
        ]

    @classmethod
    def table_or_none(cls, database, table_name: str):
        """The ``tables()`` entry for ``table_name``, or None.

        Every table-addressed page resolves through this first, so a
        user-supplied name is only ever used after it matched a real table in
        the database's OWN schema (and is then quoted as an identifier anyway).
        """
        return next((t for t in cls.tables(database) if t["name"] == table_name), None)

    @classmethod
    def columns(cls, database, table_name: str) -> list:
        result = cls.execute(
            database,
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (database.schema_name, table_name),
            row_cap=None,
        )
        return [
            {
                "name": name,
                "type": dtype,
                "nullable": nullable == "YES",
                "default": default or "",
            }
            for name, dtype, nullable, default in result.rows
        ]

    @classmethod
    def indexes(cls, database, table_name: str) -> list:
        result = cls.execute(
            database,
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            (database.schema_name, table_name),
            row_cap=None,
        )
        return [{"name": n, "definition": d} for n, d in result.rows]

    @classmethod
    def rows(
        cls, database, table_name: str, *, page: int = 1, per_page: int = GRID_PAGE_ROWS
    ) -> QueryResult:
        """One page of a table's rows (unordered — a viewer, not a report).

        The caller must have validated ``table_name`` via ``table_or_none``;
        the identifier is quoted regardless, and the scoped role's search_path
        pins resolution to the database's own schema.
        """
        from psycopg import sql

        page = max(1, int(page))
        per_page = max(1, min(int(per_page), GRID_PAGE_ROWS))
        # LIMIT one past the page so ``truncated`` doubles as "has next page".
        return cls.execute(
            database,
            sql.SQL("SELECT * FROM {} LIMIT {} OFFSET {}").format(
                sql.Identifier(table_name),
                sql.Literal(per_page + 1),
                sql.Literal((page - 1) * per_page),
            ),
            row_cap=per_page,
        )

    @classmethod
    def csv_rows(cls, database, table_name: str) -> QueryResult:
        """Up to CSV_ROW_CAP rows for export (truncated flag tells the user)."""
        from psycopg import sql

        return cls.execute(
            database,
            sql.SQL("SELECT * FROM {} LIMIT {}").format(
                sql.Identifier(table_name), sql.Literal(CSV_ROW_CAP + 1)
            ),
            row_cap=CSV_ROW_CAP,
        )

    # ------------------------------------------------------------------
    # Monitor — provisioner connection, always filtered to the workspace
    # ------------------------------------------------------------------

    @classmethod
    def _workspace_role_map(cls, workspace) -> dict:
        """role_name → Database for the workspace (the monitor's filter set)."""
        from core.models import Database

        return {
            d.role_name: d
            for d in Database.objects.for_workspace(workspace).order_by("name")
        }

    @classmethod
    def stats_for_workspace(cls, workspace) -> dict:
        """Per-database size / table count / live connections for the list page.

        Returns ``{database_id: {...}}``; ``{}`` when the server is
        unreachable or no databases exist (the page renders without metrics).
        """
        role_map = cls._workspace_role_map(workspace)
        if not role_map:
            return {}
        schemas = [d.schema_name for d in role_map.values()]
        roles = list(role_map.keys())

        from core.services import DatastoreService

        stats = {}
        try:
            with DatabaseService._admin_connect() as conn:
                sizes = conn.execute(
                    """
                    SELECT n.nspname,
                           COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint,
                           COUNT(c.oid) FILTER (WHERE c.relkind IN ('r', 'p'))
                    FROM pg_namespace n
                    LEFT JOIN pg_class c ON c.relnamespace = n.oid
                    WHERE n.nspname = ANY(%s)
                    GROUP BY n.nspname
                    """,
                    (schemas,),
                ).fetchall()
                conns = conn.execute(
                    "SELECT usename, COUNT(*) FROM pg_stat_activity "
                    "WHERE usename = ANY(%s) GROUP BY usename",
                    (roles,),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases stats unavailable: %s", e)
            return {}

        size_by_schema = {row[0]: (row[1], row[2]) for row in sizes}
        conn_by_role = {row[0]: row[1] for row in conns}
        for database in role_map.values():
            size_bytes, table_count = size_by_schema.get(database.schema_name, (0, 0))
            stats[database.id] = {
                "size_bytes": size_bytes,
                "size_display": DatastoreService.format_size(size_bytes),
                "table_count": table_count,
                "connections": conn_by_role.get(database.role_name, 0),
            }
        return stats

    @classmethod
    def activity_for_workspace(cls, workspace) -> dict:
        """Live sessions of the workspace's database roles, bucketed for the
        monitor page: active (with runtime), idle-in-transaction, blocked.

        ``ok=False`` + ``error`` when the server can't be reached.
        """
        role_map = cls._workspace_role_map(workspace)
        result = {
            "ok": True,
            "error": "",
            "sessions": [],
            "active": 0,
            "idle_in_transaction": 0,
            "blocked": 0,
            "long_running": 0,
        }
        if not role_map:
            return result
        try:
            with DatabaseService._admin_connect() as conn:
                rows = conn.execute(
                    """
                    SELECT a.pid, a.usename, a.state,
                           EXTRACT(EPOCH FROM (now() - a.query_start))::int,
                           a.wait_event_type,
                           cardinality(pg_blocking_pids(a.pid)),
                           a.query
                    FROM pg_stat_activity a
                    WHERE a.usename = ANY(%s)
                    ORDER BY a.query_start
                    """,
                    (list(role_map.keys()),),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases activity unavailable: %s", e)
            return {**result, "ok": False, "error": str(e)}

        for pid, role, state, age, wait_type, blockers, query in rows:
            database = role_map.get(role)
            session = {
                "pid": pid,
                "database": database.name if database else role,
                "state": state or "",
                "seconds": age if age is not None and age >= 0 else None,
                "wait": wait_type or "",
                "blocked_by": blockers or 0,
                "query": (query or "").strip(),
                "long_running": bool(
                    state == "active" and age and age >= LONG_RUNNING_SECONDS
                ),
            }
            result["sessions"].append(session)
            if session["blocked_by"]:
                result["blocked"] += 1
            if state == "active":
                result["active"] += 1
                if session["long_running"]:
                    result["long_running"] += 1
            elif state == "idle in transaction":
                result["idle_in_transaction"] += 1
        return result

    @classmethod
    def slow_queries_for_workspace(cls, workspace, *, limit: int = 15) -> dict:
        """Top statements by total time from ``pg_stat_statements``, scoped to
        the workspace's roles. Degrades with a reason instead of failing:
        ``available=False`` + ``reason`` when the extension isn't installed or
        the server is unreachable (sandbox_check discipline).
        """
        role_map = cls._workspace_role_map(workspace)
        result = {"available": True, "reason": "", "queries": []}
        if not role_map:
            return result
        try:
            with DatabaseService._admin_connect() as conn:
                installed = conn.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
                ).fetchone()
                if not installed:
                    return {
                        **result,
                        "available": False,
                        "reason": (
                            "pg_stat_statements is not installed in the data "
                            "database. A server admin can enable it with: "
                            "CREATE EXTENSION pg_stat_statements; (the server "
                            "must preload it via shared_preload_libraries)."
                        ),
                    }
                rows = conn.execute(
                    """
                    SELECT r.rolname, s.calls,
                           ROUND(s.total_exec_time::numeric, 1),
                           ROUND(s.mean_exec_time::numeric, 2),
                           s.rows, s.query
                    FROM pg_stat_statements s
                    JOIN pg_roles r ON r.oid = s.userid
                    JOIN pg_database d ON d.oid = s.dbid
                    WHERE r.rolname = ANY(%s) AND d.datname = current_database()
                    ORDER BY s.total_exec_time DESC
                    LIMIT %s
                    """,
                    (list(role_map.keys()), int(limit)),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases slow-query stats unavailable: %s", e)
            return {**result, "available": False, "reason": str(e)}

        result["queries"] = [
            {
                "database": role_map[role].name if role in role_map else role,
                "calls": calls,
                "total_ms": float(total_ms),
                "mean_ms": float(mean_ms),
                "rows": nrows,
                "query": (query or "").strip(),
            }
            for role, calls, total_ms, mean_ms, nrows, query in rows
        ]
        return result
