# Brand Tracker — plugin guide (for humans & AI agents)

Context for anyone (or any AI) working **on** this plugin. It is a
**self-provisioning** PyRun plugin (SDK `core.plugins.api`): one form **Save**
creates and keeps in sync a managed Script, its secrets, an owned data store, and
a weekly schedule. It is the reference for a **content-feed** plugin (and the
first example that consumes the Claude AI integration).

Read this before editing — most bugs you can introduce here are silent
cross-layer desyncs, not syntax errors.

## Architecture: two execution contexts

The plugin spans **two separate processes that cannot import each other**:

| Runs in the **web process** | Runs in the **environment venv** (a Script) |
|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | `worker_body.py` (the managed tracker script) |
| `provisioning.py` — all SDK calls (provision + reads) | — |

The web layer talks to PyRun **only** through `core.plugins.api`. The worker
is a standalone script PyRun runs in an environment; it imports only stdlib +
`requests` + `pyrunner_datastore` (injected) + optionally `pyrunner_ai` /
`claude_agent_sdk` for Claude enrichment.

## Contracts you must not break

1. **Secret/config names are a cross-process contract by convention.** The web
   side writes secrets under the env keys in `forms.SECRET_FIELDS` and config
   under `provisioning.CONFIG_KEYS`; the worker reads the *same* names from
   `os.environ[...]` and the data store. `provisioning._worker_code()` fails
   **loudly at Save** if `worker_body.py` stops referencing an expected token —
   so if you rename a secret env var or a config key, **rename it on both sides**.
2. **Import-light / SDK-only.** `views.py` and `provisioning.py` go through
   `core.plugins.api` exclusively — never `import core.models|services|tasks`.
3. **No `models.py`, no `migrations/`.** All state lives in the owned DataStore
   `brand_tracker:state`: `config`, `seen` (dedup index), `mentions` (the feed),
   `stats`, `credits`, `runs`, `progress`.
4. **Ownership + idempotency.** Everything is owned by the slug `brand_tracker`
   (`OWNER`). SDK upserts key on `(owner_plugin, owner_key)`; `provision()` must
   stay idempotent — re-running it must never create duplicates.
5. **The managed Script is owned — users configure it via the form, never edit
   it by hand.** Queue/cancel through the SDK; read state via `latest_run`.
6. **Secrets are write-only in the form.** A blank credential field means "keep
   the existing value" (`provision()` only writes fields actually supplied).

## Engine invariants (the bug-prone parts)

- **Canonical dedup is the correctness core.** `canonical_url()` collapses
  scheme/`www`/`m.`/AMP/`utm_*`/click-IDs so the *same article via web and news
  is one mention*. Don't weaken it without updating `CanonicalUrlTests`.
- **Domain exclusion is subdomain-aware, not substring** (`example.com` blocks
  `blog.example.com`, not `notexample.com`). See `ExcludedDomainTests`.
- **Weekly cadence ⇄ `qdr:w` window.** v1 runs weekly and searches the past
  week, so a missed/late-indexed mention is still caught next run. If you change
  the cadence, change the window to match (a window narrower than the gap
  permanently misses late-indexed pages).
- **Retention prunes `seen` + `mentions` to `retention_days`** (default 90) every
  run, capped at `MAX_MENTIONS`. `seen` is the durable dedup index; a mention
  older than the window may re-report (accepted).
- **First run seeds silently.** `stats.seeded` gates the content email; the feed
  is always populated.
- **Credits.** Only Serper web + news cost credits (1 at `num<=10`, else 2),
  tracked per month in `credits`; `monthly_credit_cap` pauses Serper (free
  sources keep running). The form's live estimate must match this formula.
- **Enrichment degrades, never breaks.** `enrich_mentions()` returns the list
  unchanged if the provider is off/unavailable or a batch fails; only NEW
  mentions, batched `ENRICH_BATCH`, capped at `ENRICH_MAX`.

## Keep the manifest truthful

`plugin.json` is metadata the marketplace + doctor read — it must match the code:
- **`api` / `min_pyrunner`** are `2.1` / `1.13.0` because the dashboard uses the
  run-lifecycle surface (`latest_run` / `cancel_latest_run`).
- **`provisions`** (scripts/secrets/datastores/schedules + `secret_keys`) is the
  *upper bound* — `secrets: 5` lists every key the plugin *may* create; a minimal
  install creates only `SERPER_API_KEY`.

## Versioning

The version lives in **three places** — keep them in sync on every release:
`plugin.json` `"version"`, `apps.py` `PyRunnerPlugin(version=...)`, and
`CHANGELOG.md`. Use semver.

## Develop · validate · package

```bash
# Dev mode — live-edit without uploading (run inside a PyRun checkout)
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/brand_tracker
python manage.py runserver

# Validate against the doctor (must be 0-fail before shipping)
python manage.py plugin_doctor --path examples/brand_tracker

# Run the tests
python manage.py test core.test_brand_tracker_plugin

# Package the installable zip (single top-level folder == the slug)
cd examples && zip -r brand_tracker.zip brand_tracker -x '*/__pycache__/*'
```

Tests are developed in-tree (so `core.plugins.api` is importable — no SDK stub).
The worker is import-safe (`load_plugin_config` guards the data-store read), so
its pure helpers are unit-tested directly; networked source/provider calls are
verified by real runs, not unit-mocked.

## Don't

- Add `models.py` / `migrations/`, or `import core.models|services|tasks`.
- Hardcode the data-store name (derive it from `PYRUNNER_OWNER_PLUGIN`).
- Edit the managed Script directly instead of through the form/SDK.
- Let `plugin.json` `api`/`version`/`provisions` drift from the code.
- Promise walled-social coverage — search APIs can't reach it.
