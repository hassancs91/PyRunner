"""
Databases (managed Postgres for scripts & plugins) — Stage 1.

Covers the full seam without a real data server:
- identifier derivation (63-char cap, sanitization, collision suffix)
- provisioning/deprovisioning SQL + fail-closed status recording (fake psycopg
  connection — asserts the statements the provisioner actually issues)
- scoped-DSN construction (role credentials, URL-encoding)
- the internal loopback API: token auth, explicit-grant-only resolution,
  workspace scoping, not-ready/not-configured contract
- the pyrunner_db helper's exception contract (ValueError vs PyRunnerDbError)
- run-env hardening: the provisioner DSN never reaches a script's environment
- cpanel views: Owner/Admin gating, grant reconcile, typed-name delete
"""

import os
import re
import uuid
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    Database,
    DatabaseGrant,
    Environment,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.script_helpers import pyrunner_db
from core.services.database_service import DatabaseProvisionError, DatabaseService
from core.services.datastore_token import mint_datastore_token

_TEST_KEY = Fernet.generate_key().decode()
_SERVER_URL = "postgres://provisioner:adminpw@dbhost:5432/pyrunner_data"


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _sql_text(query) -> str:
    """Best-effort text of a psycopg.sql object or plain string.

    ``as_string(None)`` needs no connection on newer psycopg; older releases
    raise for Identifier — repr() still contains every fragment, which is all
    the substring assertions below need.
    """
    if isinstance(query, str):
        return query
    try:
        return query.as_string(None)
    except Exception:
        return repr(query)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Records executed SQL; parametrizable role-exists probe and failure."""

    def __init__(self, role_exists=False, fail_on=None):
        self.executed = []
        self.role_exists = role_exists
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        text = _sql_text(query)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("simulated server failure")
        self.executed.append(text)
        if "pg_roles" in text:
            return _FakeResult((1,) if self.role_exists else None)
        return _FakeResult(None)

    def joined(self):
        return "\n".join(self.executed)


def _make_database(workspace, name="crm", status=Database.STATUS_READY, password="pw"):
    # schema/role names are globally unique (one server-side namespace), so the
    # fixture mirrors the real derivation: sanitized slug + a unique fragment.
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower())
    ident = f"db_test_{slug}_{uuid.uuid4().hex[:6]}"
    db = Database(
        name=name,
        workspace=workspace,
        schema_name=ident,
        role_name=ident,
        status=status,
    )
    db.set_password(password)
    db.save()
    return db


# ---------------------------------------------------------------------------
# Naming / DSN
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class IdentifierDerivationTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")

    def test_prefix_workspace_and_slug(self):
        ident = DatabaseService.derive_identifier(self.ws, "My-CRM Leads")
        self.assertEqual(ident, f"db_{self.ws.id.hex[:8]}_my_crm_leads")

    def test_capped_at_63_chars(self):
        ident = DatabaseService.derive_identifier(self.ws, "x" * 200)
        self.assertLessEqual(len(ident), 63)
        self.assertTrue(ident.startswith("db_"))

    def test_collision_gets_suffix(self):
        first = DatabaseService.derive_identifier(self.ws, "crm")
        Database.objects.create(
            name="crm",
            workspace=self.ws,
            schema_name=first,
            role_name=first,
            encrypted_password="x",
        )
        second = DatabaseService.derive_identifier(self.ws, "crm")
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith(first[: 63 - 7]))
        self.assertLessEqual(len(second), 63)

    def test_empty_slug_falls_back(self):
        ident = DatabaseService.derive_identifier(self.ws, "---")
        self.assertEqual(ident, f"db_{self.ws.id.hex[:8]}_db")


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ScopedDsnTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")

    def test_role_credentials_same_server(self):
        db = _make_database(self.ws, password="s3cret")
        self.assertEqual(
            DatabaseService.scoped_dsn(db),
            f"postgresql://{db.role_name}:s3cret@dbhost:5432/pyrunner_data",
        )

    def test_password_is_url_encoded(self):
        db = _make_database(self.ws, password="p@ss/w:rd")
        dsn = DatabaseService.scoped_dsn(db)
        self.assertIn("p%40ss%2Fw%3Ard@dbhost", dsn)

    def test_unconfigured_raises(self):
        db = _make_database(self.ws)
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            with self.assertRaises(DatabaseProvisionError):
                DatabaseService.scoped_dsn(db)

    def test_password_round_trips_encrypted(self):
        db = _make_database(self.ws, password="round-trip")
        self.assertEqual(db.get_password(), "round-trip")
        self.assertNotIn("round-trip", db.encrypted_password)


# ---------------------------------------------------------------------------
# Provisioning / deprovisioning
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ProvisionTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.db = _make_database(self.ws, status=Database.STATUS_PROVISIONING)

    def _provision(self, conn):
        with mock.patch.object(DatabaseService, "_admin_connect", return_value=conn):
            DatabaseService.provision(self.db)

    def test_new_role_full_sequence(self):
        conn = _FakeConn(role_exists=False)
        self._provision(conn)
        joined = conn.joined()
        self.assertIn("CREATE ROLE", joined)
        self.assertIn("NOSUPERUSER", joined)
        self.assertIn("NOCREATEDB", joined)
        self.assertIn("NOCREATEROLE", joined)
        self.assertIn("CONNECTION LIMIT", joined)
        # PG16 CREATEROLE semantics: without self-granting the new role, the
        # AUTHORIZATION clause below fails with "must be able to SET ROLE".
        self.assertIn("TO CURRENT_USER", joined)
        self.assertIn("statement_timeout", joined)
        self.assertIn("search_path", joined)
        self.assertIn("GRANT CONNECT ON DATABASE", joined)
        self.assertIn("CREATE SCHEMA IF NOT EXISTS", joined)
        self.assertIn("AUTHORIZATION", joined)
        self.assertIn("REVOKE ALL ON DATABASE", joined)
        self.assertIn("FROM PUBLIC", joined)
        self.db.refresh_from_db()
        self.assertEqual(self.db.status, Database.STATUS_READY)
        self.assertEqual(self.db.last_error, "")

    def test_existing_role_is_reconciled_not_recreated(self):
        conn = _FakeConn(role_exists=True)
        self._provision(conn)
        joined = conn.joined()
        self.assertNotIn("CREATE ROLE", joined)
        self.assertIn("ALTER ROLE", joined)
        self.db.refresh_from_db()
        self.assertEqual(self.db.status, Database.STATUS_READY)

    def test_statement_timeout_zero_resets(self):
        conn = _FakeConn()
        with override_settings(PYRUNNER_DATA_DB_STATEMENT_TIMEOUT_MS=0):
            self._provision(conn)
        self.assertIn("RESET statement_timeout", conn.joined())

    def test_failure_records_error_and_raises(self):
        conn = _FakeConn(fail_on="CREATE SCHEMA")
        with self.assertRaises(DatabaseProvisionError):
            self._provision(conn)
        self.db.refresh_from_db()
        self.assertEqual(self.db.status, Database.STATUS_ERROR)
        self.assertIn("simulated server failure", self.db.last_error)

    def test_create_database_orchestration(self):
        with mock.patch.object(DatabaseService, "provision") as provision:
            db = DatabaseService.create_database(name="events", workspace=self.ws)
        provision.assert_called_once_with(db)
        self.assertEqual(db.role_name, db.schema_name)
        self.assertTrue(db.schema_name.startswith("db_"))
        # A real generated password, encrypted at rest.
        self.assertGreaterEqual(len(db.get_password()), 24)


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class DeprovisionTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.db = _make_database(self.ws)

    def test_drops_sessions_schema_and_role(self):
        conn = _FakeConn(role_exists=True)
        with mock.patch.object(DatabaseService, "_admin_connect", return_value=conn):
            DatabaseService.deprovision(self.db)
        joined = conn.joined()
        self.assertIn("pg_terminate_backend", joined)
        self.assertIn("DROP SCHEMA IF EXISTS", joined)
        self.assertIn("CASCADE", joined)
        self.assertIn("REVOKE ALL ON DATABASE", joined)
        self.assertIn("DROP ROLE", joined)

    def test_missing_role_skips_drop_role(self):
        conn = _FakeConn(role_exists=False)
        with mock.patch.object(DatabaseService, "_admin_connect", return_value=conn):
            DatabaseService.deprovision(self.db)
        self.assertNotIn("DROP ROLE", conn.joined())

    def test_failure_records_error_and_raises(self):
        conn = _FakeConn(fail_on="DROP SCHEMA")
        with mock.patch.object(DatabaseService, "_admin_connect", return_value=conn):
            with self.assertRaises(DatabaseProvisionError):
                DatabaseService.deprovision(self.db)
        self.db.refresh_from_db()
        self.assertEqual(self.db.status, Database.STATUS_ERROR)


# ---------------------------------------------------------------------------
# Internal loopback API
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class InternalApiTests(TestCase):
    def setUp(self):
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.env = Environment.objects.create(name="e", path="dbint")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws_a
        )
        self.run = Run.objects.create(script=self.script, workspace=self.ws_a)
        self.token = mint_datastore_token(self.run.id)
        self.db = _make_database(self.ws_a, password="dbpw")

    def _dsn_url(self, name="crm"):
        return reverse("internal:databases_dsn", args=[name])

    def _grant(self, active=True):
        return DatabaseGrant.objects.create(
            script=self.script, database=self.db, active=active
        )

    def test_missing_token_is_401(self):
        self.assertEqual(self.client.get(self._dsn_url()).status_code, 401)

    def test_non_loopback_is_403(self):
        resp = self.client.get(
            self._dsn_url(), REMOTE_ADDR="10.0.0.5", **_auth(self.token)
        )
        self.assertEqual(resp.status_code, 403)

    def test_granted_script_gets_scoped_dsn(self):
        self._grant()
        resp = self.client.get(self._dsn_url(), **_auth(self.token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(self.db.role_name, body["dsn"])
        self.assertIn("dbpw", body["dsn"])
        self.assertEqual(body["schema"], self.db.schema_name)

    def test_ungranted_script_is_404(self):
        resp = self.client.get(self._dsn_url(), **_auth(self.token))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "DATABASE_NOT_FOUND")

    def test_inactive_grant_is_404(self):
        self._grant(active=False)
        resp = self.client.get(self._dsn_url(), **_auth(self.token))
        self.assertEqual(resp.status_code, 404)

    def test_cross_workspace_database_is_404(self):
        # Same name in another workspace, granted there — this run can't see it.
        other = _make_database(self.ws_b, name="reports")
        resp = self.client.get(self._dsn_url("reports"), **_auth(self.token))
        self.assertEqual(resp.status_code, 404)
        # Even a (stale/misconfigured) grant row can't cross workspaces: the
        # by-name resolve scopes to the run's workspace first.
        DatabaseGrant.objects.create(script=self.script, database=other)
        resp = self.client.get(self._dsn_url("reports"), **_auth(self.token))
        self.assertEqual(resp.status_code, 404)

    def test_not_ready_is_409(self):
        self.db.status = Database.STATUS_ERROR
        self.db.save(update_fields=["status"])
        self._grant()
        resp = self.client.get(self._dsn_url(), **_auth(self.token))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "DATABASE_NOT_READY")

    def test_unconfigured_is_503(self):
        self._grant()
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            resp = self.client.get(self._dsn_url(), **_auth(self.token))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "NOT_CONFIGURED")

    def test_list_returns_only_granted(self):
        self._grant()
        _make_database(self.ws_a, name="other")  # exists, not granted
        resp = self.client.get(
            reverse("internal:databases_list"), **_auth(self.token)
        )
        self.assertEqual(resp.status_code, 200)
        names = [d["name"] for d in resp.json()["databases"]]
        self.assertEqual(names, ["crm"])


# ---------------------------------------------------------------------------
# pyrunner_db helper contract
# ---------------------------------------------------------------------------


class HelperContractTests(TestCase):
    def setUp(self):
        pyrunner_db._dsn_cache.clear()
        self.addCleanup(pyrunner_db._dsn_cache.clear)

    def test_no_env_raises_pyrunner_db_error(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PYRUNNER_INTERNAL_URL", "PYRUNNER_INTERNAL_TOKEN")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(pyrunner_db.PyRunnerDbError):
                pyrunner_db.dsn("crm")

    def test_404_maps_to_value_error(self):
        payload = {"error": {"code": "DATABASE_NOT_FOUND", "message": "no crm"}}
        with mock.patch.object(pyrunner_db, "_request", return_value=(404, payload)):
            with self.assertRaises(ValueError) as ctx:
                pyrunner_db.dsn("crm")
        self.assertIn("no crm", str(ctx.exception))

    def test_409_and_503_map_to_db_error(self):
        for status, code in ((409, "DATABASE_NOT_READY"), (503, "NOT_CONFIGURED")):
            with self.subTest(status=status):
                payload = {"error": {"code": code, "message": "nope"}}
                with mock.patch.object(
                    pyrunner_db, "_request", return_value=(status, payload)
                ):
                    with self.assertRaises(pyrunner_db.PyRunnerDbError):
                        pyrunner_db.dsn("crm")

    def test_dsn_resolves_and_caches(self):
        payload = {"name": "crm", "dsn": "postgresql://r:p@h:5432/d", "schema": "s"}
        with mock.patch.object(
            pyrunner_db, "_request", return_value=(200, payload)
        ) as req:
            self.assertEqual(pyrunner_db.dsn("crm"), "postgresql://r:p@h:5432/d")
            self.assertEqual(pyrunner_db.dsn("crm"), "postgresql://r:p@h:5432/d")
        req.assert_called_once()

    def test_sqlalchemy_url_scheme(self):
        payload = {"name": "crm", "dsn": "postgresql://r:p@h:5432/d", "schema": "s"}
        with mock.patch.object(pyrunner_db, "_request", return_value=(200, payload)):
            self.assertEqual(
                pyrunner_db.sqlalchemy_url("crm"), "postgresql+psycopg://r:p@h:5432/d"
            )

    def test_list_databases(self):
        payload = {"databases": [{"name": "a", "status": "ready"}]}
        with mock.patch.object(pyrunner_db, "_request", return_value=(200, payload)):
            self.assertEqual(pyrunner_db.list_databases(), ["a"])


# ---------------------------------------------------------------------------
# Run-env hardening
# ---------------------------------------------------------------------------


class RunEnvDenylistTests(TestCase):
    def test_data_db_url_in_default_denylist(self):
        self.assertIn("PYRUNNER_DATA_DB_URL", settings.PYRUNNER_RUN_ENV_DENYLIST)

    def test_provisioner_dsn_never_reaches_scripts(self):
        from core.executor import _build_script_environment

        with mock.patch.dict(os.environ, {"PYRUNNER_DATA_DB_URL": _SERVER_URL}):
            env = _build_script_environment()
        self.assertNotIn("PYRUNNER_DATA_DB_URL", env)


# ---------------------------------------------------------------------------
# cpanel views (Owner/Admin gating + flows)
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ViewAccessTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        self.member = User.objects.create(email="member@example.com")
        self.outsider = User.objects.create(email="out@example.com")
        WorkspaceMembership.objects.create(
            user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER
        )
        WorkspaceMembership.objects.create(
            user=self.member, workspace=self.ws, role=WorkspaceMembership.ROLE_MEMBER
        )
        self.url = reverse("cpanel_ws:database_list", args=[self.ws.id])

    def test_owner_sees_list(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_member_is_403(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_non_member_is_404(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_superuser_sees_list(self):
        boss = User.objects.create(email="root@example.com", is_superuser=True)
        self.client.force_login(boss)
        self.assertEqual(self.client.get(self.url).status_code, 200)


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ViewFlowTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.objects.create(
            user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER
        )
        self.env = Environment.objects.create(name="e", path="dbview")
        self.script = Script.objects.create(
            name="s1", code="x", environment=self.env, workspace=self.ws
        )
        self.client.force_login(self.owner)

    def _url(self, name, *args):
        return reverse(f"cpanel_ws:{name}", args=[self.ws.id, *args])

    def test_create_provisions_and_redirects(self):
        with mock.patch.object(DatabaseService, "provision") as provision:
            resp = self.client.post(
                self._url("database_create"),
                {"name": "events", "description": "test db"},
            )
        provision.assert_called_once()
        db = Database.objects.get(name="events")
        self.assertEqual(db.workspace_id, self.ws.id)
        self.assertEqual(db.created_by, self.owner)
        # Views redirect into the canonical (unprefixed) cpanel instance —
        # house behavior for every workspace-scoped view.
        self.assertRedirects(
            resp,
            reverse("cpanel:database_detail", args=[db.pk]),
            fetch_redirect_response=False,
        )

    def test_create_unconfigured_redirects_with_error(self):
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            resp = self.client.post(
                self._url("database_create"), {"name": "events", "description": ""}
            )
        self.assertRedirects(
            resp,
            reverse("cpanel:database_list"),
            fetch_redirect_response=False,
        )
        self.assertFalse(Database.objects.filter(name="events").exists())

    def test_create_failure_lands_on_error_detail(self):
        def _fail(db):
            DatabaseService._record_error(db, "boom")
            raise DatabaseProvisionError("boom")

        with mock.patch.object(DatabaseService, "provision", side_effect=_fail):
            resp = self.client.post(
                self._url("database_create"), {"name": "events", "description": ""}
            )
        db = Database.objects.get(name="events")
        self.assertEqual(db.status, Database.STATUS_ERROR)
        self.assertRedirects(
            resp,
            reverse("cpanel:database_detail", args=[db.pk]),
            fetch_redirect_response=False,
        )

    def test_grants_reconcile(self):
        db = _make_database(self.ws)
        s2 = Script.objects.create(
            name="s2", code="x", environment=self.env, workspace=self.ws
        )
        url = self._url("database_grants", db.pk)

        self.client.post(url, {"granted_script_ids": [str(self.script.pk)]})
        self.assertTrue(
            DatabaseGrant.objects.filter(
                database=db, script=self.script, active=True
            ).exists()
        )

        # Reconcile to only s2: s1's grant is removed, s2's added.
        self.client.post(url, {"granted_script_ids": [str(s2.pk)]})
        self.assertFalse(
            DatabaseGrant.objects.filter(database=db, script=self.script).exists()
        )
        self.assertTrue(
            DatabaseGrant.objects.filter(database=db, script=s2, active=True).exists()
        )

    def test_grants_ignore_foreign_workspace_scripts(self):
        db = _make_database(self.ws)
        other_ws = Workspace.objects.create(name="X")
        foreign = Script.objects.create(
            name="fx", code="x", environment=self.env, workspace=other_ws
        )
        self.client.post(
            self._url("database_grants", db.pk),
            {"granted_script_ids": [str(foreign.pk)]},
        )
        self.assertFalse(DatabaseGrant.objects.filter(database=db).exists())

    def test_delete_requires_typed_name(self):
        db = _make_database(self.ws)
        with mock.patch.object(DatabaseService, "deprovision") as deprovision:
            self.client.post(
                self._url("database_delete", db.pk), {"confirm_name": "wrong"}
            )
        deprovision.assert_not_called()
        self.assertTrue(Database.objects.filter(pk=db.pk).exists())

    def test_delete_deprovisions_then_deletes(self):
        db = _make_database(self.ws)
        with mock.patch.object(DatabaseService, "deprovision") as deprovision:
            self.client.post(
                self._url("database_delete", db.pk), {"confirm_name": db.name}
            )
        deprovision.assert_called_once()
        self.assertFalse(Database.objects.filter(pk=db.pk).exists())

    def test_delete_kept_when_cleanup_fails_without_force(self):
        db = _make_database(self.ws)
        with mock.patch.object(
            DatabaseService,
            "deprovision",
            side_effect=DatabaseProvisionError("server gone"),
        ):
            self.client.post(
                self._url("database_delete", db.pk), {"confirm_name": db.name}
            )
        self.assertTrue(Database.objects.filter(pk=db.pk).exists())

    def test_delete_forced_despite_cleanup_failure(self):
        db = _make_database(self.ws)
        with mock.patch.object(
            DatabaseService,
            "deprovision",
            side_effect=DatabaseProvisionError("server gone"),
        ):
            self.client.post(
                self._url("database_delete", db.pk),
                {"confirm_name": db.name, "force": "on"},
            )
        self.assertFalse(Database.objects.filter(pk=db.pk).exists())

    def test_reveal_shows_dsn(self):
        db = _make_database(self.ws, password="showme")
        resp = self.client.post(self._url("database_reveal", db.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("showme", resp.content.decode())

    def test_detail_does_not_leak_password(self):
        db = _make_database(self.ws, password="hidden-pw")
        resp = self.client.get(self._url("database_detail", db.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("hidden-pw", resp.content.decode())


# ---------------------------------------------------------------------------
# Explorer (Stage 3) — read-only execute seam, view, monitor
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Plan-driven cursor: each (substring, columns, rows) entry answers the
    first query whose text contains the substring; anything else (set_config,
    DDL) yields no result set."""

    def __init__(self, plan):
        self._plan = plan
        self.executed = []
        self.description = None
        self._rows = []

    def execute(self, query, params=None):
        text = _sql_text(query)
        self.executed.append((text, params))
        self.description = None
        self._rows = []
        for substr, columns, rows in self._plan:
            if substr in text:
                self.description = [type("D", (), {"name": c})() for c in columns]
                self._rows = list(rows)
                return

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeScopedConn:
    def __init__(self, plan):
        self.read_only = None
        self.cur = _FakeCursor(plan)

    def cursor(self):
        return self.cur

    # Monitor path uses conn.execute directly (psycopg3 convenience API).
    def execute(self, query, params=None):
        self.cur.execute(query, params)
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ExplorerExecuteTests(TestCase):
    def setUp(self):
        from core.services.database_explorer import DatabaseExplorerService

        self.svc = DatabaseExplorerService
        self.ws = Workspace.objects.create(name="W")
        self.db = _make_database(self.ws)

    def _run(self, plan, **kwargs):
        conn = _FakeScopedConn(plan)
        with mock.patch.object(self.svc, "_scoped_connect", return_value=conn):
            result = self.svc.execute(self.db, "SELECT * FROM t", **kwargs)
        return conn, result

    def test_viewer_sessions_are_read_only_with_timeout(self):
        conn, _ = self._run([("SELECT * FROM t", ["a"], [(1,)])])
        self.assertIs(conn.read_only, True)
        self.assertIn("statement_timeout", conn.cur.executed[0][0])

    def test_stage5_seam_can_disable_read_only(self):
        conn, _ = self._run([("SELECT * FROM t", ["a"], [(1,)])], read_only=False)
        self.assertIs(conn.read_only, False)

    def test_row_cap_truncates_and_flags(self):
        rows = [(i,) for i in range(10)]
        _, result = self._run([("SELECT * FROM t", ["a"], rows)], row_cap=5)
        self.assertEqual(len(result.rows), 5)
        self.assertTrue(result.truncated)
        _, result = self._run([("SELECT * FROM t", ["a"], rows)], row_cap=None)
        self.assertEqual(len(result.rows), 10)
        self.assertFalse(result.truncated)

    def test_server_failure_wraps_in_explorer_error(self):
        from core.services.database_explorer import DatabaseExplorerError

        with mock.patch.object(
            self.svc, "_scoped_connect", side_effect=RuntimeError("down")
        ):
            with self.assertRaises(DatabaseExplorerError):
                self.svc.execute(self.db, "SELECT 1")

    def test_table_or_none_validates_against_real_tables(self):
        plan = [("FROM pg_class", ["relname", "n", "size"], [("items", 3, 8192)])]
        conn = _FakeScopedConn(plan)
        with mock.patch.object(self.svc, "_scoped_connect", return_value=conn):
            self.assertIsNotNone(self.svc.table_or_none(self.db, "items"))
        conn = _FakeScopedConn(plan)
        with mock.patch.object(self.svc, "_scoped_connect", return_value=conn):
            self.assertIsNone(self.svc.table_or_none(self.db, "no_such"))

    def test_rows_quotes_identifier_and_pages(self):
        plan = [("SELECT * FROM", ["id"], [(i,) for i in range(51)])]
        conn = _FakeScopedConn(plan)
        with mock.patch.object(self.svc, "_scoped_connect", return_value=conn):
            result = self.svc.rows(self.db, "items", page=2, per_page=50)
        text = conn.cur.executed[-1][0]
        self.assertIn("items", text)
        self.assertIn("OFFSET", text)
        self.assertEqual(len(result.rows), 50)
        self.assertTrue(result.truncated)  # doubles as has-next-page


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ExplorerMonitorTests(TestCase):
    def setUp(self):
        from core.services.database_explorer import DatabaseExplorerService

        self.svc = DatabaseExplorerService
        self.ws = Workspace.objects.create(name="W")
        self.db = _make_database(self.ws, name="crm")

    def _admin(self, plan):
        return mock.patch.object(
            DatabaseService, "_admin_connect", return_value=_FakeScopedConn(plan)
        )

    def test_stats_maps_by_database(self):
        plan = [
            ("pg_namespace", [], [(self.db.schema_name, 4096, 2)]),
            ("pg_stat_activity", [], [(self.db.role_name, 3)]),
        ]
        with self._admin(plan):
            stats = self.svc.stats_for_workspace(self.ws)
        entry = stats[self.db.id]
        self.assertEqual(entry["table_count"], 2)
        self.assertEqual(entry["connections"], 3)
        self.assertEqual(entry["size_bytes"], 4096)

    def test_stats_empty_when_server_down(self):
        with mock.patch.object(
            DatabaseService, "_admin_connect", side_effect=RuntimeError("down")
        ):
            self.assertEqual(self.svc.stats_for_workspace(self.ws), {})

    def test_activity_buckets_states(self):
        plan = [
            (
                "pg_stat_activity",
                [],
                [
                    (1, self.db.role_name, "active", 45, "", 0, "SELECT 1"),
                    (2, self.db.role_name, "active", 2, "", 0, "SELECT 2"),
                    (3, self.db.role_name, "idle in transaction", 10, "", 0, ""),
                    (4, self.db.role_name, "active", 5, "Lock", 2, "UPDATE t"),
                ],
            )
        ]
        with self._admin(plan):
            activity = self.svc.activity_for_workspace(self.ws)
        self.assertTrue(activity["ok"])
        self.assertEqual(activity["active"], 3)
        self.assertEqual(activity["long_running"], 1)
        self.assertEqual(activity["idle_in_transaction"], 1)
        self.assertEqual(activity["blocked"], 1)
        self.assertEqual(activity["sessions"][0]["database"], "crm")

    def test_activity_reports_unreachable_server(self):
        with mock.patch.object(
            DatabaseService, "_admin_connect", side_effect=RuntimeError("down")
        ):
            activity = self.svc.activity_for_workspace(self.ws)
        self.assertFalse(activity["ok"])
        self.assertIn("down", activity["error"])

    def test_slow_queries_degrade_without_extension(self):
        plan = [("pg_extension", [], [])]  # no row → not installed
        with self._admin(plan):
            slow = self.svc.slow_queries_for_workspace(self.ws)
        self.assertFalse(slow["available"])
        self.assertIn("CREATE EXTENSION", slow["reason"])

    def test_slow_queries_parse_and_map_roles(self):
        plan = [
            ("pg_extension", [], [(1,)]),
            (
                "pg_stat_statements",
                [],
                [(self.db.role_name, 12, 900.5, 75.04, 240, "SELECT * FROM items")],
            ),
        ]
        with self._admin(plan):
            slow = self.svc.slow_queries_for_workspace(self.ws)
        self.assertTrue(slow["available"])
        q = slow["queries"][0]
        self.assertEqual(q["database"], "crm")
        self.assertEqual(q["calls"], 12)
        self.assertEqual(q["total_ms"], 900.5)


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class ExplorerViewTests(TestCase):
    def setUp(self):
        from core.services.database_explorer import DatabaseExplorerService

        _setup_wizard_off(self)
        self.svc = DatabaseExplorerService
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        self.member = User.objects.create(email="member@example.com")
        WorkspaceMembership.objects.create(
            user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER
        )
        WorkspaceMembership.objects.create(
            user=self.member, workspace=self.ws, role=WorkspaceMembership.ROLE_MEMBER
        )
        self.db = _make_database(self.ws)
        self.client.force_login(self.owner)

    def _url(self, name, *args):
        return reverse(f"cpanel_ws:{name}", args=[self.ws.id, *args])

    def _patch_view_data(self):
        return (
            mock.patch.object(
                self.svc,
                "tables",
                return_value=[
                    {
                        "name": "items",
                        "row_estimate": 3,
                        "size_bytes": 8192,
                        "size_display": "8.0 KB",
                    }
                ],
            ),
            mock.patch.object(
                self.svc,
                "columns",
                return_value=[
                    {"name": "id", "type": "integer", "nullable": False, "default": ""}
                ],
            ),
            mock.patch.object(self.svc, "indexes", return_value=[]),
        )

    def test_monitor_requires_manage_role(self):
        self.client.force_login(self.member)
        with mock.patch.object(
            self.svc, "activity_for_workspace"
        ) as activity:
            resp = self.client.get(self._url("database_monitor"))
        self.assertEqual(resp.status_code, 403)
        activity.assert_not_called()

    def test_monitor_renders_with_degraded_slow_queries(self):
        with (
            mock.patch.object(
                self.svc,
                "activity_for_workspace",
                return_value={
                    "ok": True,
                    "error": "",
                    "sessions": [],
                    "active": 0,
                    "idle_in_transaction": 0,
                    "blocked": 0,
                    "long_running": 0,
                },
            ),
            mock.patch.object(
                self.svc,
                "slow_queries_for_workspace",
                return_value={
                    "available": False,
                    "reason": "pg_stat_statements is not installed",
                    "queries": [],
                },
            ),
            mock.patch.object(self.svc, "stats_for_workspace", return_value={}),
        ):
            resp = self.client.get(self._url("database_monitor"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pg_stat_statements is not installed", resp.content.decode())

    def test_table_page_renders_grid(self):
        from core.services.database_explorer import QueryResult

        p_tables, p_columns, p_indexes = self._patch_view_data()
        with (
            p_tables,
            p_columns,
            p_indexes,
            mock.patch.object(
                self.svc,
                "rows",
                return_value=QueryResult(["id"], [(1,), (2,)], False),
            ),
        ):
            resp = self.client.get(self._url("database_table", self.db.pk, "items"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("items", body)
        self.assertIn("Export CSV", body)

    def test_unknown_table_is_404(self):
        with mock.patch.object(self.svc, "tables", return_value=[]):
            resp = self.client.get(self._url("database_table", self.db.pk, "nope"))
        self.assertEqual(resp.status_code, 404)

    def test_csv_export_streams_rows(self):
        from core.services.database_explorer import QueryResult

        p_tables, _, _ = self._patch_view_data()
        with (
            p_tables,
            mock.patch.object(
                self.svc,
                "csv_rows",
                return_value=QueryResult(["id", "v"], [(1, "a"), (2, "b")], False),
            ),
        ):
            resp = self.client.get(
                self._url("database_table_csv", self.db.pk, "items")
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        content = resp.content.decode()
        self.assertIn("id,v", content)
        self.assertIn("2,b", content)

    def test_detail_shows_tables_and_survives_explorer_failure(self):
        from core.services.database_explorer import DatabaseExplorerError

        p_tables, _, _ = self._patch_view_data()
        with p_tables:
            resp = self.client.get(self._url("database_detail", self.db.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("items", resp.content.decode())

        with mock.patch.object(
            self.svc, "tables", side_effect=DatabaseExplorerError("server gone")
        ):
            resp = self.client.get(self._url("database_detail", self.db.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("server gone", resp.content.decode())


# ---------------------------------------------------------------------------
# Plugin SDK — DatabaseAPI (API 2.2)
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class PluginSdkDatabaseTests(TestCase):
    OWNER = "myplug"

    def setUp(self):
        from core.plugins.api import DatabaseAPI

        self.ws = Workspace.objects.create(name="W")
        self.api = DatabaseAPI(self.OWNER, workspace=self.ws)
        self.env = Environment.objects.create(name="e", path="sdkdb")
        self.script = Script.objects.create(
            name="worker", code="x", environment=self.env, workspace=self.ws
        )

    def test_is_available_tracks_settings(self):
        self.assertTrue(self.api.is_available())
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            self.assertFalse(self.api.is_available())

    def test_provision_auto_names_and_stamps_ownership(self):
        with mock.patch.object(DatabaseService, "provision") as provision:
            db = self.api.provision("metrics", description="plugin db")
        provision.assert_called_once_with(db)
        self.assertEqual(db.name, "myplug:metrics")
        self.assertEqual(db.owner_plugin, "myplug")
        self.assertEqual(db.owner_key, "metrics")
        self.assertEqual(db.workspace_id, self.ws.id)
        self.assertEqual(db.description, "plugin db")

    def test_provision_is_idempotent_and_heals_error_rows(self):
        with mock.patch.object(DatabaseService, "provision"):
            first = self.api.provision("metrics")
        Database.objects.filter(pk=first.pk).update(status=Database.STATUS_ERROR)
        with mock.patch.object(DatabaseService, "provision") as provision:
            second = self.api.provision("metrics")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Database.objects.count(), 1)
        # The healing re-provision runs against the SAME (existing) row.
        provision.assert_called_once_with(second)

    def test_get_and_list_are_owner_scoped(self):
        with mock.patch.object(DatabaseService, "provision"):
            mine = self.api.provision("metrics")
        _make_database(self.ws, name="user_db")  # unowned row in same workspace

        self.assertEqual(self.api.get("metrics").pk, mine.pk)
        self.assertIsNone(self.api.get("user_db"))
        self.assertEqual([d.pk for d in self.api.list()], [mine.pk])

        from core.plugins.api import DatabaseAPI

        other = DatabaseAPI("otherplug", workspace=self.ws)
        self.assertIsNone(other.get("metrics"))
        self.assertEqual(other.list(), [])

    def test_legacy_lane_uses_raw_name_and_no_stamps(self):
        from core.plugins.api import DatabaseAPI

        with mock.patch.object(DatabaseService, "provision"):
            db = DatabaseAPI(workspace=self.ws).provision("plain")
        self.assertEqual(db.name, "plain")
        self.assertIsNone(db.owner_plugin)
        self.assertIsNone(db.owner_key)

    def test_grant_is_idempotent_and_reactivates(self):
        db = _make_database(self.ws, name="myplug:metrics")
        g1 = self.api.grant(self.script, db)
        g2 = self.api.grant(self.script, db)
        self.assertEqual(g1.pk, g2.pk)
        self.assertTrue(g2.active)

        self.api.grant(self.script, db, active=False)
        g1.refresh_from_db()
        self.assertFalse(g1.active)
        self.api.grant(self.script, db)
        g1.refresh_from_db()
        self.assertTrue(g1.active)

    def test_dsn_only_for_ready_databases(self):
        self.assertIsNone(self.api.dsn("metrics"))  # doesn't exist
        db = _make_database(
            self.ws, name="myplug:metrics", status=Database.STATUS_ERROR
        )
        db.owner_plugin = self.OWNER
        db.owner_key = "metrics"
        db.save()
        self.assertIsNone(self.api.dsn("metrics"))  # not ready

        db.status = Database.STATUS_READY
        db.save(update_fields=["status"])
        dsn = self.api.dsn("metrics")
        self.assertIn(db.role_name, dsn)
        self.assertTrue(dsn.startswith("postgresql://"))


# ---------------------------------------------------------------------------
# Backup / restore (Stage 4)
# ---------------------------------------------------------------------------


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class DumpHelperTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.db = _make_database(self.ws, password="dumppw")

    def test_capability_degrades_without_binaries(self):
        with mock.patch(
            "core.services.database_service.shutil.which", return_value=None
        ):
            ok, reason = DatabaseService.dump_capability()
        self.assertFalse(ok)
        self.assertIn("pg_dump", reason)

    def test_dump_schema_runs_pg_dump_with_env_password(self):
        completed = mock.Mock(returncode=0, stdout="-- dump", stderr="")
        with (
            mock.patch(
                "core.services.database_service.shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            mock.patch(
                "core.services.database_service.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            out = DatabaseService.dump_schema(self.db)
        self.assertEqual(out, "-- dump")
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertIn("-n", argv)
        self.assertIn(self.db.schema_name, argv)
        self.assertIn("--no-owner", argv)
        # The provisioner password travels via PGPASSWORD, never argv.
        self.assertEqual(env["PGPASSWORD"], "adminpw")
        self.assertNotIn("adminpw", " ".join(argv))

    def test_dump_schema_failure_raises(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="server version mismatch")
        with (
            mock.patch(
                "core.services.database_service.shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            mock.patch(
                "core.services.database_service.subprocess.run",
                return_value=completed,
            ),
        ):
            with self.assertRaises(DatabaseProvisionError) as ctx:
                DatabaseService.dump_schema(self.db)
        self.assertIn("version mismatch", str(ctx.exception))

    def test_restore_strips_create_schema_and_runs_as_role(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        dump = (
            "SET x;\nSET transaction_timeout = 0;\n"
            "CREATE SCHEMA old_schema;\nCREATE TABLE t (id int);\n"
        )
        with (
            mock.patch(
                "core.services.database_service.shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            mock.patch(
                "core.services.database_service.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            DatabaseService.restore_schema_dump(self.db, dump)
        argv = run.call_args.args[0]
        fed = run.call_args.kwargs["input"]
        env = run.call_args.kwargs["env"]
        self.assertIn(self.db.role_name, argv)  # psql connects AS the role
        self.assertEqual(env["PGPASSWORD"], "dumppw")
        self.assertNotIn("CREATE SCHEMA", fed)
        # pg_dump 17 preamble GUC that older servers reject (live PG16 catch).
        self.assertNotIn("transaction_timeout", fed)
        self.assertIn("SET x;", fed)
        self.assertIn("CREATE TABLE t", fed)
        self.assertIn("ON_ERROR_STOP=1", argv)


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class BackupDatabasesTests(TestCase):
    def setUp(self):
        from core.services.backup_service import BackupService

        self.backup = BackupService
        self.ws = Workspace.objects.create(name="W")
        self.env = Environment.objects.create(name="e", path="bkdb")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws
        )
        self.db = _make_database(self.ws, password="bkpw")
        DatabaseGrant.objects.create(script=self.script, database=self.db)

    def test_export_includes_metadata_grants_and_dump(self):
        with mock.patch.object(
            DatabaseService, "dump_schema", return_value="-- sql"
        ):
            exported = self.backup._export_databases()
        entry = exported[0]
        self.assertEqual(entry["name"], "crm")
        self.assertEqual(entry["schema_name"], self.db.schema_name)
        self.assertEqual(entry["encrypted_password"], self.db.encrypted_password)
        self.assertEqual(entry["dump_sql"], "-- sql")
        self.assertEqual(entry["grants"][0]["script_id"], str(self.script.id))

    def test_export_records_skip_reason_instead_of_failing(self):
        with mock.patch.object(
            DatabaseService, "dump_schema", side_effect=RuntimeError("no pg_dump")
        ):
            exported = self.backup._export_databases()
        self.assertIsNone(exported[0]["dump_sql"])
        self.assertIn("no pg_dump", exported[0]["dump_skipped_reason"])

    def test_export_skips_dump_when_unconfigured_or_not_ready(self):
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            exported = self.backup._export_databases()
        self.assertIn("no data server", exported[0]["dump_skipped_reason"])

        self.db.status = Database.STATUS_ERROR
        self.db.save(update_fields=["status"])
        exported = self.backup._export_databases()
        self.assertIn("error", exported[0]["dump_skipped_reason"])

    def test_create_backup_carries_databases_and_version(self):
        with mock.patch.object(DatabaseService, "dump_schema", return_value="-- sql"):
            data = self.backup.create_backup(include_runs=False)
        # Databases joined the format at 1.5.0, so that is the FLOOR — this test's
        # subject is the databases payload, and pinning the exact current version
        # here would fail on every later additive bump for no reason.
        version = tuple(int(p) for p in data["backup_metadata"]["version"].split("."))
        self.assertGreaterEqual(version, (1, 5, 0))
        self.assertEqual(len(data["databases"]), 1)

    def _restore(self, databases_data, script_map=None):
        return self.backup._import_databases(
            databases_data,
            script_map if script_map is not None else {str(self.script.id): self.script},
            {},
            None,
            {},
            self.ws,
        )

    def _entry(self, **overrides):
        entry = {
            "id": str(uuid.uuid4()),
            "name": "restored",
            "workspace_id": None,
            "owner_plugin": None,
            "owner_key": None,
            "schema_name": f"db_restored_{uuid.uuid4().hex[:6]}",
            "role_name": f"db_restored_{uuid.uuid4().hex[:6]}",
            "encrypted_password": self.db.encrypted_password,
            "description": "",
            "grants": [{"script_id": str(self.script.id), "active": True}],
            "dump_sql": "-- sql",
            "dump_skipped_reason": "",
            "created_by_email": None,
        }
        entry.update(overrides)
        return entry

    def test_import_provisions_replays_and_regrants(self):
        entry = self._entry()
        with (
            mock.patch.object(DatabaseService, "provision") as provision,
            mock.patch.object(DatabaseService, "restore_schema_dump") as replay,
        ):
            count, warnings = self._restore([entry])
        self.assertEqual(count, 1)
        self.assertEqual(warnings, [])
        restored = Database.objects.get(name="restored")
        self.assertEqual(restored.schema_name, entry["schema_name"])  # verbatim
        provision.assert_called_once()
        self.assertEqual(str(provision.call_args.args[0].pk), entry["id"])
        replay.assert_called_once()
        self.assertEqual(str(replay.call_args.args[0].pk), entry["id"])
        self.assertEqual(replay.call_args.args[1], "-- sql")
        self.assertTrue(
            DatabaseGrant.objects.filter(
                database=restored, script=self.script, active=True
            ).exists()
        )

    def test_import_skips_all_without_data_server(self):
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            count, warnings = self._restore([self._entry()])
        self.assertEqual(count, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no data server", warnings[0])
        self.assertFalse(Database.objects.filter(name="restored").exists())

    def test_import_provision_failure_keeps_row_and_grants(self):
        with mock.patch.object(
            DatabaseService, "provision", side_effect=DatabaseProvisionError("down")
        ):
            count, warnings = self._restore([self._entry()])
        self.assertEqual(count, 1)
        self.assertIn("provisioning failed", warnings[0])
        restored = Database.objects.get(name="restored")
        self.assertTrue(DatabaseGrant.objects.filter(database=restored).exists())

    def test_import_replay_failure_warns_but_keeps_database(self):
        with (
            mock.patch.object(DatabaseService, "provision"),
            mock.patch.object(
                DatabaseService,
                "restore_schema_dump",
                side_effect=DatabaseProvisionError("psql failed"),
            ),
        ):
            count, warnings = self._restore([self._entry()])
        self.assertEqual(count, 1)
        self.assertIn("data replay failed", warnings[0])

    def test_import_dataless_entry_warns_restored_empty(self):
        entry = self._entry(dump_sql=None, dump_skipped_reason="pg_dump missing")
        with mock.patch.object(DatabaseService, "provision"):
            count, warnings = self._restore([entry])
        self.assertEqual(count, 1)
        self.assertIn("restored empty", warnings[0])

    def test_cleanup_deprovisions_or_warns(self):
        with mock.patch.object(DatabaseService, "deprovision") as deprovision:
            warnings = self.backup._cleanup_existing_databases()
        deprovision.assert_called_once()
        self.assertEqual(warnings, [])

        with mock.patch.object(
            DatabaseService, "deprovision", side_effect=DatabaseProvisionError("gone")
        ):
            warnings = self.backup._cleanup_existing_databases()
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.db.schema_name, warnings[0])

        with override_settings(PYRUNNER_DATA_DB_URL=""):
            warnings = self.backup._cleanup_existing_databases()
        self.assertIn("without server cleanup", warnings[0])


# ---------------------------------------------------------------------------
# Model resolution (tenancy contract parity with DataStore)
# ---------------------------------------------------------------------------


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ResolveForWorkspaceTests(TestCase):
    def setUp(self):
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")

    def test_scoped_resolution(self):
        db_a = _make_database(self.ws_a, name="shared")
        _make_database(self.ws_b, name="shared")
        self.assertEqual(
            Database.resolve_for_workspace("shared", self.ws_a.id), db_a
        )

    def test_missing_raises(self):
        with self.assertRaises(Database.DoesNotExist):
            Database.resolve_for_workspace("nope", self.ws_a.id)

    def test_null_workspace_fallback(self):
        legacy = Database(
            name="legacy",
            workspace=None,
            schema_name="db_legacy",
            role_name="db_legacy",
            encrypted_password="x",
        )
        legacy.save()
        self.assertEqual(
            Database.resolve_for_workspace("legacy", self.ws_a.id), legacy
        )
