"""
Provisioning for the Brand Tracker plugin — everything goes through the SDK
(``core.plugins.api``), so the plugin owns its resources, re-saves are idempotent,
and nothing imports core models/tasks/services directly.

One config form save provisions, in the default workspace, all owned by the
``brand_tracker`` slug:
  * a DataStore  ``brand_tracker:state``  (entry ``config`` = non-secret config;
    ``seen`` = dedup index, ``mentions`` = the feed, ``stats``/``credits``/``runs``
    = dashboard data the worker writes),
  * owner-scoped Secrets  (SERPER_API_KEY always; REDDIT_CLIENT_ID /
    REDDIT_CLIENT_SECRET / RESEND_API_KEY only when supplied),
  * a managed Script  (key ``track``, selected-mode injection + grants),
  * a weekly Schedule.

Re-saving updates the same rows (idempotent on ``(owner_plugin, owner_key)``).
"""

from pathlib import Path

from core.plugins.api import (
    DataStoreAPI,
    EnvironmentAPI,
    PublicPageAPI,
    ScheduleAPI,
    ScriptAPI,
    SecretAPI,
)

from .forms import SECRET_FIELDS

OWNER = "brand_tracker"
SCRIPT_KEY = "track"
SCRIPT_NAME = "Brand Tracker"
STORE_KEY = "state"
REPORT_PAGE = "report"  # declared in plugin.json "provides.public_pages"
DEFAULT_TIMEOUT = 1800  # a weekly multi-keyword, multi-source run

# Keys of the non-secret config persisted to the DataStore (also prefill the form
# and form the cross-process contract checked against worker_body.py).
CONFIG_KEYS = (
    "keywords",
    "excluded_domains",
    "news_enabled",
    "hackernews_enabled",
    "reddit_enabled",
    "num_results",
    "retention_days",
    "monthly_credit_cap",
    "email_enabled",
    "email_to",
    "email_from",
    "enrich_provider",
    "enrich_model",
)


def _worker_code():
    """The managed Script's code = our bundled worker_body.py (read at provision).

    Guards the cross-layer contract: ``worker_body.py`` is a standalone script
    (separate process — it can't import this module), so the secret env-var names
    and config keys are shared by convention. If a rename drifts them out of sync
    we fail loudly here at Save, not silently at run time.
    """
    code = Path(__file__).with_name("worker_body.py").read_text(encoding="utf-8")
    expected = list(SECRET_FIELDS.values()) + list(CONFIG_KEYS)
    missing = [token for token in expected if token not in code]
    if missing:
        raise ValueError(
            "worker_body.py is out of sync with provisioning (missing references: "
            + ", ".join(missing)
            + "). Aborting to avoid a silently misconfigured tracker."
        )
    return code


def _as_list(value):
    """Normalize a textarea/comma string OR a list into a clean list of strings."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
        for line in (value or "").replace(",", "\n").splitlines():
            items.append(line)
    return [s.strip() for s in items if s and s.strip()]


# --------------------------------------------------------------------------- #
# Reads (used by the views to render the page)
# --------------------------------------------------------------------------- #

def get_config():
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    return (store.get("config", {}) or {}) if store is not None else {}


def configured_secret_keys():
    """Set of clean env-keys that already exist as owner secrets."""
    api = SecretAPI(OWNER)
    return {k for k in SECRET_FIELDS.values() if api.get(k) is not None}


def get_script():
    return ScriptAPI(OWNER).get(SCRIPT_KEY)


def get_schedule():
    scheds = ScheduleAPI(OWNER).list()
    return scheds[0] if scheds else None


def _store_entry(key, default):
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    if store is None:
        return default
    val = store.get(key, default)
    return val if val is not None else default


def get_mentions():
    """The deduped mention feed the worker maintains (newest last)."""
    val = _store_entry("mentions", [])
    return val if isinstance(val, list) else []


def get_stats():
    val = _store_entry("stats", {})
    return val if isinstance(val, dict) else {}


def get_credits():
    val = _store_entry("credits", {})
    return val if isinstance(val, dict) else {}


def get_runs():
    val = _store_entry("runs", [])
    return val if isinstance(val, list) else []


def get_progress():
    return _store_entry("progress", None)


def list_environments():
    return EnvironmentAPI().list()


# --------------------------------------------------------------------------- #
# Public report share (capability URL at /p/<token>/)
# --------------------------------------------------------------------------- #

def share_report(created_by=None):
    """Share (or return the existing) public report link; returns the URL path."""
    return PublicPageAPI(OWNER).share(REPORT_PAGE, created_by=created_by)


def revoke_report():
    """Kill the public report link permanently. True if one existed."""
    return PublicPageAPI(OWNER).revoke(REPORT_PAGE)


def report_share():
    """Current share state dict (or None if the report was never shared)."""
    return PublicPageAPI(OWNER).get(REPORT_PAGE)


def initial_from_config():
    """Build the form ``initial`` dict from saved config + schedule + script."""
    cfg = get_config()
    initial = {}
    if "keywords" in cfg:
        initial["keywords"] = "\n".join(cfg.get("keywords") or [])
    if "excluded_domains" in cfg:
        initial["excluded_domains"] = "\n".join(cfg.get("excluded_domains") or [])
    for k in ("news_enabled", "hackernews_enabled", "reddit_enabled", "email_enabled"):
        if k in cfg:
            initial[k] = bool(cfg.get(k))
    for k in ("num_results", "retention_days", "monthly_credit_cap", "email_to",
              "email_from", "enrich_provider", "enrich_model"):
        if cfg.get(k) is not None:
            initial[k] = cfg.get(k)

    script = get_script()
    if script is not None:
        initial["notify_on"] = script.notify_on
        initial["notify_email"] = script.notify_email
        if script.environment_id:
            initial["environment"] = script.environment.name

    sched = get_schedule()
    if sched is not None:
        if sched.run_mode == "weekly":
            if sched.weekly_times:
                initial["schedule_time"] = sched.weekly_times[0]
            if sched.weekly_days:
                initial["schedule_weekday"] = str(sched.weekly_days[0])
        initial["timezone"] = sched.timezone
    return initial


# --------------------------------------------------------------------------- #
# Write (the one provisioning entry point)
# --------------------------------------------------------------------------- #

def provision(data, *, created_by=None):
    """Idempotently provision/update everything from cleaned form ``data``.

    Returns ``(script, warnings)``. ``warnings`` is a list of advisory strings.
    """
    warnings = []

    # 1) Owned DataStore + non-secret config entry.
    store = DataStoreAPI(OWNER).upsert(
        STORE_KEY, description="Brand Tracker plugin config + mention feed", created_by=created_by
    )
    store.set("config", {
        "keywords": _as_list(data.get("keywords")),
        "excluded_domains": _as_list(data.get("excluded_domains")),
        "news_enabled": bool(data.get("news_enabled", True)),
        "hackernews_enabled": bool(data.get("hackernews_enabled")),
        "reddit_enabled": bool(data.get("reddit_enabled")),
        "num_results": int(data.get("num_results") or 10),
        "retention_days": int(data.get("retention_days") or 90),
        "monthly_credit_cap": int(data.get("monthly_credit_cap") or 0),
        "email_enabled": bool(data.get("email_enabled")),
        "email_to": (data.get("email_to") or "").strip(),
        "email_from": (data.get("email_from") or "").strip(),
        "enrich_provider": (data.get("enrich_provider") or "off"),
        "enrich_model": (data.get("enrich_model") or "").strip(),
    })

    # 2) Owner-scoped secrets — only (re)write the ones actually supplied;
    #    a blank field keeps the existing value.
    secrets_api = SecretAPI(OWNER)
    for field_name, env_key in SECRET_FIELDS.items():
        value = (data.get(field_name) or "").strip()
        if value:
            secrets_api.upsert(env_key, value, description=f"Brand Tracker — {env_key}")

    # 3) Environment (must exist; should carry requests).
    env = EnvironmentAPI().get(data["environment"])
    if env is None:
        raise ValueError("Select an environment (one that has 'requests' installed).")
    reqs = (env.requirements or "").lower()
    if "requests" not in reqs:
        warnings.append(
            f"Environment '{env.name}' may be missing 'requests'. "
            "Add it under Environments or runs will fail."
        )

    # Reddit needs OAuth app credentials to work server-side.
    if bool(data.get("reddit_enabled")):
        has_creds = (
            (data.get("reddit_client_id") or "").strip()
            and (data.get("reddit_client_secret") or "").strip()
        ) or {"REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"} <= configured_secret_keys()
        if not has_creds:
            warnings.append(
                "Reddit is enabled but its Client ID / Secret are not set — "
                "Reddit will be skipped until you add them."
            )

    # AI enrichment readiness (advisory — enrichment degrades safely at runtime).
    provider = data.get("enrich_provider") or "off"
    if provider == "claude" and "claude-agent-sdk" not in reqs:
        warnings.append(
            f"AI enrichment uses Claude, but environment '{env.name}' may be missing "
            "'claude-agent-sdk'. Add it under Environments (and enable AI under "
            "Services → AI Provider) or mentions will be stored without sentiment."
        )
    if provider == "openrouter":
        has_or = (data.get("openrouter_api_key") or "").strip() or "OPENROUTER_API_KEY" in configured_secret_keys()
        if not has_or:
            warnings.append(
                "AI enrichment uses OpenRouter but no OpenRouter API key is set — "
                "mentions will be stored without sentiment until you add it."
            )

    # 4) Managed Script (selected-mode injection; only granted secrets reach it).
    script = ScriptAPI(OWNER).upsert(
        key=SCRIPT_KEY,
        name=SCRIPT_NAME,
        code=_worker_code(),
        environment=env,
        timeout_seconds=DEFAULT_TIMEOUT,
        injection_mode="selected",
        description="Managed by the Brand Tracker plugin — edit settings on its page.",
        is_enabled=True,
        notify_on=data.get("notify_on") or "failure",
        notify_email=data.get("notify_email") or "",
        created_by=created_by,
    )

    # 5) Grant the owner secrets that exist to the managed script.
    for env_key in SECRET_FIELDS.values():
        secret = secrets_api.get(env_key)
        if secret is not None:
            secrets_api.grant(script, secret)

    # 6) Weekly schedule.
    _sync_schedule(script, data)

    return script, warnings


def queue_run(triggered_by=None):
    """Queue a tracked Run of the managed tracker script (via the RunBackend seam).

    Returns ``(run, error_message)``; ``run`` is None when not runnable. Skips if a
    run is already in flight (the tracker is single-run by design — weekly cadence).
    """
    script = get_script()
    if script is None:
        return None, "Not configured yet — save the settings below first."
    if not script.can_run:
        return None, "The tracker script is disabled or archived."
    latest = ScriptAPI(OWNER).latest_run(SCRIPT_KEY)
    if latest and latest.status in ("pending", "running"):
        return None, "A run is already in progress."
    run = ScriptAPI(OWNER).queue_run(SCRIPT_KEY, triggered_by=triggered_by)
    return run, None


# --------------------------------------------------------------------------- #
# Live status + control
# --------------------------------------------------------------------------- #

def live_status():
    """A JSON-serializable snapshot for the page's status poller.

    Run state is AUTHORITATIVE via the SDK (``latest_run``); the per-keyword
    progress comes from the worker's heartbeat, shown only when it belongs to the
    current run (so a previous run's bar never lingers).
    """
    run = ScriptAPI(OWNER).latest_run(SCRIPT_KEY)
    active = bool(run and run.status in ("pending", "running"))
    progress = None
    if active:
        p = get_progress()
        if p and p.get("state") != "done" and (p.get("run_id") or "") == run.id.replace("-", ""):
            progress = p
    return {
        "active": active,
        "run": run.as_dict() if run else None,
        "progress": progress,
    }


def cancel_running():
    """Stop the latest pending/running run (SDK → shared force-stop). Returns bool."""
    return ScriptAPI(OWNER).cancel_latest_run(SCRIPT_KEY)


def schedule_summary():
    """A small {mode, label, next_run} for the header status chip (or None)."""
    sched = get_schedule()
    if sched is None:
        return None
    if sched.run_mode == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        d = (sched.weekly_days or [0])[0]
        label = f"Weekly {days[d] if 0 <= d < 7 else d} {(sched.weekly_times or ['?'])[0]}"
    else:
        label = sched.run_mode
    return {"mode": sched.run_mode, "label": label, "next_run": sched.next_run}


def _secret_or_saved(values, field):
    """The submitted secret value, or the saved one when the field was left blank."""
    submitted = (values.get(field) or "").strip()
    if submitted:
        return submitted
    secret = SecretAPI(OWNER).get(SECRET_FIELDS[field])
    return secret.get_decrypted_value() if secret is not None else ""


def test_serper(values):
    """Probe the Serper key with a tiny query (costs 1 credit). Returns {ok, message}."""
    key = _secret_or_saved(values, "serper_api_key")
    if not key:
        return {"ok": False, "message": "Enter the Serper API key (or save it first)."}

    import requests

    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": "pyrunner brand tracker test", "num": 1},
            timeout=8,
        )
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "message": f"Couldn't reach Serper ({exc.__class__.__name__})."}
    if resp.status_code in (401, 403):
        return {"ok": False, "message": "Authentication failed — check the API key."}
    if resp.status_code != 200:
        return {"ok": False, "message": f"Serper returned HTTP {resp.status_code}."}
    return {"ok": True, "message": "Connected — your Serper key works."}


def _sync_schedule(script, data):
    """v1 runs weekly only (matches the past-week search window, no gaps)."""
    api = ScheduleAPI(OWNER)
    api.sync(
        script,
        mode="weekly",
        time_str=data.get("schedule_time") or "08:00",
        weekday=int(data.get("schedule_weekday") or 0),
        tz=data.get("timezone") or "UTC",
    )
