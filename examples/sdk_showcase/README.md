# SDK Showcase (PyRun plugin)

A **live tour of the PyRun plugin SDK** (`core.plugins.api`). One page, one
card per capability — each with a working action and the few lines of code
behind it. It's the companion to the [qdrant_backup](../qdrant_backup/) plugin:
where qdrant_backup shows *a real plugin end-to-end*, this shows *each SDK call
in isolation* so you can lift the pattern into your own plugin.

It is **self-provisioning** like every SDK plugin: one **Set up demo** save
creates and keeps in sync a data store, an owner-scoped secret, a managed script,
and a schedule — all owned by the `sdk_showcase` slug, all idempotent on re-save.

## What it demonstrates

| Card | SDK surface |
|---|---|
| **Set up** | `DataStoreAPI` · `SecretAPI` · `ScriptAPI` · `ScheduleAPI` · `EnvironmentAPI` — the whole provisioning chain in `provision()` |
| **1 · Data store** | `DataStoreAPI.upsert`, `store.set/get`, owner auto-naming (`sdk_showcase:state`), idempotency — a counter |
| **2 · Secrets** | `SecretAPI.upsert/grant` — encrypted, owner-scoped, injected into the script as the clean env var `$DEMO_TOKEN` (selected-mode) |
| **3 · Run lifecycle** | `ScriptAPI.queue_run` / `latest_run` / `runs` / `cancel_latest_run`, the `RunView` read-model, live progress + **Stop** |
| **4 · Worker output** | the script side — `pyrunner_datastore`, `$PYRUNNER_OWNER_PLUGIN`, the progress heartbeat, the run-history record |
| **5 · Schedule** | `ScheduleAPI.sync/list` (manual / interval / daily) |
| **6 · Ownership** | the owned-resource inventory (`.list()` on each API); clean teardown on uninstall |

The **demo worker uses only the standard library** — no `requests`, no `boto3` —
so it runs in *any* environment with zero package setup. That makes "see it work"
as low-friction as possible.

## Install

1. **Environment** — under *Environments*, create/choose any one (no extra
   packages needed).
2. **Upload** `sdk_showcase.zip` (Plugins → Upload), **Activate**, then **Restart**.
3. Open **SDK Showcase** in the sidebar (superuser only), fill the short setup
   form, **Set up demo**, then explore the cards — increment the counter, run the
   demo, watch progress, stop it, change the schedule.

## Develop locally (dev mode)

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/sdk_showcase
python manage.py runserver
```

Validate anytime without uploading:

```bash
python manage.py plugin_doctor --path examples/sdk_showcase
```

## Tests

```bash
python manage.py test core.test_sdk_showcase_plugin
```

The tests live in [`tests.py`](tests.py) and run via a thin shim in
`core/test_sdk_showcase_plugin.py`. They cover provisioning idempotency, the
counter read/write, the worker secret/config contract, and form validation.

## Package the zip

```bash
cd examples
zip -r sdk_showcase.zip sdk_showcase -x '*/__pycache__/*'
```

The archive must contain a single top-level folder (`sdk_showcase/`) — the slug,
matching `plugin.json` and `apps.py`.

## Files

| File | Where it runs | Purpose |
|---|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | web process | the page (setup form + capability cards) |
| `provisioning.py` | web process | all SDK calls (idempotent provision + reads/actions) |
| `worker_body.py` | environment venv | the managed demo script (stdlib only) |
| `tests.py` | test runner | unit tests for the provisioning + contract surface |

See `docs/plugins.md` for the full author guide, and `CLAUDE.md` here for the
plugin's internal contracts.
