# Qdrant Backup — plugin guide (for humans & AI agents)

This file is context for anyone (or any AI) working **on** this plugin. It is the
flagship reference plugin for the PyRunner plugin SDK (`core.plugins.api`): a
**self-provisioning** plugin where one form **Save** creates and keeps in sync a
managed Script, its secrets, a data store, and a schedule.

Read this before editing — most of the bugs you can introduce here are silent
cross-layer desyncs, not syntax errors.

## Architecture: two execution contexts

The plugin spans **two separate processes that cannot import each other**:

| Runs in the **web process** | Runs in the **environment venv** (a Script) |
|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | `worker_body.py` (the managed backup script) |
| `provisioning.py` — all SDK calls (provision + reads) | `demo_seed.py` (optional dashboard seed) |

The web layer talks to PyRunner **only** through `core.plugins.api`. The worker
is a standalone script PyRunner runs in an environment; it can import only
stdlib + `requests` + `boto3` + `pyrunner_datastore` (PyRunner injects the last).

## Contracts you must not break

1. **Secret/config names are a cross-process contract by convention.** The web
   side writes secrets under the env keys in `forms.SECRET_FIELDS` and config
   under `provisioning.CONFIG_KEYS`; the worker reads the *same* names from
   `os.environ[...]` and the data store. They are wired only by matching
   strings. `provisioning._worker_code()` fails **loudly at Save** if
   `worker_body.py` stops referencing an expected token — so if you rename a
   secret env var or a config key, **rename it on both sides** or Save breaks
   (by design, to avoid a silently misconfigured backup).
2. **Import-light / SDK-only.** `views.py` and `provisioning.py` go through
   `core.plugins.api` exclusively. Never `import core.models` /
   `core.services` / `core.tasks`. The doctor enforces import-light.
3. **No `models.py`, no `migrations/`.** All persistent state lives in the owned
   DataStore `qdrant_backup:state` (`config` + `runs` + `progress`). The doctor
   **rejects** a plugin that ships models or migrations — use DataStores.
4. **Ownership + idempotency.** Everything is owned by the slug `qdrant_backup`
   (`OWNER`). SDK upserts key on `(owner_plugin, owner_key)`, so re-Save updates
   the same rows. `provision()` must stay idempotent — re-running it must never
   create duplicates.
5. **The managed Script is owned — users configure it via the form, never edit
   it by hand.** Queue/cancel runs through the SDK (`queue_run`,
   `cancel_latest_run`); read state through `latest_run`.
6. **Secrets are write-only in the form.** A blank credential field means "keep
   the existing value" (`forms.__init__` makes them optional once configured;
   `provision()` only writes the fields actually supplied).
7. **Download security invariant.** Never presign an arbitrary object key.
   `resolve_download_key()` validates the requested file against the recorded
   run history; only those keys reach `presigned_url()`. Keep it that way — the
   query string is never trusted.
8. **SSRF guard.** The "Test connection" endpoints route every URL through
   `_endpoint_allowed()` (blocks link-local/metadata/reserved; RFC1918 and
   hostnames are intentionally allowed for self-hosted Qdrant).
9. **Worker portability.** Keep the `sys.stdout.reconfigure(encoding="utf-8")`
   shim — the emoji log lines crash on a non-UTF-8 console otherwise. Derive the
   owner slug from `PYRUNNER_OWNER_PLUGIN` (fallback to the literal slug); don't
   hardcode the store name.

## Keep the manifest truthful

`plugin.json` is metadata the marketplace and doctor read — it must match the code:
- **`api` / `min_pyrunner`** reflect the SDK features actually used. Currently
  **`2.1` / `1.13.0`** because the plugin calls `latest_run` + `cancel_latest_run`
  (the 2.1 run-lifecycle surface). If you drop those, you can lower the floor; if
  you adopt a newer SDK feature, raise it.
- **`provisions`** (scripts/secrets/datastores/schedules + `secret_keys`) must
  match what `provision()` actually creates.

## Versioning

The version lives in **three places** — keep them in sync on every release:
`plugin.json` `"version"`, `apps.py` `PyRunnerPlugin(version=...)`, and
`CHANGELOG.md`. Use semver — PyRunner's update-detection depends on it.

## Develop · validate · package

```bash
# Dev mode — live-edit without uploading (run inside a PyRunner checkout)
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/qdrant_backup
python manage.py runserver

# Validate against the doctor (must be 0-fail before shipping)
python manage.py plugin_doctor --path examples/qdrant_backup

# Package the installable zip (single top-level folder == the slug)
cd examples && zip -r qdrant_backup.zip qdrant_backup -x '*/__pycache__/*'
```

Tests run inside the PyRunner repo with the normal Django test runner (this
plugin is developed in-tree, so `core.plugins.api` is always importable — no SDK
stub needed). Mirror the style of `core/test_plugin_sdk.py`.

## Don't

- Add `models.py` / `migrations/`, or `import core.models|services|tasks`.
- Hardcode the data-store name, or sign an R2 key not found in run history.
- Edit the managed Script directly instead of through the form/SDK.
- Let `plugin.json` `api`/`version`/`provisions` drift from the code.
