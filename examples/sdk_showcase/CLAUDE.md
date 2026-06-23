# SDK Showcase — plugin guide (for humans & AI agents)

Context for anyone working **on** this plugin. It is a teaching reference: each
page card demonstrates one `core.plugins.api` surface, so keep changes
**legible** — a clever abstraction that hides the SDK call defeats the purpose.

## Architecture: two execution contexts

Like every SDK plugin, this spans two processes that cannot import each other:

| Web process | Environment venv (a Script) |
|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | `worker_body.py` (the managed demo script) |
| `provisioning.py` — all SDK calls | |

The web layer talks to PyRunner **only** through `core.plugins.api`. The worker
imports only stdlib + `pyrunner_datastore` (PyRunner injects the last) — **no
third-party packages**, on purpose, so the demo runs in any environment. Don't
add `requests`/`boto3` here.

## Contracts you must not break

1. **Secret/config names are a cross-process contract by convention.** The web
   side writes the secret under `forms.SECRET_FIELDS["demo_token"]`
   (`DEMO_TOKEN`) and config under `provisioning.CONFIG_KEYS` (`message`,
   `steps`); the worker reads the *same* names from `os.environ` and the data
   store. `provisioning._worker_code()` fails **loudly at Save** if
   `worker_body.py` stops referencing an expected token — rename on both sides
   or Save breaks (by design).
2. **Import-light / SDK-only.** `views.py` and `provisioning.py` go through
   `core.plugins.api` exclusively — never `import core.models|services|tasks`.
   The doctor enforces import-light (test files are exempt).
3. **No `models.py` / `migrations/`.** State lives in the owned DataStore
   `sdk_showcase:state` (`config` + `counter` + `runs` + `progress`). The doctor
   rejects models/migrations.
4. **Ownership + idempotency.** Everything is owned by the slug `sdk_showcase`.
   SDK upserts key on `(owner_plugin, owner_key)`, so `provision()` must stay
   idempotent — re-running creates no duplicates.
5. **Keep each card a one-capability demo.** A card should map to a small,
   visible SDK call with its snippet. If you add an API, add a card; don't fold
   several calls behind one button.
6. **There is no SDK `delete`.** "Reset demo data" clears *data* only; the
   provisioned rows are removed when the plugin is uninstalled. Don't reach into
   `core.models` to delete rows (it would break import-light).

## Keep the manifest truthful

`plugin.json` must match the code:
- **`api` / `min_pyrunner`** = `2.1` / `1.13.0` (the run-lifecycle surface).
- **`provisions`** = what `provision()` creates (1 datastore, 1 secret, 1 script,
  1 schedule + the `DEMO_TOKEN` key).

## Versioning

Bump in three places on release: `plugin.json` `"version"`, `apps.py`
`PyRunnerPlugin(version=...)`, and `CHANGELOG.md`. Semver.

## Develop · validate · package

```bash
export DEBUG=True PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/sdk_showcase
python manage.py runserver                                   # dev mode
python manage.py plugin_doctor --path examples/sdk_showcase  # must be 0-fail
cd examples && zip -r sdk_showcase.zip sdk_showcase -x '*/__pycache__/*'
```

Tests run in-tree via `core/test_sdk_showcase_plugin.py` (a splice shim) — mirror
`examples/qdrant_backup/tests.py`.

## Don't

- Add third-party imports to `worker_body.py` (keep it stdlib-only).
- Add `models.py`/`migrations/`, or `import core.models|services|tasks`.
- Hide SDK calls behind abstractions — legibility is the whole point.
- Let `plugin.json` `api`/`version`/`provisions` drift from the code.
