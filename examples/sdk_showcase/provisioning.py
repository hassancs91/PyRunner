"""
Provisioning for the SDK Showcase plugin — every persistence/orchestration call
goes through the SDK (``core.plugins.api``), so this module doubles as the worked
example of *how to use it*. Nothing imports core models/tasks/services directly.

One "Set up demo" save provisions, owned by the ``sdk_showcase`` slug:
  * a DataStore  ``sdk_showcase:state``  (config + counter + runs + progress),
  * 1 owner-scoped Secret  (DEMO_TOKEN),
  * a managed Script  (key ``demo`` = the bundled stdlib worker),
  * a Schedule (manual by default).

Re-saving updates the same rows (idempotent on ``(owner_plugin, owner_key)``).
"""

from pathlib import Path

from core.plugins.api import (
    DataStoreAPI,
    EnvironmentAPI,
    ScheduleAPI,
    ScriptAPI,
    SecretAPI,
)

from .forms import SECRET_FIELDS

OWNER = "sdk_showcase"
SCRIPT_KEY = "demo"
SCRIPT_NAME = "SDK Showcase Demo"
STORE_KEY = "state"
DEFAULT_TIMEOUT = 120

# Keys of the non-secret config persisted to the DataStore (the worker reads them).
CONFIG_KEYS = ("message", "steps")


def _worker_code():
    """The managed Script's code = bundled worker_body.py, with a drift guard.

    The worker is a standalone process (it can't import this module), so the
    injected secret name and config keys are shared by convention. If a rename
    drifts them out of sync we fail loudly here at Save, not at run time.
    """
    code = Path(__file__).with_name("worker_body.py").read_text(encoding="utf-8")
    expected = list(SECRET_FIELDS.values()) + list(CONFIG_KEYS)
    missing = [token for token in expected if token not in code]
    if missing:
        raise ValueError(
            "worker_body.py is out of sync with provisioning (missing references: "
            + ", ".join(missing) + ")."
        )
    return code


# --------------------------------------------------------------------------- #
# Reads (used by the views to render the page)
# --------------------------------------------------------------------------- #

def _store():
    return DataStoreAPI(OWNER).get(STORE_KEY)


def get_config():
    store = _store()
    return (store.get("config", {}) or {}) if store is not None else {}


def get_counter():
    store = _store()
    return int(store.get("counter", 0) or 0) if store is not None else 0


def store_keys():
    """The entry keys present in the owned data store (it's just JSON KV)."""
    store = _store()
    return sorted(store.all().keys()) if store is not None else []


def get_runs():
    store = _store()
    if store is None:
        return []
    runs = store.get("runs", [])
    return runs if isinstance(runs, list) else []


def get_progress():
    store = _store()
    return store.get("progress") if store is not None else None


def get_script():
    return ScriptAPI(OWNER).get(SCRIPT_KEY)


def get_schedule():
    scheds = ScheduleAPI(OWNER).list()
    return scheds[0] if scheds else None


def list_environments():
    return EnvironmentAPI().list()


def get_secret():
    return SecretAPI(OWNER).get(SECRET_FIELDS["demo_token"])


def has_secret():
    return get_secret() is not None


def secret_redacted():
    """A redacted view of the stored secret (never its plaintext)."""
    secret = get_secret()
    if secret is None:
        return None
    value = secret.get_decrypted_value() or ""
    if len(value) >= 2:
        return value[:1] + "***" + value[-1:]
    return "***" if value else "(empty)"


def is_setup():
    return get_script() is not None


def owned_inventory():
    """Counts of every resource this plugin owns — the ownership grouping that
    makes a plugin's footprint visible and delete-guarded."""
    return {
        "datastores": len(DataStoreAPI(OWNER).list()),
        "secrets": len(SecretAPI(OWNER).list()),
        "scripts": len(ScriptAPI(OWNER).list()),
        "schedules": len(ScheduleAPI(OWNER).list()),
    }


def initial_from_state():
    """Prefill the setup form from saved config + the managed script's environment."""
    cfg = get_config()
    initial = {k: cfg[k] for k in CONFIG_KEYS if cfg.get(k) is not None}
    script = get_script()
    if script is not None and script.environment_id:
        initial["environment"] = script.environment.name
    return initial


def schedule_summary():
    """A small {mode, label, next_run} for the schedule card (or None)."""
    sched = get_schedule()
    if sched is None:
        return None
    mode = sched.run_mode
    if mode == "interval":
        label = f"Every {sched.interval_minutes} min"
    elif mode == "daily":
        label = f"Daily {(sched.daily_times or ['?'])[0]}"
    elif mode == "weekly":
        label = "Weekly"
    else:
        label = "Manual"
    return {"mode": mode, "label": label, "next_run": sched.next_run}


def live_status():
    """A JSON-serializable snapshot for the page's status poller.

    Run state is AUTHORITATIVE via the SDK (``latest_run``); the per-step progress
    comes from the worker's heartbeat, shown only when it belongs to this run.
    """
    run = ScriptAPI(OWNER).latest_run(SCRIPT_KEY)
    active = bool(run and run.status in ("pending", "running"))
    progress = None
    if active:
        p = get_progress()
        if p and p.get("state") != "done" and (p.get("run_id") or "") == run.id.replace("-", ""):
            progress = p
    return {"active": active, "run": run.as_dict() if run else None, "progress": progress}


def recent_runs(limit=10):
    """Authoritative run history from the SDK run-lifecycle surface (RunViews)."""
    return [r.as_dict() for r in ScriptAPI(OWNER).runs(SCRIPT_KEY, limit=limit)]


# --------------------------------------------------------------------------- #
# Writes / actions (each is a one-capability demo)
# --------------------------------------------------------------------------- #

def provision(data, *, created_by=None):
    """Idempotently provision/update everything from cleaned setup ``data``.

    Returns ``(script, warnings)``. This is the full provisioning chain in one
    place: DataStore upsert + config, owner-scoped Secret upsert + grant,
    Environment select, Script upsert, and a default Schedule.
    """
    warnings = []

    # 1) Owned DataStore + non-secret config entry.
    store = DataStoreAPI(OWNER).upsert(
        STORE_KEY,
        description="SDK Showcase demo state (config + counter + runs)",
        created_by=created_by,
    )
    store.set("config", {
        "message": data.get("message") or "hello from the data store",
        "steps": int(data.get("steps") or 5),
    })
    if store.get("counter") is None:
        store.set("counter", 0)

    # 2) Owner-scoped secret — only (re)write when a value was supplied
    #    (a blank field keeps the existing value).
    secrets = SecretAPI(OWNER)
    token = (data.get("demo_token") or "").strip()
    if token:
        secrets.upsert(SECRET_FIELDS["demo_token"], token, description="SDK Showcase demo secret")

    # 3) Environment (any will do — the worker is stdlib-only).
    env = EnvironmentAPI().get(data["environment"])
    if env is None:
        raise ValueError("Select an environment to run the demo script in.")

    # 4) Managed Script (selected-mode injection; isolation stays 'inherit').
    script = ScriptAPI(OWNER).upsert(
        key=SCRIPT_KEY,
        name=SCRIPT_NAME,
        code=_worker_code(),
        environment=env,
        timeout_seconds=DEFAULT_TIMEOUT,
        injection_mode="selected",
        description="Managed by the SDK Showcase plugin — edit settings on its page.",
        is_enabled=True,
        created_by=created_by,
    )

    # 5) Grant the demo secret to the script (so it injects as $DEMO_TOKEN).
    secret = secrets.get(SECRET_FIELDS["demo_token"])
    if secret is not None:
        secrets.grant(script, secret)

    # 6) Default schedule (manual) on first setup; leave an existing one alone.
    if get_schedule() is None:
        ScheduleAPI(OWNER).sync(script, mode="manual")

    return script, warnings


def increment_counter():
    """Demonstrate a data-store read+write: bump and return a counter."""
    store = DataStoreAPI(OWNER).upsert(STORE_KEY)
    value = int(store.get("counter", 0) or 0) + 1
    store.set("counter", value)
    return value


def reset_demo_data():
    """Clear the demo counter, run history, and progress (a data-level reset).

    Note: this clears *data*, not the provisioned rows — those are removed when
    the plugin is uninstalled (the ownership model makes that a clean sweep).
    """
    store = _store()
    if store is None:
        return
    store.set("counter", 0)
    store.set("runs", [])
    store.set("progress", None)


def queue_demo_run(triggered_by=None):
    """Queue a tracked Run of the managed demo script (via the RunBackend seam)."""
    script = get_script()
    if script is None:
        return None, "Set up the demo first."
    if not script.can_run:
        return None, "The demo script is disabled or archived."
    run = ScriptAPI(OWNER).queue_run(SCRIPT_KEY, triggered_by=triggered_by)
    return run, None


def cancel_running():
    """Stop the latest pending/running demo run (SDK → shared force-stop)."""
    return ScriptAPI(OWNER).cancel_latest_run(SCRIPT_KEY)


def sync_schedule(mode):
    """Sync the demo script's schedule to one of a few simple modes."""
    script = get_script()
    if script is None:
        return False
    api = ScheduleAPI(OWNER)
    if mode == "interval":
        api.sync(script, mode="interval", interval_minutes=60)
    elif mode == "daily":
        api.sync(script, mode="daily", time_str="09:00")
    else:
        api.sync(script, mode="manual")
    return True
