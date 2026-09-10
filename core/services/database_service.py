"""
Database provisioning service — the only code that talks to the data server
with the provisioner credential (``PYRUNNER_DATA_DB_URL``).

One PyRun ``Database`` = one Postgres schema + one login role that owns it,
on a data server that is deliberately SEPARATE from the core Django database.
Everything here is built on that invariant:

- The role is created ``NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`` with a
  connection limit and a statement timeout, and owns exactly its schema — so
  granted scripts get full DDL+DML inside it and Postgres itself (not PyRun
  code) blocks everything outside it.
- Provisioning is idempotent: an existing role is reconciled (password/limits
  re-stamped) and ``CREATE SCHEMA IF NOT EXISTS`` re-runs are safe, so a
  half-failed provision can simply be retried.
- Fail-closed: any server error is recorded on the row (``status='error'`` +
  ``last_error``) and raised as ``DatabaseProvisionError`` — a database is
  never silently "ready".

All SQL is composed with ``psycopg.sql`` (identifiers/literals are quoted by
the driver, never interpolated). psycopg imports stay lazy so this module is
importable during the settings-time light-import pre-check.
"""

import logging
import os
import re
import secrets
import shutil
import subprocess
import urllib.parse
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)


class DatabaseProvisionError(Exception):
    """A data-server operation failed (provision/deprovision/test)."""


class DatabaseService:
    """Provision, deprovision, and resolve credentials for managed databases."""

    # ------------------------------------------------------------------
    # Configuration / connection
    # ------------------------------------------------------------------

    @classmethod
    def is_configured(cls) -> bool:
        """Whether a data server is attached (``PYRUNNER_DATA_DB_URL`` set)."""
        return bool(settings.PYRUNNER_DATA_DB_URL)

    @classmethod
    def _parsed_url(cls):
        """The admin DSN split into parts. Raises if not configured."""
        if not cls.is_configured():
            raise DatabaseProvisionError(
                "No data server is configured. Set PYRUNNER_DATA_DB_URL to a "
                "Postgres URL (separate database from the core one)."
            )
        return urllib.parse.urlsplit(settings.PYRUNNER_DATA_DB_URL)

    @classmethod
    def server_info(cls) -> dict:
        """Credential-free connection facts for the UI (host/port/dbname)."""
        if not cls.is_configured():
            return {"configured": False}
        parts = cls._parsed_url()
        return {
            "configured": True,
            "host": parts.hostname or "",
            "port": parts.port or 5432,
            "dbname": (parts.path or "/").lstrip("/"),
        }

    @classmethod
    def _admin_connect(cls):
        """Open an autocommit provisioner connection (patched in tests).

        Autocommit on purpose: CREATE/DROP ROLE and friends should each take
        effect immediately so an idempotent retry sees the true server state,
        not a rolled-back transaction's.
        """
        import psycopg

        # connect_timeout: a wrong host/port must fail a page fast, not hang a
        # request for the OS's TCP timeout.
        return psycopg.connect(
            settings.PYRUNNER_DATA_DB_URL, autocommit=True, connect_timeout=10
        )

    # ------------------------------------------------------------------
    # Naming / DSNs
    # ------------------------------------------------------------------

    @classmethod
    def derive_identifier(cls, workspace, name: str) -> str:
        """Derive the schema/role identifier for a new database.

        ``db_<ws8>_<slug>`` — the ``db_`` prefix guarantees a letter start, the
        8-hex workspace fragment keeps workspaces apart server-side, and the
        result is truncated to Postgres's 63-char identifier cap. Stored on the
        row at provision time and never re-derived. On the (rare) collision
        with an existing row, a 6-hex suffix is appended.
        """
        slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "db"
        ws_part = workspace.id.hex[:8] if workspace is not None else "global"
        candidate = f"db_{ws_part}_{slug}"[:63]

        from core.models import Database

        if Database.objects.filter(schema_name=candidate).exists():
            suffix = uuid.uuid4().hex[:6]
            candidate = f"{candidate[: 63 - len(suffix) - 1]}_{suffix}"
        return candidate

    @classmethod
    def scoped_dsn(cls, database) -> str:
        """The DSN a granted script connects with: the database's own role.

        Same server/dbname as the admin URL, but authenticated as the
        schema-owning role — so the connection physically cannot touch other
        schemas or the core database.
        """
        parts = cls._parsed_url()
        user = urllib.parse.quote(database.role_name, safe="")
        password = urllib.parse.quote(database.get_password(), safe="")
        host = parts.hostname or "localhost"
        netloc = f"{user}:{password}@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urllib.parse.urlunsplit(
            ("postgresql", netloc, parts.path, parts.query, "")
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def create_database(
        cls,
        *,
        name: str,
        workspace,
        description: str = "",
        created_by=None,
        owner_plugin: str = None,
        owner_key: str = None,
    ):
        """Create the row and provision it server-side, in that order.

        Returns the ``Database`` row. On a server-side failure the row is kept
        in ``status='error'`` (so the UI can show the cause and offer Retry)
        and ``DatabaseProvisionError`` propagates.
        """
        from core.models import Database

        database = Database(
            name=name,
            workspace=workspace,
            description=description,
            created_by=created_by,
            owner_plugin=owner_plugin,
            owner_key=owner_key,
            schema_name=cls.derive_identifier(workspace, name),
        )
        database.role_name = database.schema_name  # distinct namespaces, one name
        database.set_password(secrets.token_urlsafe(24))
        database.save()

        cls.provision(database)
        return database

    @classmethod
    def provision(cls, database) -> None:
        """Create (or reconcile) the role + schema on the data server."""
        from psycopg import sql

        parts = cls._parsed_url()
        dbname = (parts.path or "/").lstrip("/")
        role = sql.Identifier(database.role_name)
        schema = sql.Identifier(database.schema_name)
        db_ident = sql.Identifier(dbname)
        password = sql.Literal(database.get_password())
        conn_limit = sql.Literal(settings.PYRUNNER_DATA_DB_CONNECTION_LIMIT)

        try:
            with cls._admin_connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (database.role_name,),
                ).fetchone()
                if exists:
                    # Reconcile a retry / password rotation: re-stamp only what
                    # can drift. The NO* attributes are fixed at CREATE and a
                    # non-superuser provisioner may not even mention SUPERUSER
                    # in ALTER ROLE on PG16 ("only roles with the SUPERUSER
                    # attribute may change the SUPERUSER attribute").
                    conn.execute(
                        sql.SQL(
                            "ALTER ROLE {} WITH LOGIN PASSWORD {} CONNECTION LIMIT {}"
                        ).format(role, password, conn_limit)
                    )
                else:
                    conn.execute(
                        sql.SQL(
                            "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER "
                            "NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT {}"
                        ).format(role, password, conn_limit)
                    )

                # PG16+: CREATEROLE no longer implies SET ROLE on created roles
                # (the auto-granted membership has SET FALSE), and CREATE SCHEMA
                # ... AUTHORIZATION needs it. The provisioner holds ADMIN OPTION
                # on the role it just created, so it can grant itself membership
                # (plain GRANT defaults to SET TRUE). On PG15 and older this is
                # equally valid under the legacy CREATEROLE powers. Idempotent.
                conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(role))

                timeout_ms = settings.PYRUNNER_DATA_DB_STATEMENT_TIMEOUT_MS
                if timeout_ms > 0:
                    conn.execute(
                        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                            role, sql.Literal(timeout_ms)
                        )
                    )
                else:
                    conn.execute(
                        sql.SQL("ALTER ROLE {} RESET statement_timeout").format(role)
                    )
                conn.execute(
                    sql.SQL("ALTER ROLE {} SET search_path = {}").format(role, schema)
                )
                conn.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        db_ident, role
                    )
                )
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                        schema, role
                    )
                )
                # Server hygiene (idempotent): PUBLIC gets CONNECT+TEMP on every
                # database by default, so without this ANY role on the server
                # could connect to the data database (catalog metadata is
                # world-readable once connected). Revoking ALL from PUBLIC makes
                # the per-role GRANT CONNECT above the only way in — and covers
                # the legacy pre-PG15 default where PUBLIC could CREATE in the
                # public schema.
                conn.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db_ident)
                )
        except DatabaseProvisionError:
            raise
        except Exception as e:
            cls._record_error(database, f"Provisioning failed: {e}")
            raise DatabaseProvisionError(str(e)) from e

        database.status = database.STATUS_READY
        database.last_error = ""
        database.save(update_fields=["status", "last_error", "updated_at"])
        logger.info(
            "Provisioned database '%s' (schema %s)", database.name, database.schema_name
        )

    @classmethod
    def deprovision(cls, database) -> None:
        """Drop the schema (CASCADE — all data), its role, and live sessions.

        Raises ``DatabaseProvisionError`` on failure so the caller can decide
        whether to keep the row (default) or orphan the server objects.
        """
        from psycopg import sql

        parts = cls._parsed_url()
        dbname = (parts.path or "/").lstrip("/")
        role = sql.Identifier(database.role_name)
        schema = sql.Identifier(database.schema_name)

        try:
            with cls._admin_connect() as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = %s",
                    (database.role_name,),
                )
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(schema)
                )
                # The CONNECT grant is a dependency that blocks DROP ROLE.
                exists = conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (database.role_name,),
                ).fetchone()
                if exists:
                    conn.execute(
                        sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                            sql.Identifier(dbname), role
                        )
                    )
                    conn.execute(sql.SQL("DROP ROLE {}").format(role))
        except DatabaseProvisionError:
            raise
        except Exception as e:
            cls._record_error(database, f"Deprovisioning failed: {e}")
            raise DatabaseProvisionError(str(e)) from e

        logger.info(
            "Deprovisioned database '%s' (schema %s)",
            database.name,
            database.schema_name,
        )

    # ------------------------------------------------------------------
    # Dumps (backup/restore integration — Stage 4)
    # ------------------------------------------------------------------

    # Bounded so a stuck server can't hang a backup run forever.
    DUMP_TIMEOUT_SECONDS = 300

    @classmethod
    def dump_capability(cls) -> tuple:
        """(ok, reason): whether schema dumps/restores can run on this host.

        Needs the postgres client binaries (pg_dump for backup, psql for
        restore — psql because plain dumps carry COPY FROM stdin blocks a
        driver can't replay). The Docker image ships them; a bare-metal host
        without them degrades to metadata-only backups with this reason.
        """
        missing = [b for b in ("pg_dump", "psql") if shutil.which(b) is None]
        if missing:
            return False, (
                f"{' and '.join(missing)} not installed — database backups "
                "include metadata only (install postgresql-client to include data)."
            )
        return True, ""

    @classmethod
    def _client_cmd_env(cls, binary: str, *, user: str, password: str) -> tuple:
        """Common argv prefix + env for a postgres client run.

        The password travels via PGPASSWORD (never argv — argv is visible in
        the process list).
        """
        parts = cls._parsed_url()
        argv = [
            shutil.which(binary) or binary,
            "-h", parts.hostname or "localhost",
            "-p", str(parts.port or 5432),
            "-U", user,
            "-d", (parts.path or "/").lstrip("/"),
        ]
        env = {**os.environ, "PGPASSWORD": password}
        return argv, env

    @classmethod
    def dump_schema(cls, database) -> str:
        """Plain-SQL pg_dump of the database's schema (structure + data).

        Connects as the provisioner (a member of every database role, so it can
        read all schemas); ``--no-owner --no-privileges`` keeps the dump
        replayable by the database's own role on restore.
        """
        ok, reason = cls.dump_capability()
        if not ok:
            raise DatabaseProvisionError(reason)
        parts = cls._parsed_url()
        argv, env = cls._client_cmd_env(
            "pg_dump",
            user=urllib.parse.unquote(parts.username or ""),
            password=urllib.parse.unquote(parts.password or ""),
        )
        argv += ["-n", database.schema_name, "--no-owner", "--no-privileges"]
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env,
            timeout=cls.DUMP_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise DatabaseProvisionError(
                f"pg_dump failed for '{database.name}': {proc.stderr.strip()[:500]}"
            )
        return proc.stdout

    @classmethod
    def restore_schema_dump(cls, database, dump_sql: str) -> None:
        """Replay a ``dump_schema`` dump into a freshly provisioned database.

        Runs psql AS THE DATABASE'S OWN ROLE so every restored object is owned
        by it (full DDL+DML stays with the role, matching a script-created
        table). Two dump lines are stripped first:
        - ``CREATE SCHEMA`` — the schema already exists from provisioning, and
          the role may not create schemas;
        - ``SET transaction_timeout`` — pg_dump 17+ emits this PG17-only GUC
          in its preamble, and replaying onto an older server aborts on it
          ("unrecognized configuration parameter"; found live on PG16).
        ``ON_ERROR_STOP`` keeps a partial replay from passing silently.
        """
        ok, reason = cls.dump_capability()
        if not ok:
            raise DatabaseProvisionError(reason)
        cleaned = "\n".join(
            line
            for line in dump_sql.splitlines()
            if not line.startswith(("CREATE SCHEMA ", "SET transaction_timeout"))
        )
        argv, env = cls._client_cmd_env(
            "psql", user=database.role_name, password=database.get_password()
        )
        argv += ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"]
        proc = subprocess.run(
            argv, input=cleaned, capture_output=True, text=True, env=env,
            timeout=cls.DUMP_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise DatabaseProvisionError(
                f"psql restore failed for '{database.name}': "
                f"{proc.stderr.strip()[:500]}"
            )

    @classmethod
    def test_connection(cls) -> tuple:
        """Probe the data server. Returns ``(ok, human_message)``, never raises."""
        try:
            with cls._admin_connect() as conn:
                version = conn.execute("SHOW server_version").fetchone()[0]
                dbname = conn.execute("SELECT current_database()").fetchone()[0]
                # Surfaced now because the Stage 3 explorer's slow-query view
                # needs it; lets admins fix server config before that ships.
                pss = conn.execute(
                    "SELECT 1 FROM pg_available_extensions "
                    "WHERE name = 'pg_stat_statements'"
                ).fetchone()
            pss_note = (
                "pg_stat_statements available"
                if pss
                else "pg_stat_statements not available (slow-query stats will be limited)"
            )
            return True, f"Connected to '{dbname}' (PostgreSQL {version}); {pss_note}."
        except DatabaseProvisionError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Connection failed: {e}"

    # ------------------------------------------------------------------

    @classmethod
    def _record_error(cls, database, message: str) -> None:
        """Persist a failure on the row; never masks the original error."""
        try:
            database.status = database.STATUS_ERROR
            database.last_error = message
            database.save(update_fields=["status", "last_error", "updated_at"])
        except Exception:
            logger.exception(
                "Could not record provisioning error on database %s", database.pk
            )
