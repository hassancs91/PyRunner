"""
Plugin API — token generalization + dispatcher tests (docs/PLAN_plugin_api.md).

Stage 1 (this file's MUST-PASS core): the ``DataStoreAPIToken`` → ``APIToken``
generalization must leave ``/api/v1/datastores/…`` byte-for-byte unchanged, and
scope expansion must be impossible in BOTH directions:

- a legacy token (backfilled to ``datastore``/``datastores`` scope) never gains
  plugin API access, and
- a new ``plugin``-scoped token never gains datastore access (403 SCOPE_MISMATCH).

Stage 2 extends this file with the dispatcher auth/scope/limits/isolation matrix.
"""

import json
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core import plugins as plugin_registry
from core.forms import APITokenForm
from core.models import (
    APIToken,
    DataStore,
    DataStoreAPIToken,
    DataStoreEntry,
    Workspace,
)
from core.plugins import PyRunnerPlugin


def _mock_setup(test):
    """Stop the setup-wizard middleware from 302-ing client requests."""
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _entry(store, key, value):
    e = DataStoreEntry(datastore=store, key=key)
    e.set_value(value)
    e.save()
    return e


def _register_fixture_plugin(test, slug="fixture_plugin"):
    """Register a plugin in the in-process registry for the test's duration."""
    plugin_registry.register(PyRunnerPlugin(slug=slug, name="Fixture Plugin"))
    test.addCleanup(plugin_registry.unregister, slug)


class DatastoreApiRegressionTests(TestCase):
    """MUST-PASS — /api/v1/datastores/… is byte-for-byte unaffected by Stage 1.

    Tokens here are created exactly the way pre-plugin-API code did (datastore
    FK or bare), relying on the backfill-equivalent model behavior.
    """

    def setUp(self):
        _mock_setup(self)
        self.default = Workspace.get_default()
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.store_a = DataStore.objects.create(name="alpha", workspace=self.ws_a)
        self.store_a2 = DataStore.objects.create(name="alpha_two", workspace=self.ws_a)
        self.store_b = DataStore.objects.create(name="beta", workspace=self.ws_b)
        _entry(self.store_a, "k", "a-value")
        _entry(self.store_b, "k", "b-value")

        # Legacy shapes: a "global" token (workspace-bound, no FK) and a
        # store-scoped token, created WITHOUT touching the new fields.
        self.global_token = APIToken.objects.create(
            name="global", token=APIToken.generate_token(), workspace=self.ws_a
        )
        self.scoped_token = APIToken.objects.create(
            name="scoped", token=APIToken.generate_token(), datastore=self.store_a
        )

    # -- reach ---------------------------------------------------------------

    def test_global_token_lists_only_its_workspace_stores(self):
        resp = self.client.get("/api/v1/datastores/", **_auth(self.global_token.token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        names = {d["name"] for d in body["datastores"]}
        self.assertEqual(names, {"alpha", "alpha_two"})
        self.assertEqual(body["count"], 2)
        # Response shape unchanged (field-for-field).
        self.assertEqual(
            set(body["datastores"][0]),
            {"name", "description", "entry_count", "created_at", "updated_at"},
        )

    def test_scoped_token_lists_only_its_store(self):
        resp = self.client.get("/api/v1/datastores/", **_auth(self.scoped_token.token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([d["name"] for d in resp.json()["datastores"]], ["alpha"])

    def test_scoped_token_other_store_in_workspace_403(self):
        resp = self.client.get(
            "/api/v1/datastores/alpha_two/", **_auth(self.scoped_token.token)
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_cross_workspace_store_404(self):
        resp = self.client.get(
            "/api/v1/datastores/beta/", **_auth(self.global_token.token)
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "NOT_FOUND")

    def test_entries_and_single_entry(self):
        resp = self.client.get(
            "/api/v1/datastores/alpha/entries/", **_auth(self.global_token.token)
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["entries"][0]["key"], "k")

        resp = self.client.get(
            "/api/v1/datastores/alpha/entries/k/", **_auth(self.global_token.token)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["value"], "a-value")

    def test_null_workspace_token_falls_back_to_default_workspace(self):
        # Pre-tenancy legacy fallback for datastore-scoped tokens stays.
        store_d = DataStore.objects.create(name="dstore", workspace=self.default)
        bare = APIToken.objects.create(name="bare", token=APIToken.generate_token())
        resp = self.client.get("/api/v1/datastores/", **_auth(bare.token))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(store_d.name, {d["name"] for d in resp.json()["datastores"]})

    # -- auth ----------------------------------------------------------------

    def test_missing_token_401(self):
        resp = self.client.get("/api/v1/datastores/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_invalid_token_401(self):
        resp = self.client.get("/api/v1/datastores/", **_auth("not-a-real-token"))
        self.assertEqual(resp.status_code, 401)

    def test_expired_token_401(self):
        self.global_token.expires_at = timezone.now() - timedelta(minutes=1)
        self.global_token.save(update_fields=["expires_at"])
        resp = self.client.get("/api/v1/datastores/", **_auth(self.global_token.token))
        self.assertEqual(resp.status_code, 401)

    def test_inactive_token_401(self):
        self.global_token.is_active = False
        self.global_token.save(update_fields=["is_active"])
        resp = self.client.get("/api/v1/datastores/", **_auth(self.global_token.token))
        self.assertEqual(resp.status_code, 401)

    def test_rate_limit_path_still_wired(self):
        with mock.patch(
            "core.views.api.decorators.rate_limit_exceeded", return_value=True
        ):
            resp = self.client.get(
                "/api/v1/datastores/", **_auth(self.global_token.token)
            )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "RATE_LIMITED")

    def test_last_used_stamped(self):
        self.client.get("/api/v1/datastores/", **_auth(self.global_token.token))
        self.global_token.refresh_from_db()
        self.assertIsNotNone(self.global_token.last_used_at)


class PluginScopeRejectionTests(TestCase):
    """Scope expansion is impossible in both directions."""

    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.objects.create(name="W")
        self.store = DataStore.objects.create(name="alpha", workspace=self.ws)
        self.plugin_token = APIToken.objects.create(
            name="pt",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug="brand_tracker",
            workspace=self.ws,
        )
        self.global_token = APIToken.objects.create(
            name="gt", token=APIToken.generate_token(), workspace=self.ws
        )
        self.scoped_token = APIToken.objects.create(
            name="st", token=APIToken.generate_token(), datastore=self.store
        )

    def test_plugin_token_rejected_on_every_datastore_endpoint(self):
        for url in (
            "/api/v1/datastores/",
            "/api/v1/datastores/alpha/",
            "/api/v1/datastores/alpha/entries/",
            "/api/v1/datastores/alpha/entries/k/",
        ):
            resp = self.client.get(url, **_auth(self.plugin_token.token))
            self.assertEqual(resp.status_code, 403, url)
            self.assertEqual(resp.json()["error"]["code"], "SCOPE_MISMATCH", url)

    def test_legacy_tokens_cannot_reach_plugin_api(self):
        # Invariant (plan: "Risks"): no datastore-scoped token may EVER get a
        # 200 from /api/v1/plugins/…. Before Stage 2 the URL 404s; after, the
        # dispatcher must 403 it — either way, never a success.
        for token in (self.global_token, self.scoped_token):
            resp = self.client.get(
                "/api/v1/plugins/brand_tracker/mentions/", **_auth(token.token)
            )
            self.assertIn(resp.status_code, (403, 404))


class APITokenModelTests(TestCase):
    """Model-level integrity: scope constraints + legacy-shape derivation."""

    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.store = DataStore.objects.create(name="s", workspace=self.ws)

    def test_alias_is_same_class(self):
        self.assertIs(DataStoreAPIToken, APIToken)

    def test_legacy_fk_creation_derives_datastore_scope(self):
        t = APIToken.objects.create(
            name="t", token=APIToken.generate_token(), datastore=self.store
        )
        self.assertEqual(t.scope, APIToken.Scope.DATASTORE)

    def test_bare_creation_is_datastores_scope(self):
        t = APIToken.objects.create(name="t", token=APIToken.generate_token())
        self.assertEqual(t.scope, APIToken.Scope.DATASTORES)

    def test_plugin_scope_requires_slug(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                APIToken.objects.create(
                    name="t",
                    token=APIToken.generate_token(),
                    scope=APIToken.Scope.PLUGIN,
                )

    def test_plugin_scope_rejects_datastore_fk(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                APIToken.objects.create(
                    name="t",
                    token=APIToken.generate_token(),
                    scope=APIToken.Scope.PLUGIN,
                    plugin_slug="p",
                    datastore=self.store,
                )

    def test_datastore_scope_requires_fk(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                APIToken.objects.create(
                    name="t",
                    token=APIToken.generate_token(),
                    scope=APIToken.Scope.DATASTORE,
                )

    def test_datastores_scope_rejects_plugin_slug(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                APIToken.objects.create(
                    name="t",
                    token=APIToken.generate_token(),
                    plugin_slug="p",
                )


class APITokenFormTests(TestCase):
    """Form scope selector: default flow unchanged, per-scope requirements."""

    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.store = DataStore.objects.create(name="s", workspace=self.ws)
        _register_fixture_plugin(self)

    def _form(self, data):
        return APITokenForm(data, workspace=self.ws)

    def test_default_scope_datastores_valid_with_name_only(self):
        form = self._form({"name": "t", "scope": "datastores"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_datastore_scope_requires_store(self):
        form = self._form({"name": "t", "scope": "datastore"})
        self.assertFalse(form.is_valid())
        self.assertIn("datastore", form.errors)

    def test_datastore_scope_with_store_valid(self):
        form = self._form({"name": "t", "scope": "datastore", "datastore": self.store.pk})
        self.assertTrue(form.is_valid(), form.errors)

    def test_plugin_scope_requires_slug(self):
        form = self._form({"name": "t", "scope": "plugin"})
        self.assertFalse(form.is_valid())
        self.assertIn("plugin_slug", form.errors)

    def test_plugin_scope_with_loaded_plugin_valid(self):
        form = self._form({"name": "t", "scope": "plugin", "plugin_slug": "fixture_plugin"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_plugin_scope_rejects_unloaded_plugin(self):
        form = self._form({"name": "t", "scope": "plugin", "plugin_slug": "ghost_plugin"})
        self.assertFalse(form.is_valid())
        self.assertIn("plugin_slug", form.errors)

    def test_scope_switch_clears_stray_selections(self):
        # A datastore picked, then scope switched to "datastores": the stray FK
        # must not survive (would violate the CheckConstraint).
        form = self._form(
            {"name": "t", "scope": "datastores", "datastore": self.store.pk}
        )
        self.assertTrue(form.is_valid(), form.errors)
        token = form.save(commit=False)
        token.token = APIToken.generate_token()
        token.save()
        self.assertIsNone(token.datastore)
        self.assertEqual(token.scope, APIToken.Scope.DATASTORES)


class APITokenCreateViewTests(TestCase):
    """The create-token UI: scope selector present, plugin tokens stamped."""

    def setUp(self):
        _mock_setup(self)
        from core.models import User

        self.default = Workspace.get_default()
        self.user = User.objects.create(email="u@example.com")
        self.client.force_login(self.user)
        _register_fixture_plugin(self)

    def test_create_page_renders_scope_and_plugin_fields(self):
        resp = self.client.get(reverse("cpanel:api_token_create"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("id_scope", body)
        self.assertIn("id_plugin_slug", body)
        self.assertIn("fixture_plugin", body)

    def test_create_plugin_token_stamps_workspace(self):
        resp = self.client.post(
            reverse("cpanel:api_token_create"),
            {"name": "pt", "scope": "plugin", "plugin_slug": "fixture_plugin"},
        )
        self.assertEqual(resp.status_code, 302)
        token = APIToken.objects.get(name="pt")
        self.assertEqual(token.scope, APIToken.Scope.PLUGIN)
        self.assertEqual(token.plugin_slug, "fixture_plugin")
        self.assertIsNotNone(token.workspace)

    def test_create_default_scope_flow_unchanged(self):
        resp = self.client.post(
            reverse("cpanel:api_token_create"),
            {"name": "gt", "scope": "datastores"},
        )
        self.assertEqual(resp.status_code, 302)
        token = APIToken.objects.get(name="gt")
        self.assertEqual(token.scope, APIToken.Scope.DATASTORES)
        self.assertIsNone(token.datastore)


class ScopeBackfillMigrationTests(TransactionTestCase):
    """The 0050 backfill maps legacy rows to datastore/datastores ONLY.

    Runs the real migration against real pre-migration rows (created at 0049,
    where `scope` doesn't exist yet) — the sharp edge the plan calls out.
    """

    migrate_from = [("core", "0049_secret_providers")]
    migrate_to = [("core", "0050_api_token_scope")]

    def test_backfill_from_datastore_fk(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        OldToken = old_apps.get_model("core", "DataStoreAPIToken")
        OldStore = old_apps.get_model("core", "DataStore")
        OldWorkspace = old_apps.get_model("core", "Workspace")

        ws = OldWorkspace.objects.create(name="W")
        store = OldStore.objects.create(name="s", workspace_id=ws.id)
        OldToken.objects.create(name="scoped", token="x" * 64, datastore_id=store.id)
        OldToken.objects.create(name="global", token="y" * 64, workspace_id=ws.id)

        try:
            executor.loader.build_graph()
            executor.migrate(self.migrate_to)
            new_apps = executor.loader.project_state(self.migrate_to).apps
            NewToken = new_apps.get_model("core", "APIToken")

            scopes = dict(NewToken.objects.values_list("name", "scope"))
            self.assertEqual(scopes, {"scoped": "datastore", "global": "datastores"})
            # No legacy row may ever come out plugin-scoped (silent expansion).
            self.assertFalse(NewToken.objects.filter(scope="plugin").exists())
        finally:
            # Restore the schema to head for the tests that follow.
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes("core"))


# =========================================================================== #
# Stage 2 — dispatcher + SDK seam (auth / scope / limits / isolation matrix)
# =========================================================================== #

import shutil
import sys
import tempfile
from pathlib import Path

from django.test import override_settings

import plugins as plugins_pkg
from core.plugins import api as sdk
from core.views.api import plugins as plugin_views

FIXTURE_MANIFEST = {
    "manifest_version": 1,
    "slug": "fixture_plugin",
    "name": "Fixture Plugin",
    "version": "1.2.3",
    "api": "2.3",
    "provides": {
        "api_resources": [
            {"name": "mentions", "summary": "Test feed", "methods": ["GET"]},
            {"name": "ghost", "summary": "Declared but unregistered", "methods": ["GET"]},
        ],
        "public_pages": [
            {"name": "report", "summary": "Test public page"},
            {"name": "ghost_page", "summary": "Declared but unregistered"},
        ],
    },
}

FIXTURE_API_SOURCE = '''
from core.plugins.api import APIError, page, resource


@resource("mentions")
def mentions(req):
    if req.params.get("boom"):
        raise ValueError("internal secret detail")
    if req.params.get("apierror"):
        raise APIError("not configured yet", code="NOT_CONFIGURED", status=409)
    if req.params.get("redirect"):
        raise APIError("go away", status=302)
    if req.params.get("fake5xx"):
        raise APIError("pretend upstream", status=502)
    if req.params.get("big"):
        return {"data": "x" * 5000}
    if req.params.get("notdict"):
        return ["not", "a", "dict"]
    if req.params.get("unserializable"):
        return {"obj": object()}
    return {
        "resource": req.resource,
        "item_id": req.item_id,
        "method": req.method,
        "params": req.params,
        "tags": req.params_list.get("tag", []),
        "workspace": getattr(req.workspace, "name", None),
    }


@resource("undeclared")
def undeclared(req):
    return {"should": "never be served"}


@page("report")
def report_page(req):
    if req.params.get("boom"):
        raise ValueError("page internals")
    if req.params.get("notstr"):
        return {"not": "a string"}
    if req.params.get("big"):
        return "<html>" + "x" * 5000 + "</html>"
    ws = getattr(req.workspace, "name", "?")
    return "<html><body>Report for " + ws + "</body></html>"


@page("undeclared_page")
def undeclared_page(req):
    return "<html>never served</html>"
'''


class _PluginFixtureBase(TestCase):
    """Injects a real on-disk fixture plugin (dev-mode mechanics: the temp dir
    is spliced onto the ``plugins`` package __path__, exactly how
    PYRUNNER_PLUGIN_DEV loads a folder) and points INSTALLED_PLUGINS at it."""

    slug = "fixture_plugin"
    manifest = FIXTURE_MANIFEST
    api_source = FIXTURE_API_SOURCE

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.mkdtemp(prefix="pyrunner_test_plugins_")
        pkg = Path(cls._tmp) / cls.slug
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "plugin.json").write_text(json.dumps(cls.manifest), encoding="utf-8")
        if cls.api_source is not None:
            (pkg / "api.py").write_text(cls.api_source, encoding="utf-8")
        plugins_pkg.__path__.append(cls._tmp)
        cls._override = override_settings(INSTALLED_PLUGINS=[f"plugins.{cls.slug}"])
        cls._override.enable()
        cls.addClassCleanup(cls._teardown_fixture)

    @classmethod
    def _teardown_fixture(cls):
        cls._override.disable()
        if cls._tmp in plugins_pkg.__path__:
            plugins_pkg.__path__.remove(cls._tmp)
        for mod in [m for m in sys.modules if m.startswith(f"plugins.{cls.slug}")]:
            del sys.modules[mod]
        plugin_views._manifest_cache.clear()
        plugin_views._handlers_cache.clear()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        _mock_setup(self)
        # Dispatcher caches are per-process; tests must not leak entries.
        self.addCleanup(plugin_views._manifest_cache.clear)
        self.addCleanup(plugin_views._handlers_cache.clear)
        self.ws = Workspace.objects.create(name="W")
        self.token = APIToken.objects.create(
            name="pt",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug=self.slug,
            workspace=self.ws,
        )

    def _get(self, url, token=None, **extra):
        return self.client.get(url, **_auth(token or self.token.token), **extra)


class DispatcherHappyPathTests(_PluginFixtureBase):
    def test_list_call_returns_handler_dict_verbatim(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?keyword=acme")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["resource"], "mentions")
        self.assertIsNone(body["item_id"])
        self.assertEqual(body["method"], "GET")
        self.assertEqual(body["params"], {"keyword": "acme"})
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_item_call_sets_item_id(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/42/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["item_id"], "42")

    def test_params_list_carries_repeated_keys(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?tag=a&tag=b")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tags"], ["a", "b"])
        self.assertEqual(body["params"]["tag"], "a")  # first value per key

    def test_workspace_derived_from_token_server_side(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.json()["workspace"], "W")

    def test_last_used_stamped(self):
        self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    def test_options_preflight_answered_pre_auth(self):
        resp = self.client.options("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_discovery_lists_exactly_the_tokens_plugin(self):
        resp = self._get("/api/v1/plugins/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        plugin = body["plugins"][0]
        self.assertEqual(plugin["slug"], "fixture_plugin")
        self.assertEqual(plugin["name"], "Fixture Plugin")
        self.assertEqual(plugin["version"], "1.2.3")
        self.assertEqual(
            {r["name"] for r in plugin["resources"]}, {"mentions", "ghost"}
        )
        # Declared pages appear as metadata only — never their capability URLs.
        self.assertEqual(
            {p["name"] for p in plugin["public_pages"]}, {"report", "ghost_page"}
        )
        self.assertNotIn("/p/", json.dumps(plugin))

    def test_plugin_index_lists_declared_resources(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["slug"], "fixture_plugin")
        self.assertEqual(
            {r["name"] for r in body["resources"]}, {"mentions", "ghost"}
        )


class DispatcherAuthAndScopeTests(_PluginFixtureBase):
    def test_missing_token_401_with_cors(self):
        resp = self.client.get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_invalid_token_401(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/", token="bogus")
        self.assertEqual(resp.status_code, 401)

    def test_expired_token_401(self):
        self.token.expires_at = timezone.now() - timedelta(minutes=1)
        self.token.save(update_fields=["expires_at"])
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 401)

    def test_inactive_token_401(self):
        self.token.is_active = False
        self.token.save(update_fields=["is_active"])
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 401)

    def test_datastore_scoped_tokens_403_on_dispatch_and_discovery(self):
        store = DataStore.objects.create(name="s", workspace=self.ws)
        legacy_global = APIToken.objects.create(
            name="g", token=APIToken.generate_token(), workspace=self.ws
        )
        legacy_scoped = APIToken.objects.create(
            name="s", token=APIToken.generate_token(), datastore=store
        )
        for token in (legacy_global, legacy_scoped):
            for url in ("/api/v1/plugins/", "/api/v1/plugins/fixture_plugin/mentions/"):
                resp = self._get(url, token=token.token)
                self.assertEqual(resp.status_code, 403, url)
                self.assertEqual(resp.json()["error"]["code"], "SCOPE_MISMATCH")
                self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_wrong_plugin_slug_403(self):
        other = APIToken.objects.create(
            name="o",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug="other_plugin",
            workspace=self.ws,
        )
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/", token=other.token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "SCOPE_MISMATCH")

    def test_null_workspace_plugin_token_403_fail_closed(self):
        # The token's workspace was deleted (FK is SET_NULL): NEVER fall back
        # to the default workspace — that's a cross-tenant leak.
        self.token.workspace = None
        self.token.save(update_fields=["workspace"])
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "SCOPE_MISMATCH")

    def test_unloaded_plugin_404(self):
        ghost = APIToken.objects.create(
            name="gh",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug="ghost_plugin",
            workspace=self.ws,
        )
        resp = self._get("/api/v1/plugins/ghost_plugin/mentions/", token=ghost.token)
        self.assertEqual(resp.status_code, 404)

    def test_unloaded_plugin_discovery_is_empty_200(self):
        ghost = APIToken.objects.create(
            name="gh",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug="ghost_plugin",
            workspace=self.ws,
        )
        resp = self._get("/api/v1/plugins/", token=ghost.token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"plugins": [], "count": 0})

    @override_settings(PLUGIN_API_AUTHFAIL_RATE_LIMIT=3)
    def test_authfail_throttle_cuts_off_scanning(self):
        # Failures 1..3 burn the budget (401 each); failure 4 tips the counter
        # (429); request 5 is rejected BEFORE the DB lookup — even with a
        # valid token, because the IP itself is cut off.
        for _ in range(3):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/", token="guess")
            self.assertEqual(resp.status_code, 401)
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/", token="guess")
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp)
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/")
        self.assertEqual(resp.status_code, 429)


class DispatcherResourceRulesTests(_PluginFixtureBase):
    def test_undeclared_resource_404_even_with_handler(self):
        # "undeclared" has a marked handler but no manifest entry: the manifest
        # stays truthful, same discipline as provisions.
        resp = self._get("/api/v1/plugins/fixture_plugin/undeclared/")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_resource_404(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/nope/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_post_405(self):
        resp = self.client.post(
            "/api/v1/plugins/fixture_plugin/mentions/", **_auth(self.token.token)
        )
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_head_405(self):
        resp = self.client.head(
            "/api/v1/plugins/fixture_plugin/mentions/", **_auth(self.token.token)
        )
        self.assertEqual(resp.status_code, 405)

    def test_declared_but_unregistered_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/ghost/")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")


class DispatcherErrorIsolationTests(_PluginFixtureBase):
    def test_apierror_maps_to_its_status_and_code(self):
        resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?apierror=1")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "NOT_CONFIGURED")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_apierror_redirect_status_clamped_to_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?redirect=1")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")

    def test_apierror_fake_5xx_clamped_to_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?fake5xx=1")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")

    def test_handler_crash_500_generic_no_internals(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?boom=1")
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "PLUGIN_ERROR")
        self.assertNotIn("internal secret detail", json.dumps(body))
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_non_dict_result_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?notdict=1")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")

    def test_unserializable_result_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get(
                "/api/v1/plugins/fixture_plugin/mentions/?unserializable=1"
            )
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")

    @override_settings(PLUGIN_API_MAX_RESPONSE_BYTES=1000)
    def test_response_size_cap(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/fixture_plugin/mentions/?big=1")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "RESPONSE_TOO_LARGE")


class DispatcherRateLimitTests(_PluginFixtureBase):
    @override_settings(API_RATE_LIMIT=2)
    def test_per_token_limit_429_with_retry_after(self):
        url = "/api/v1/plugins/fixture_plugin/mentions/"
        self.assertEqual(self._get(url).status_code, 200)
        self.assertEqual(self._get(url).status_code, 200)
        resp = self._get(url)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "RATE_LIMITED")
        retry_after = int(resp["Retry-After"])
        self.assertTrue(1 <= retry_after <= 60)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    @override_settings(PLUGIN_API_PLUGIN_RATE_LIMIT=2)
    def test_per_plugin_limit_shared_across_tokens(self):
        url = "/api/v1/plugins/fixture_plugin/mentions/"
        other = APIToken.objects.create(
            name="p2",
            token=APIToken.generate_token(),
            scope=APIToken.Scope.PLUGIN,
            plugin_slug=self.slug,
            workspace=self.ws,
        )
        self.assertEqual(self._get(url).status_code, 200)
        self.assertEqual(self._get(url, token=other.token).status_code, 200)
        resp = self._get(url)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp)


class BrokenPluginApiModuleTests(_PluginFixtureBase):
    slug = "broken_plugin"
    manifest = {
        "slug": "broken_plugin",
        "name": "Broken",
        "version": "0.1.0",
        "provides": {
            "api_resources": [{"name": "things", "summary": "", "methods": ["GET"]}]
        },
    }
    api_source = "raise RuntimeError('import-time explosion')\n"

    def setUp(self):
        super().setUp()
        self.token.plugin_slug = "broken_plugin"
        self.token.save(update_fields=["plugin_slug"])

    def test_broken_api_import_is_guarded_503(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/broken_plugin/things/")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_API_UNAVAILABLE")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")


class MissingApiModuleTests(_PluginFixtureBase):
    slug = "no_api_plugin"
    manifest = {
        "slug": "no_api_plugin",
        "name": "NoApi",
        "version": "0.1.0",
        "provides": {
            "api_resources": [{"name": "things", "summary": "", "methods": ["GET"]}]
        },
    }
    api_source = None  # no api.py on disk at all

    def setUp(self):
        super().setUp()
        self.token.plugin_slug = "no_api_plugin"
        self.token.save(update_fields=["plugin_slug"])

    def test_declared_without_api_module_500(self):
        with self.assertLogs("core.views.api.plugins", level="ERROR"):
            resp = self._get("/api/v1/plugins/no_api_plugin/things/")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "PLUGIN_ERROR")


class DatastoreCorsPreflightDriveByTests(TestCase):
    """Stage 2 drive-by: the shipped datastore API's broken CORS preflight.

    Before: decorator order ran auth before the OPTIONS branch, so a browser
    preflight (which carries no auth header) got a 401 with no CORS headers —
    cross-origin browser calls could never complete. Strictly additive fix:
    it only makes previously-impossible calls work.
    """

    def setUp(self):
        _mock_setup(self)

    def test_options_preflight_succeeds_tokenless(self):
        resp = self.client.options("/api/v1/datastores/")
        self.assertEqual(resp.status_code, 204)  # same shape as the plugin API
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_error_responses_carry_cors_headers(self):
        resp = self.client.get("/api/v1/datastores/")  # no token → 401
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

        resp = self.client.get("/api/v1/datastores/", **_auth("bogus"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")


class ChannelAPITests(TestCase):
    """API 2.3 — ChannelAPI.list() is read-only and workspace-scoped."""

    def test_version_bumped(self):
        # This surface landed in 2.3, so that is the FLOOR — asserting equality
        # would make every later additive bump (2.4 = LibraryAPI, ...) fail here
        # for no reason, since a plugin targeting 2.3 keeps working on 2.4.
        version = tuple(int(part) for part in sdk.API_VERSION.split("."))
        self.assertGreaterEqual(version, (2, 3))

    def test_list_scoped_to_workspace(self):
        from core.models import Channel

        ws_a = Workspace.objects.create(name="A")
        ws_b = Workspace.objects.create(name="B")
        Channel.objects.create(
            workspace=ws_a, provider="telegram", name="ops-alerts", enabled=True
        )
        Channel.objects.create(
            workspace=ws_b, provider="slack", name="other-tenant", enabled=False
        )

        listed = sdk.ChannelAPI(owner="fixture_plugin", workspace=ws_a).list()
        self.assertEqual(
            listed,
            [{"name": "ops-alerts", "channel_type": "telegram", "is_enabled": True}],
        )


# =========================================================================== #
# Stage 5 — public pages (/p/<token>/): capability URLs, rotation, CSP
# =========================================================================== #

from core.models import PluginPublicPage
from core.plugins.api import PublicPageAPI

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src * data:"


class PublicPageViewTests(_PluginFixtureBase):
    def _share(self, page="report", **kwargs):
        return PublicPageAPI(owner=self.slug, workspace=self.ws).share(page, **kwargs)

    def test_shared_page_renders_with_hardening_headers(self):
        url = self._share()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Report for W", resp.content.decode())
        self.assertEqual(resp["Content-Security-Policy"], _CSP)
        self.assertEqual(resp["X-Robots-Tag"], "noindex")
        # Other middleware may merge additional directives; no-store must hold.
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertEqual(resp["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Set-Cookie", resp)

    def test_workspace_comes_from_share_row_not_caller(self):
        other_ws = Workspace.objects.create(name="OTHER")
        url = PublicPageAPI(owner=self.slug, workspace=other_ws).share("report")
        resp = self.client.get(url)
        self.assertIn("Report for OTHER", resp.content.decode())

    def test_unknown_token_404(self):
        resp = self.client.get("/p/not-a-real-token/")
        self.assertEqual(resp.status_code, 404)

    def test_revoked_page_404(self):
        url = self._share()
        PublicPageAPI(owner=self.slug, workspace=self.ws).revoke("report")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_expired_page_404(self):
        url = self._share(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_reshare_after_revoke_rotates_token_old_url_stays_dead(self):
        api = PublicPageAPI(owner=self.slug, workspace=self.ws)
        old_url = api.share("report")
        api.revoke("report")
        new_url = api.share("report")
        self.assertNotEqual(old_url, new_url)
        self.assertEqual(self.client.get(old_url).status_code, 404)  # dead forever
        self.assertEqual(self.client.get(new_url).status_code, 200)

    def test_reshare_after_expiry_rotates_token(self):
        api = PublicPageAPI(owner=self.slug, workspace=self.ws)
        old_url = api.share("report", expires_at=timezone.now() - timedelta(minutes=1))
        new_url = api.share("report")
        self.assertNotEqual(old_url, new_url)
        self.assertEqual(self.client.get(old_url).status_code, 404)
        self.assertEqual(self.client.get(new_url).status_code, 200)

    def test_plain_reshare_of_live_page_keeps_url(self):
        api = PublicPageAPI(owner=self.slug, workspace=self.ws)
        self.assertEqual(api.share("report"), api.share("report"))

    def test_plain_reshare_keeps_existing_expiry(self):
        api = PublicPageAPI(owner=self.slug, workspace=self.ws)
        future = timezone.now() + timedelta(days=7)
        api.share("report", expires_at=future)
        api.share("report")  # no expires_at argument → expiry untouched
        row = PluginPublicPage.objects.get(plugin_slug=self.slug, page="report")
        self.assertEqual(row.expires_at, future)
        api.share("report", expires_at=None)  # explicit None → cleared
        row.refresh_from_db()
        self.assertIsNone(row.expires_at)

    def test_share_is_one_row_per_page_and_workspace(self):
        api = PublicPageAPI(owner=self.slug, workspace=self.ws)
        api.share("report")
        api.share("report")
        self.assertEqual(
            PluginPublicPage.objects.filter(
                plugin_slug=self.slug, page="report", workspace=self.ws
            ).count(),
            1,
        )

    def test_workspace_deletion_kills_pages(self):
        doomed = Workspace.objects.create(name="doomed")
        url = PublicPageAPI(owner=self.slug, workspace=doomed).share("report")
        doomed.delete()
        self.assertFalse(PluginPublicPage.objects.filter(workspace_id=doomed.id).exists())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_undeclared_page_404_even_with_handler(self):
        url = self._share("undeclared_page")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_declared_but_unregistered_page_500(self):
        url = self._share("ghost_page")
        with self.assertLogs("core.views.public_pages", level="ERROR"):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 500)

    def test_handler_crash_500_generic(self):
        url = self._share()
        with self.assertLogs("core.views.public_pages", level="ERROR"):
            resp = self.client.get(url + "?boom=1")
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("page internals", resp.content.decode())

    def test_non_string_result_500(self):
        url = self._share()
        with self.assertLogs("core.views.public_pages", level="ERROR"):
            resp = self.client.get(url + "?notstr=1")
        self.assertEqual(resp.status_code, 500)

    @override_settings(PLUGIN_API_MAX_RESPONSE_BYTES=1000)
    def test_size_cap_500(self):
        url = self._share()
        with self.assertLogs("core.views.public_pages", level="ERROR"):
            resp = self.client.get(url + "?big=1")
        self.assertEqual(resp.status_code, 500)

    def test_post_405(self):
        url = self._share()
        self.assertEqual(self.client.post(url).status_code, 405)

    @override_settings(PUBLIC_PAGE_IP_RATE_LIMIT=2)
    def test_per_ip_limit_before_lookup(self):
        url = self._share()
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp)
        # Even a bogus token costs budget, not a DB query, once cut off.
        self.assertEqual(self.client.get("/p/whatever/").status_code, 429)

    def test_last_accessed_write_throttled(self):
        url = self._share()
        self.client.get(url)
        row = PluginPublicPage.objects.get(plugin_slug=self.slug, page="report")
        first = row.last_accessed_at
        self.assertIsNotNone(first)
        self.client.get(url)
        row.refresh_from_db()
        self.assertEqual(row.last_accessed_at, first)  # within 60s → no write

    def test_public_page_api_requires_owner(self):
        with self.assertRaises(ValueError):
            PublicPageAPI(owner=None, workspace=self.ws)


class PublicPageOversightTests(_PluginFixtureBase):
    """Settings → API tokens page lists shares; revoke kills the URL."""

    def setUp(self):
        super().setUp()
        from core.models import User

        self.user = User.objects.create(email="admin@example.com")
        self.client.force_login(self.user)

    def test_listed_and_revocable(self):
        # Share into the user's active (default) workspace — the list view
        # scopes oversight rows to request.workspace.
        active_ws = Workspace.get_default()
        url = PublicPageAPI(owner=self.slug, workspace=active_ws).share("report")
        row = PluginPublicPage.objects.get(plugin_slug=self.slug, page="report")

        page_list = self.client.get(reverse("cpanel:api_token_list"))
        self.assertEqual(page_list.status_code, 200)
        body = page_list.content.decode()
        self.assertIn("Public pages", body)
        self.assertIn(row.token[:20], body)

        resp = self.client.post(
            reverse("cpanel:public_page_revoke", args=[row.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get(url).status_code, 404)
