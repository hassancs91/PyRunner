"""
Tests for the Brand Tracker plugin.

These run inside the PyRunner repo with the normal Django test runner — the
plugin is developed in-tree, so ``core.plugins.api`` is importable and the SDK is
exercised for real (no fakes). They are imported into the main suite by the thin
shim ``core/test_brand_tracker_plugin.py``, which splices ``examples/`` onto the
``plugins`` package path (exactly as Dev Mode does) so this module loads as
``plugins.brand_tracker.tests`` and the relative imports below resolve.

Coverage: the cross-process worker contract, the worker's pure dedup/retention
helpers (where a bug silently double-reports or never expires data), and
idempotent provisioning through the real SDK. The networked source functions are
verified by real runs, not unit-mocked here.
"""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from . import provisioning as prov
from . import worker_body as wb
from .forms import SECRET_FIELDS, BrandTrackerConfigForm


# --------------------------------------------------------------------------- #
# Cross-process worker contract — secret env names + config keys are wired to the
# standalone worker_body by convention; _worker_code() must fail loudly at Save
# if they drift, never ship a silently misconfigured tracker.
# --------------------------------------------------------------------------- #

class WorkerContractTests(SimpleTestCase):
    def test_shipped_worker_references_every_secret_and_config_key(self):
        code = prov._worker_code()
        for token in list(SECRET_FIELDS.values()) + list(prov.CONFIG_KEYS):
            self.assertIn(token, code, f"worker_body.py is missing reference to {token}")

    def test_drift_raises_loudly(self):
        with mock.patch.object(prov, "CONFIG_KEYS", prov.CONFIG_KEYS + ("zzz_unreferenced",)):
            with self.assertRaises(ValueError) as cm:
                prov._worker_code()
        self.assertIn("zzz_unreferenced", str(cm.exception))


# --------------------------------------------------------------------------- #
# Canonicalization + dedup — THE correctness invariant: the same article via web
# and news (http/https, www/m, amp, utm tags) must collapse to ONE key.
# --------------------------------------------------------------------------- #

class CanonicalUrlTests(SimpleTestCase):
    def test_scheme_host_trailing_and_tracking_collapse(self):
        a = wb.canonical_url("http://www.example.com/Article?utm_source=news&utm_medium=x")
        b = wb.canonical_url("https://example.com/Article/")
        self.assertEqual(a, b)

    def test_amp_and_mobile_host_collapse(self):
        self.assertEqual(
            wb.canonical_url("https://m.example.com/news/amp"),
            wb.canonical_url("https://example.com/news"),
        )

    def test_click_id_params_dropped_but_real_params_kept(self):
        self.assertEqual(
            wb.canonical_url("https://x.com/p?id=42&fbclid=abc&gclid=z"),
            "https://x.com/p?id=42",
        )

    def test_distinct_paths_stay_distinct(self):
        self.assertNotEqual(
            wb.canonical_url("https://example.com/a"),
            wb.canonical_url("https://example.com/b"),
        )

    def test_web_and_news_mention_of_same_article_dedupe(self):
        web = wb._mention("k", "T", "http://www.site.com/story?utm_campaign=a", "s", "web")
        news = wb._mention("k", "T", "https://site.com/story/", "s", "news")
        self.assertEqual(web["canonical"], news["canonical"])


class ExcludedDomainTests(SimpleTestCase):
    def test_exact_and_subdomain_excluded(self):
        self.assertTrue(wb.is_excluded_domain("https://example.com/x", ["example.com"]))
        self.assertTrue(wb.is_excluded_domain("https://blog.example.com/x", ["example.com"]))

    def test_substring_lookalikes_not_excluded(self):
        # The bug the prototype had: substring match wrongly blocks these.
        self.assertFalse(wb.is_excluded_domain("https://notexample.com/x", ["example.com"]))
        self.assertFalse(wb.is_excluded_domain("https://example.com.evil.com/x", ["example.com"]))

    def test_blank_excludes_ignored(self):
        self.assertFalse(wb.is_excluded_domain("https://example.com", ["", "  "]))


class HelperTests(SimpleTestCase):
    def test_matches_keyword(self):
        self.assertTrue(wb.matches_keyword("Foo", "this has FoO inside"))
        self.assertFalse(wb.matches_keyword("Foo", "bar baz"))

    def test_prune_window_drops_old_items(self):
        items = [
            {"found_at": "2026-01-01T00:00:00"},
            {"found_at": "2026-06-20T00:00:00"},
            {"found_at": ""},
        ]
        kept = wb.prune_window(items, "2026-03-01T00:00:00")
        self.assertEqual(kept, [{"found_at": "2026-06-20T00:00:00"}])


# --------------------------------------------------------------------------- #
# AI enrichment — provider gating, JSON parsing, batching/ceiling, and graceful
# degrade. The networked provider call (_classify) is mocked; everything else is
# the real worker logic.
# --------------------------------------------------------------------------- #

class EnrichmentTests(SimpleTestCase):
    def _mentions(self, n):
        return [wb._mention("k", f"T{i}", f"https://x.com/{i}", "s", "web") for i in range(n)]

    def test_parse_tolerates_shapes_and_validates(self):
        arr = wb._parse_classifications('[{"i":0,"source_type":"news","sentiment":"positive"}]')
        self.assertEqual(arr[0], {"source_type": "news", "sentiment": "positive"})
        fenced = wb._parse_classifications('```json\n{"results":[{"i":0,"source_type":"BOGUS","sentiment":"x"}]}\n```')
        self.assertEqual(fenced[0], {"source_type": "other", "sentiment": "neutral"})  # invalid → fallback
        self.assertEqual(wb._parse_classifications("not json at all"), {})

    def test_off_is_a_noop(self):
        with mock.patch.object(wb, "ENRICH_PROVIDER", "off"):
            ms = self._mentions(2)
            wb.enrich_mentions(ms)
        self.assertEqual(ms[0]["source_type"], "")

    def test_applies_tags_in_order(self):
        canned = json.dumps({"results": [
            {"i": 0, "source_type": "blog", "sentiment": "positive"},
            {"i": 1, "source_type": "forum", "sentiment": "negative"},
        ]})
        with mock.patch.object(wb, "ENRICH_PROVIDER", "openrouter"), \
                mock.patch.object(wb, "OPENROUTER_API_KEY", "key"), \
                mock.patch.object(wb, "_classify", return_value=canned):
            ms = self._mentions(2)
            wb.enrich_mentions(ms)
        self.assertEqual(ms[0]["sentiment"], "positive")
        self.assertEqual(ms[1]["source_type"], "forum")

    def test_unavailable_provider_degrades(self):
        with mock.patch.object(wb, "ENRICH_PROVIDER", "openrouter"), \
                mock.patch.object(wb, "OPENROUTER_API_KEY", ""):
            ms = self._mentions(2)
            wb.enrich_mentions(ms)  # must not raise
        self.assertEqual(ms[0]["source_type"], "")

    def test_batch_failure_degrades(self):
        with mock.patch.object(wb, "ENRICH_PROVIDER", "openrouter"), \
                mock.patch.object(wb, "OPENROUTER_API_KEY", "key"), \
                mock.patch.object(wb, "_classify", side_effect=RuntimeError("boom")):
            ms = self._mentions(2)
            wb.enrich_mentions(ms)  # must not raise
        self.assertEqual(ms[0]["source_type"], "")

    def test_per_run_ceiling(self):
        canned = json.dumps({"results": [
            {"i": i, "source_type": "blog", "sentiment": "neutral"} for i in range(wb.ENRICH_BATCH)
        ]})
        with mock.patch.object(wb, "ENRICH_PROVIDER", "openrouter"), \
                mock.patch.object(wb, "OPENROUTER_API_KEY", "key"), \
                mock.patch.object(wb, "_classify", return_value=canned):
            ms = self._mentions(wb.ENRICH_MAX + 20)
            wb.enrich_mentions(ms)
        self.assertEqual(ms[wb.ENRICH_MAX - 1]["source_type"], "blog")   # within ceiling
        self.assertEqual(ms[wb.ENRICH_MAX]["source_type"], "")           # beyond ceiling


# --------------------------------------------------------------------------- #
# Config form — required fields, schedule/time + credit bounds, the email-report
# trio, and the first-setup vs. already-configured credential requirement.
# --------------------------------------------------------------------------- #

class ConfigFormTests(SimpleTestCase):
    ENVS = [SimpleNamespace(name="prod")]

    def _form(self, *, configured=frozenset(), **over):
        data = {
            "keywords": "SimplerLLM\nPyRunner",
            "excluded_domains": "",
            "num_results": "10",
            "news_enabled": "on",
            "serper_api_key": "sk",
            "retention_days": "90",
            "monthly_credit_cap": "0",
            "enrich_provider": "off",
            "environment": "prod",
            "notify_on": "failure",
            "schedule_weekday": "0",
            "schedule_time": "08:00",
            "timezone": "UTC",
        }
        data.update(over)
        return BrandTrackerConfigForm(
            data, environments=self.ENVS, configured_secrets=set(configured)
        )

    def test_valid_form(self):
        self.assertTrue(self._form().is_valid())

    def test_keywords_required(self):
        form = self._form(keywords="   \n  ")
        self.assertFalse(form.is_valid())
        self.assertIn("keywords", form.errors)

    def test_serper_required_on_first_setup(self):
        form = self._form(serper_api_key="")
        self.assertFalse(form.is_valid())
        self.assertIn("serper_api_key", form.errors)

    def test_serper_optional_once_configured(self):
        form = self._form(configured={"SERPER_API_KEY"}, serper_api_key="")
        self.assertTrue(form.is_valid(), form.errors)

    def test_bad_time_rejected(self):
        for bad in ("25:00", "12:60", "0800", "8am"):
            form = self._form(schedule_time=bad)
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("schedule_time", form.errors)

    def test_num_results_bounds(self):
        self.assertFalse(self._form(num_results="0").is_valid())
        self.assertFalse(self._form(num_results="101").is_valid())

    def test_email_report_requires_destination_sender_and_key(self):
        form = self._form(email_enabled="on")
        self.assertFalse(form.is_valid())
        for field in ("email_to", "email_from", "resend_api_key"):
            self.assertIn(field, form.errors)

    def test_email_report_valid_when_complete(self):
        form = self._form(
            email_enabled="on", email_to="me@example.com",
            email_from="alerts@example.com", resend_api_key="re-key",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_timezone_defaults_to_utc(self):
        form = self._form(timezone="")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["timezone"], "UTC")

    def test_openrouter_enrichment_requires_key_and_model(self):
        form = self._form(enrich_provider="openrouter")
        self.assertFalse(form.is_valid())
        self.assertIn("openrouter_api_key", form.errors)
        self.assertIn("enrich_model", form.errors)

    def test_openrouter_enrichment_valid_when_complete(self):
        form = self._form(
            enrich_provider="openrouter",
            openrouter_api_key="or-key", enrich_model="openai/gpt-4o-mini",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_claude_enrichment_needs_no_key(self):
        form = self._form(enrich_provider="claude")
        self.assertTrue(form.is_valid(), form.errors)


# --------------------------------------------------------------------------- #
# Provisioning — one Save idempotently creates exactly the declared resources,
# all owned by the plugin slug, through the real SDK.
# --------------------------------------------------------------------------- #

class ProvisionTests(TestCase):
    def setUp(self):
        from core.models import Environment, Workspace

        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(
            name="prod", path="btenv", requirements="requests"
        )
        patch = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        patch.start()
        self.addCleanup(patch.stop)

    def _data(self, **over):
        data = {
            "keywords": "SimplerLLM\nPyRunner",
            "excluded_domains": "learnwithhasan.com",
            "news_enabled": True,
            "hackernews_enabled": True,
            "reddit_enabled": False,
            "num_results": 10,
            "retention_days": 90,
            "monthly_credit_cap": 0,
            "email_enabled": False,
            "email_to": "",
            "email_from": "",
            "serper_api_key": "sk-serper",
            "environment": "prod",
            "notify_on": "failure",
            "notify_email": "",
            "schedule_time": "08:00",
            "schedule_weekday": "0",
            "timezone": "UTC",
        }
        data.update(over)
        return data

    def _counts(self):
        from core.models import DataStore, Script, ScriptSchedule, Secret, SecretGrant

        script = Script.objects.get(owner_plugin=prov.OWNER, owner_key=prov.SCRIPT_KEY)
        return {
            "scripts": Script.objects.filter(owner_plugin=prov.OWNER).count(),
            "secrets": Secret.objects.filter(owner_plugin=prov.OWNER).count(),
            "stores": DataStore.objects.filter(name=f"{prov.OWNER}:{prov.STORE_KEY}").count(),
            "grants": SecretGrant.objects.filter(script=script).count(),
            "schedules": ScriptSchedule.objects.filter(script=script).count(),
        }

    def test_provision_creates_declared_resources(self):
        from core.models import Script

        script, warnings = prov.provision(self._data())
        self.assertEqual(warnings, [])  # env has requests, reddit off
        self.assertEqual(script.name, prov.SCRIPT_NAME)
        self.assertEqual(script.injection_mode, Script.InjectionMode.SELECTED)
        # Only the required Serper secret was supplied.
        self.assertEqual(self._counts(),
                         {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        cfg = prov.get_config()
        self.assertEqual(cfg["keywords"], ["SimplerLLM", "PyRunner"])
        self.assertEqual(cfg["excluded_domains"], ["learnwithhasan.com"])
        self.assertEqual(cfg["retention_days"], 90)

    def test_optional_secrets_create_and_grant(self):
        prov.provision(self._data(
            reddit_enabled=True,
            reddit_client_id="rid", reddit_client_secret="rsec",
            email_enabled=True, resend_api_key="re-key",
            email_to="me@example.com", email_from="alerts@example.com",
            enrich_provider="openrouter", enrich_model="openai/gpt-4o-mini",
            openrouter_api_key="or-key",
        ))
        c = self._counts()
        self.assertEqual(c["secrets"], 5)   # serper + 2 reddit + resend + openrouter
        self.assertEqual(c["grants"], 5)
        self.assertEqual(set(prov.configured_secret_keys()), set(SECRET_FIELDS.values()))

    def test_provision_is_idempotent(self):
        prov.provision(self._data())
        prov.provision(self._data(retention_days=30))
        self.assertEqual(self._counts(),
                         {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        self.assertEqual(prov.get_config()["retention_days"], 30)

    def test_blank_credential_keeps_existing_value(self):
        from core.models import Secret

        prov.provision(self._data())
        prov.provision(self._data(serper_api_key=""))
        secret = Secret.objects.get(owner_plugin=prov.OWNER, owner_key="SERPER_API_KEY")
        self.assertEqual(secret.get_decrypted_value(), "sk-serper")
        self.assertEqual(Secret.objects.filter(owner_plugin=prov.OWNER).count(), 1)

    def test_environment_missing_requests_warns(self):
        from core.models import Environment

        Environment.objects.create(name="bare", path="bareenv", requirements="")
        _, warnings = prov.provision(self._data(environment="bare"))
        self.assertTrue(any("requests" in w for w in warnings))

    def test_reddit_without_credentials_warns(self):
        _, warnings = prov.provision(self._data(reddit_enabled=True))
        self.assertTrue(any("Reddit" in w for w in warnings))

    def test_unknown_environment_raises(self):
        with self.assertRaises(ValueError):
            prov.provision(self._data(environment="ghost"))

    def test_weekly_schedule_created(self):
        prov.provision(self._data(schedule_weekday="2", schedule_time="09:30"))
        sched = prov.get_schedule()
        self.assertIsNotNone(sched)
        self.assertEqual(sched.run_mode, "weekly")


# --------------------------------------------------------------------------- #
# External API (SDK 2.3) — the mentions/stats handlers + the real dispatcher
# route. THE plugin-api acceptance path: no core special-casing anywhere.
# --------------------------------------------------------------------------- #

_MENTIONS = [
    {"keyword": "PyRunner", "title": "Old post", "url": "https://a.example/1",
     "snippet": "s1", "source": "web", "source_type": "blog",
     "sentiment": "neutral", "found_at": "2026-07-01T08:00:00"},
    {"keyword": "SimplerLLM", "title": "HN thread", "url": "https://b.example/2",
     "snippet": "s2", "source": "hackernews", "source_type": "forum",
     "sentiment": "positive", "found_at": "2026-07-10T08:00:00"},
    {"keyword": "PyRunner", "title": "Fresh review", "url": "https://c.example/3",
     "snippet": "s3", "source": "news", "source_type": "news",
     "sentiment": "positive", "found_at": "2026-07-14T08:00:00"},
]

_STATS = {
    "last_run": "2026-07-14T09:00:00",
    "window_total": 3,
    "total_all_time": 41,
    "by_keyword": {"PyRunner": 2, "SimplerLLM": 1},
    "by_source": {"web": 1, "hackernews": 1, "news": 1},
}


def _seed_state(workspace):
    from core.plugins.api import DataStoreAPI

    store = DataStoreAPI(owner=prov.OWNER, workspace=workspace).upsert(prov.STORE_KEY)
    store.set("mentions", _MENTIONS)  # worker order: newest LAST
    store.set("stats", _STATS)
    store.set("runs", [{"ts": "2026-07-14 09:00", "new_count": 2, "status": "success"}])
    return store


def _api_request(workspace, resource_name, params=None):
    from core.plugins.api import APIRequest

    return APIRequest(
        workspace=workspace, resource=resource_name, item_id=None,
        method="GET", params=params or {}, params_list={},
    )


class ApiHandlerTests(TestCase):
    """Unit level — the handlers against a seeded owned store."""

    def setUp(self):
        from core.models import Workspace

        self.ws = Workspace.get_default()
        _seed_state(self.ws)

    def test_mentions_newest_first_and_shape(self):
        from . import api

        body = api.mentions(_api_request(self.ws, "mentions"))
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["mentions"][0]["title"], "Fresh review")
        self.assertEqual(
            set(body["mentions"][0]),
            {"keyword", "title", "url", "snippet", "source", "source_type",
             "sentiment", "found_at"},
        )

    def test_mentions_filters(self):
        from . import api

        by_kw = api.mentions(_api_request(self.ws, "mentions", {"keyword": "PyRunner"}))
        self.assertEqual(by_kw["count"], 2)
        by_src = api.mentions(_api_request(self.ws, "mentions", {"source": "news"}))
        self.assertEqual(by_src["count"], 1)
        by_sent = api.mentions(
            _api_request(self.ws, "mentions", {"sentiment": "positive"})
        )
        self.assertEqual(by_sent["count"], 2)
        since = api.mentions(_api_request(self.ws, "mentions", {"since": "2026-07-10"}))
        self.assertEqual(since["count"], 2)

    def test_mentions_pagination(self):
        from . import api

        body = api.mentions(
            _api_request(self.ws, "mentions", {"page": "2", "page_size": "1"})
        )
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["total_pages"], 3)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["mentions"][0]["title"], "HN thread")

    def test_mentions_bad_page_param_is_clean_apierror(self):
        from core.plugins.api import APIError

        from . import api

        with self.assertRaises(APIError) as ctx:
            api.mentions(_api_request(self.ws, "mentions", {"page": "x"}))
        self.assertEqual(ctx.exception.status, 400)

    def test_unconfigured_workspace_raises_not_configured(self):
        from core.models import Workspace
        from core.plugins.api import APIError

        from . import api

        other = Workspace.objects.create(name="empty")
        with self.assertRaises(APIError) as ctx:
            api.mentions(_api_request(other, "mentions"))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "NOT_CONFIGURED")

    def test_stats_counters(self):
        from . import api

        body = api.stats(_api_request(self.ws, "stats"))
        self.assertEqual(body["total_all_time"], 41)
        self.assertEqual(body["by_keyword"], {"PyRunner": 2, "SimplerLLM": 1})
        self.assertEqual(len(body["runs"]), 1)


class ApiDispatchTests(TestCase):
    """End-to-end — the real /api/v1/plugins/brand_tracker/ route, a real
    plugin-scoped token, and workspace scoping through the dispatcher."""

    def setUp(self):
        from django.test import override_settings

        from core.models import APIToken, Workspace

        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        override = override_settings(INSTALLED_PLUGINS=["plugins.brand_tracker"])
        override.enable()
        self.addCleanup(override.disable)
        from core.views.api import plugins as plugin_views

        self.addCleanup(plugin_views._manifest_cache.clear)
        self.addCleanup(plugin_views._handlers_cache.clear)

        self.ws = Workspace.get_default()
        _seed_state(self.ws)
        self.token = APIToken.objects.create(
            name="bt", token=APIToken.generate_token(), scope="plugin",
            plugin_slug="brand_tracker", workspace=self.ws,
        )

    def _get(self, url, token=None):
        return self.client.get(
            url, HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}"
        )

    def test_mentions_feed_over_http(self):
        resp = self._get(
            "/api/v1/plugins/brand_tracker/mentions/?keyword=PyRunner&page_size=1"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["mentions"][0]["title"], "Fresh review")

    def test_stats_over_http(self):
        resp = self._get("/api/v1/plugins/brand_tracker/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total_all_time"], 41)

    def test_workspace_isolation_through_token(self):
        from core.models import APIToken, Workspace

        other_ws = Workspace.objects.create(name="tenant-b")
        other_token = APIToken.objects.create(
            name="bt-b", token=APIToken.generate_token(), scope="plugin",
            plugin_slug="brand_tracker", workspace=other_ws,
        )
        resp = self._get(
            "/api/v1/plugins/brand_tracker/mentions/", token=other_token.token
        )
        # tenant-b has no brand_tracker store: 409, never the default
        # workspace's feed.
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "NOT_CONFIGURED")

    def test_wrong_scope_token_rejected(self):
        from core.models import APIToken

        legacy = APIToken.objects.create(
            name="legacy", token=APIToken.generate_token(), workspace=self.ws
        )
        resp = self._get("/api/v1/plugins/brand_tracker/mentions/", token=legacy.token)
        self.assertEqual(resp.status_code, 403)

    def test_discovery_shows_brand_tracker(self):
        resp = self._get("/api/v1/plugins/")
        self.assertEqual(resp.status_code, 200)
        plugin = resp.json()["plugins"][0]
        self.assertEqual(plugin["slug"], "brand_tracker")
        self.assertEqual(
            {r["name"] for r in plugin["resources"]}, {"mentions", "stats"}
        )


class DoctorCleanTests(SimpleTestCase):
    """The shipped folder must stay doctor-0-fail (ship gate in CLAUDE.md)."""

    def test_doctor_zero_fail(self):
        from pathlib import Path

        from core.services.plugin_doctor import run_doctor

        report = run_doctor(Path(__file__).resolve().parent)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.fail_count, 0, report.format())


# --------------------------------------------------------------------------- #
# Public report page (Stage 5) — the @page handler + share/revoke flow.
# --------------------------------------------------------------------------- #

class PublicReportPageTests(TestCase):
    def setUp(self):
        from pathlib import Path

        from django.conf import settings
        from django.test import override_settings

        from core.models import Workspace

        # In production the plugin is an installed app, so the app-directories
        # template loader finds its templates; the test harness only splices
        # the import path, so point the filesystem loader at them explicitly.
        templates = [dict(cfg) for cfg in settings.TEMPLATES]
        templates[0]["DIRS"] = list(templates[0].get("DIRS", [])) + [
            Path(__file__).resolve().parent / "templates"
        ]
        override = override_settings(TEMPLATES=templates)
        override.enable()
        self.addCleanup(override.disable)

        self.ws = Workspace.get_default()
        _seed_state(self.ws)

    def _page_request(self, workspace=None):
        from core.plugins.api import PageRequest

        return PageRequest(workspace=workspace or self.ws, page="report", params={})

    def test_report_renders_escaped_feed(self):
        from . import api

        html = api.report(self._page_request())
        self.assertIn("Fresh review", html)
        self.assertIn("Brand mentions report", html)
        self.assertNotIn("<script", html.lower())

    def test_report_escapes_poisoned_titles(self):
        # A scraped title is attacker-controlled: it must come out escaped.
        from core.plugins.api import DataStoreAPI

        from . import api

        store = DataStoreAPI(owner=prov.OWNER, workspace=self.ws).get(prov.STORE_KEY)
        store.set("mentions", [{
            "keyword": "PyRunner", "title": '<script>alert(1)</script>',
            "url": "https://x.example/", "snippet": "", "source": "web",
            "source_type": "", "sentiment": "", "found_at": "2026-07-14T08:00:00",
        }])
        html = api.report(self._page_request())
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_report_unconfigured_workspace_renders_empty_state(self):
        from core.models import Workspace

        from . import api

        other = Workspace.objects.create(name="fresh")
        html = api.report(self._page_request(workspace=other))
        self.assertIn("no data yet", html)

    def test_share_and_revoke_round_trip(self):
        url = prov.share_report()
        self.assertTrue(url.startswith("/p/"))
        share = prov.report_share()
        self.assertTrue(share["is_active"])
        self.assertTrue(prov.revoke_report())
        self.assertFalse(prov.report_share()["is_active"])
        # Re-share rotates: a revoked URL never resurrects.
        new_url = prov.share_report()
        self.assertNotEqual(url, new_url)
