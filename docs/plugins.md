# Writing a PyRunner plugin

A plugin is a self-contained Django app that adds UI and behavior to PyRunner
**without editing any core file**. Admins upload it, activate it, and it serves at
`/plugins/<slug>/` and appears in the console sidebar.

The cardinal rule of the system: **a broken plugin can never break the main
site.** "Installed" (files on disk) is not "active" (loaded), nothing risky loads
into the live process without passing an isolated preflight first, and a
pre-activation **doctor** (see below) refuses rule-breakers before they can touch
the live process.

> **Plugin Platform v2** adds four things on top of the v1 system: **Dev Mode**
> (live local iteration), the **SDK** (`core.plugins.api` — orchestrate PyRunner
> primitives without importing core internals), **resource ownership + scoped
> secrets** (your scripts/secrets/datastores are grouped, idempotent, and
> delete-guarded), and the **doctor** (a static-lint activation gate). Everything
> is additive — a v1 plugin keeps working.

---

## Folder layout

```
<slug>/                      # e.g. my_flows — a self-contained Django app
  __init__.py
  apps.py                    # subclass core.plugins.PluginAppConfig (import-light!)
  plugin.json                # manifest: slug + marketplace metadata (see below)
  urls.py                    # app_name = "<slug>"; auto-mounted at /plugins/<slug>/
  views.py
  provisioning.py            # optional; your SDK calls (create owned scripts/secrets/…)
  worker_body.py             # optional; the body of a managed Script you provision
  assets/icon.svg            # optional; the manifest "icon" path (any path under <slug>/)
  templates/<slug>/...       # extend "base.html" so pages match the console
  static/<slug>/...          # optional
```

**Plugins ship NO `models.py` and NO `migrations/`.** Your database is owned
**DataStores** (see *Persistence* below) — so no plugin DDL ever reaches a core
table, and the entire "a plugin migration broke the DB" risk class is gone. The
doctor **rejects** any plugin that ships models/migrations.

The `slug` must match `^[a-z][a-z0-9_]*$` and be identical in three places: the
folder name, `plugin.json`'s `slug`, and `PyRunnerPlugin(slug=...)` in `apps.py`.
It must not be a reserved name (`core`, `theme`, `landing`, `plugins`, `admin`,
`static`, `api`).

## `plugin.json`

The manifest is the single carrier for a plugin's packaged metadata. It is stored
verbatim on install and surfaced on the plugin's detail page — and it is the
contract a future plugin **marketplace** reads. Keep it to **static, packaged,
author-owned** facts: anything that can change without shipping a new version
(ratings, install counts, price) is the marketplace server's job, not the
manifest's.

```json
{
    "manifest_version": 1,
    "slug": "my_flows",
    "publisher": "your_handle",
    "name": "My Flows",
    "version": "1.2.0",

    "summary": "Short one-liner shown on cards.",
    "description": "What this plugin does, in a sentence or two.",
    "icon": "assets/icon.svg",
    "icon_fallback": "🧩",
    "categories": ["automation"],
    "keywords": ["flows", "etl"],

    "author": "Your Name",
    "author_url": "https://example.com",
    "license": "MIT",
    "homepage": "https://example.com/my_flows",
    "repository": "https://github.com/you/my_flows",
    "documentation": "https://example.com/my_flows/docs",

    "api": "2.0",
    "min_pyrunner": "1.11.0",
    "max_pyrunner": "",

    "provisions": {
        "scripts": 1,
        "secrets": 3,
        "datastores": 1,
        "schedules": 1,
        "secret_keys": ["MY_API_KEY"]
    }
}
```

Only `slug` is strictly required; **every other field is optional** and a legacy
manifest with just `slug`/`name`/`version` still installs and activates. Field
reference:

| Field | Notes |
|---|---|
| `manifest_version` | Format version of the manifest itself. Currently `1` (omit ⇒ treated as 1). |
| `slug` | Must match the folder name + `apps.py` `app_name`. Instance-unique. |
| `publisher` | Marketplace namespace → global id is `publisher/slug`. Lowercase letters/digits/`_`/`-`. Local install stays slug-only. |
| `name`, `version` | `version` should be **semver** (`MAJOR.MINOR.PATCH`) — update-detection depends on it. Default to the slug / `0.0.0`. |
| `summary` | Short tagline for list cards (falls back to `description`). |
| `description` | Longer prose for the detail page. |
| `icon` | **Bundled file**, relative path that must stay under `<slug>/`. `.png` / `.svg` / `.webp` / `.jpg` / `.jpeg`. Served from disk, so it shows for installed-but-not-active plugins and works offline. SVG is served only as an `<img>` source, never inlined. |
| `icon_fallback` | Emoji shown when there's no bundled icon (or it fails to load). |
| `categories`, `keywords` | Lists of strings for discovery/filtering. |
| `author`, `author_url`, `license`, `homepage`, `repository`, `documentation` | Authorship + support. `license` is an SPDX id (e.g. `MIT`). |
| `api` | The `core.plugins.api` version you target (see `API_VERSION`). |
| `min_pyrunner`, `max_pyrunner` | Compatibility bounds. |
| `provisions` | **Declares what the plugin creates** (resource counts + the secret keys it needs). Rendered before install/activation as a trust surface ("creates 1 script, 3 secrets, 1 schedule"). Counts are non-negative integers; `secret_keys` is a list of strings. |

The doctor `warn`s (advisory, never blocks) when the marketplace-recommended
fields — `author`, `license`, `summary`, `icon` — are missing, and `fail`s on
*malformed* values (bad semver, an `icon` that escapes the folder or has an
unsupported extension, an unknown `manifest_version`, or a wrong-shaped
`provisions`). Those recommended fields become **required at marketplace
submission**, not at local install.

## `apps.py` (keep it import-light)

```python
from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class MyFlowsConfig(PluginAppConfig):
    name = "plugins.my_flows"     # always "plugins.<slug>"
    label = "my_flows"            # the app label (use the slug)
    plugin = PyRunnerPlugin(
        slug="my_flows",
        name="My Flows",
        version="1.0.0",
        nav_items=[
            NavItem(label="My Flows", url_name="my_flows:index"),
            # icon_svg="<path ... />"     # optional inline SVG <path>; omit for the default
            # superuser_only=True         # hide this item from non-superusers
        ],
    )
```

`apps.py` is imported **before the app registry is ready** (the boot loader's
light-import pre-check), so it must **not** import `core.models` / `core.tasks` /
`core.services` or any heavy third-party package at module top. Import those
**lazily inside functions**, or use the SDK (`core.plugins.api`), which is
import-light by design. The doctor enforces this (and the preflight asserts it
dynamically).

## `urls.py`

```python
from django.urls import path
from . import views

app_name = "my_flows"          # must match the slug

urlpatterns = [
    path("", views.index, name="index"),
]
```

Auto-mounted at `/plugins/my_flows/`. Reference routes as `{% url 'my_flows:index' %}`.

## Templates

Extend the console base and namespace under your slug so nothing shadows a core
template (the doctor checks this):

```django
{% extends "base.html" %}
{% block title %}My Flows - PyRunner{% endblock %}
{% block content %}
<div class="flex">
    {% include "cpanel/_sidebar.html" %}
    <div class="flex-1 min-w-0">
        <div class="px-5 lg:px-8 py-7 max-w-[1440px] mx-auto space-y-5">
            <!-- your page -->
        </div>
    </div>
</div>
{% endblock %}
```

---

## Dev Mode — iterate locally with live reload

Develop a plugin from a local folder under `manage.py runserver`, with Django's
StatReloader reloading your `.py`/templates instantly — no zip, no upload, no
preflight, no restart.

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/my_flows   # the folder IS the slug
python manage.py runserver
```

The dev plugin loads as `plugins.<slug>` (byte-identical to the shipped form), so
your `apps.py` (`name="plugins.<slug>"`) and `urls.py` need no changes between dev
and ship. It's triple-guarded — `DEBUG` **and** `PYRUNNER_PLUGIN_DEV` **and**
`RUN_MAIN` (the reloader child) — so the production WSGI/gunicorn path never loads
it. A dev plugin gets no `plugins`-table row and is invisible to the restart
detector. Validate a folder anytime with `manage.py plugin_doctor --path ./my_flows`.

---

## The plugin SDK — `core.plugins.api`

Orchestrate PyRunner primitives (scripts, secrets, datastores, schedules,
environments, runs) **through the SDK** instead of importing `core.models` /
`core.tasks` / `core.services` directly. The SDK auto-stamps **ownership** (your
plugin slug) **and the workspace**, is **idempotent**, auto-names datastores, and
never bypasses the run/sandbox seams.

```python
from core.plugins.api import (
    EnvironmentAPI, ScriptAPI, SecretAPI, DataStoreAPI, ScheduleAPI, API_VERSION,
)

OWNER = "my_flows"   # your slug — passes ownership through every call

# Environments are SELECTED, never created by plugins:
env = EnvironmentAPI().get("data-science")          # read-only; .list() too

# Secrets — owner-scoped, injected under their CLEAN name:
SecretAPI(OWNER).upsert("R2_BUCKET", "my-bucket")   # idempotent by (owner, key)

# DataStores — your database (auto-named "<owner>:<key>"):
store = DataStoreAPI(OWNER).upsert("state")
store.set("config", {"retries": 3})
cfg = DataStoreAPI(OWNER).get("state").get("config")

# Scripts — idempotent on (owner, owner_key); plugin scripts default to
# injection_mode='selected' and isolation_mode='inherit' (the sandbox policy decides):
script = ScriptAPI(OWNER).upsert(
    key="backup", name="My Backup", code=generated_code, environment=env,
    timeout_seconds=3600, notify_on="failure",
)
SecretAPI(OWNER).grant(script, SecretAPI(OWNER).get("R2_BUCKET"))  # selected-mode injection

# Pick a venv once → every owned script follows:
ScriptAPI(OWNER).set_environment(env)

# Schedule + run, through the real RunBackend + scheduler:
ScheduleAPI(OWNER).sync(script, mode="daily", time_str="02:00", tz="UTC")
ScriptAPI(OWNER).queue_run("backup")

# Observe + control that run — no core.models import:
view = ScriptAPI(OWNER).latest_run("backup")     # most recent run, or None
history = ScriptAPI(OWNER).runs("backup", limit=10)  # newest-first RunViews
ScriptAPI(OWNER).cancel_latest_run("backup")     # Stop button → True if cancelled
```

Key behaviors:
- **Idempotent upsert.** `upsert(key=...)` keys on `(owner_plugin, owner_key)`, so
  re-saving config updates the same Script/Secret/DataStore — no duplicates on
  re-provision. (You no longer hand-store a `script_id`.)
- **Auto-naming.** A DataStore's stored `name` is `"<owner>:<key>"` (globally/
  per-workspace unique) while you refer to it by the short `key`.
- **Clean secret names.** An owner-scoped secret `R2_BUCKET` injects as
  `R2_BUCKET` into that owner's scripts — two plugins can both define `R2_BUCKET`.
- **Workspace.** Calls default to the default workspace; pass
  `ScriptAPI(OWNER, workspace=ws)` to target another.
- **Legacy lane.** `owner=None` (e.g. `SecretAPI().upsert(...)`) writes an
  unowned, global/user-namespace row — handy for porting old code gradually.
- **No seam bypass.** `queue_run` goes through `queue_script_run` (RunBackend +
  `resolve_isolation`); the SDK never touches raw SQLite or the scheduler directly.

### Observe & control runs (API 2.1)

Once you've provisioned and queued a Script, watch and stop its runs through the
SDK — never by importing `core.models.Run` (which couples you to the schema and
trips the doctor's `sdk-usage` warn). Three owner+workspace-scoped methods on
`ScriptAPI`:

| Method | Returns | Use |
|---|---|---|
| `latest_run(key)` | `RunView \| None` | the most recent run of your script `key` |
| `runs(key, *, limit=20)` | `list[RunView]` | recent history, newest first |
| `cancel_latest_run(key)` | `bool` | cancel the latest pending/running run; `True` if one was cancelled |

`cancel_latest_run` reuses the **same** force-stop path as the tasks Stop button
(`TaskService.force_stop_run`): a *running* run has its process tree killed, a
*pending* run is dequeued, both flipped to `cancelled`. It returns `False` when
nothing is cancellable.

A **`RunView`** is an immutable, ORM-free snapshot — no live `Run` leaks past the
SDK. Fields: `id, status, trigger_type, created_at, started_at, ended_at,
duration, exit_code, pid, task_id, is_finished, is_running`. Its `.as_dict()`
returns JSON-serializable values (datetimes → ISO 8601), so a status endpoint can
return it directly:

```python
from django.http import JsonResponse
from core.plugins.api import ScriptAPI

OWNER = "my_flows"

def backup_status(request):
    """Live status for the plugin's dashboard — poll this from JS."""
    view = ScriptAPI(OWNER).latest_run("backup")
    return JsonResponse({
        "running": bool(view and view.is_running),   # drives a "backup running…" badge
        "latest": view.as_dict() if view else None,
        "history": [v.as_dict() for v in ScriptAPI(OWNER).runs("backup", limit=5)],
    })

def backup_stop(request):
    """A Stop button in the plugin UI."""
    stopped = ScriptAPI(OWNER).cancel_latest_run("backup")
    return JsonResponse({"stopped": stopped})
```

`worker_body.py` (the runtime script you provision) reads its credentials from the
injected, masked env vars (clean names), opens datastores with the normal
`from pyrunner_datastore import DataStore`, and can read `PYRUNNER_OWNER_PLUGIN`.

---

## Ownership & scoped secrets

Every resource the SDK creates carries your `owner_plugin` slug (a string, not an
FK — it survives plugin deletion) plus a stable `owner_key` handle. Owned
resources are:

- **Grouped & pill-marked** — they show an *owner pill* in the Scripts/Secrets/
  DataStores lists.
- **Delete-guarded** — a user can't delete them from the generic pages (the
  message routes them to your plugin); a superuser can force-delete with explicit
  confirmation, which cleanly drops dangling grants.
- **Cleaned up on uninstall** — `Delete plugin → remove data` deletes exactly the
  rows you own; user rows are never touched.

**Scoped secret injection (opt-in).** Every script has an `injection_mode`:
- `'all'` (the default for user scripts, and the literal pre-v2 behavior) — inject
  every user secret in the workspace.
- `'selected'` (the SDK default for plugin scripts) — inject only **granted**
  secrets + **same-owner** secrets + **explicitly-global** (unowned) secrets, by
  clean name. Use `SecretAPI(OWNER).grant(script, secret)` to attach one.

This is purely additive: existing scripts stay `'all'`, byte-for-byte.

---

## Persistence — DataStores, not models

Plugins persist via **owned DataStores** (a named store × keyed JSON entries),
addressed through `DataStoreAPI`. This is deliberate: no plugin model means no
plugin migration, which means no plugin DDL can ever reach a core table. A real
"relational mini-app" is a sign the work belongs in core or a separate service —
it is out of scope for a plugin, and the doctor will reject `models.py`/
`migrations/`.

The runtime `from pyrunner_datastore import DataStore` API is engine-portable
(SQLite direct, or a loopback API on Postgres) and unchanged.

---

## Running real work: `run_in_environment`

Keep the web layer thin. Anything that needs third-party packages must run in a
**PyRunner environment's venv** as an isolated subprocess — never imported into
the Django process:

```python
from core.plugins.api import EnvironmentAPI
from core.plugins import run_in_environment

env = EnvironmentAPI().get("data-science")
exit_code, stdout, stderr = run_in_environment(env, code="import pandas; print(pandas.__version__)", timeout=30)
# or a bundled file: run_in_environment(env, path="/app/plugins/my_flows/worker.py", args=["--n", "5"])
```

This reuses the hardened executor path (the env's Python, process-group isolation,
a timeout, captured + size-capped output). A bad package fails the *call*, not the
server. For long jobs, provision a Script and `queue_run` it instead.

---

## The plugin "doctor"

A pre-activation rules check (`manage.py plugin_doctor <slug | --path ./folder>`,
and run automatically at activation). Tier-1 is a **static lint** — file checks +
`ast.parse`, no execution — so it's safe on untrusted files. Severity is
`fail` (blocks activation) or `warn` (advisory):

| Check | Severity |
|---|---|
| Valid, non-reserved slug; manifest present + slug matches folder | fail |
| `__init__.py` + `apps.py` present | fail |
| **No `models.py` / `migrations/`** | fail |
| `apps.py` defines one `PluginAppConfig` with `name=="plugins.<slug>"`, `label==slug` | fail |
| `apps.py` imports `core.models` at module top | fail |
| `urls.py` `app_name == slug` | fail |
| Templates/static namespaced under `<slug>/` (no shadowing) | fail |
| Manifest metadata is malformed (bad semver, `icon` escapes folder / bad ext, unknown `manifest_version`, wrong `provisions` shape) | fail |
| `apps.py` has heavy/third-party top-level imports | warn |
| Imports core internals directly instead of `core.plugins.api` | warn |
| Missing recommended marketplace fields (`author` / `license` / `summary` / `icon`) | warn |

The doctor runs **before** the preflight subprocess at activation, so a
rule-breaker is refused before any plugin code or migration could run. It never
runs on the boot path, so an already-active plugin stays active across an upgrade
even if new rules are added.

---

## Lifecycle (what the admin does)

1. **Upload** the `.zip` (Plugins → Upload). Validated (zip-slip/size safe) and
   unpacked; code is *not* imported. Status becomes `Installed`.
2. **Activate** runs the **doctor** (static lint) and then `plugin_preflight` in a
   throwaway subprocess (import + resolve URLs + assert apps.py didn't import
   `core.models`). On success → `Active`; on failure the per-rule report is shown
   and the live site is untouched.
3. **Restart** (a button appears) applies the change — gunicorn + the worker
   re-import the new active set, preflighting each in isolation first.
4. **Deactivate** keeps your data; **Delete** removes files + row, optionally
   deleting the resources your plugin owns.

If a plugin ever fails at boot, it's quarantined as `Errored` and skipped — the
site still boots. Last resort: `PYRUNNER_DISABLE_PLUGINS=1` boots with zero plugins.

---

## Do / don't

**Do**
- Iterate with **Dev Mode**; validate with `plugin_doctor` before you ship.
- Orchestrate via **`core.plugins.api`**; persist via **owned DataStores**.
- Keep `apps.py` import-light; push compute into `run_in_environment` or a `Run`.
- Namespace templates/static/URLs under your slug.

**Don't**
- Ship `models.py` / `migrations/` (the doctor rejects them — use DataStores).
- Import `core.models`/third-party packages at module top in `apps.py`.
- Edit core files or rely on core internals beyond `core.plugins` / `core.plugins.api`.
- Do slow or failure-prone work in `ready()`.

A complete sample lives in [`examples/example_plugin/`](../examples/example_plugin/);
the `qdrant-backup-plugin/` is a full SDK-based reference (config in an owned
DataStore, owner-scoped secrets, an idempotent managed script + schedule).
