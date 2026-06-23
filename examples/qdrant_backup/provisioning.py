"""
Provisioning for the Qdrant Backup plugin — everything goes through the SDK
(``core.plugins.api``), so the plugin owns its resources, re-saves are idempotent,
and nothing imports core models/tasks/services directly.

One config form save provisions, in the default workspace, all owned by the
``qdrant_backup`` slug:
  * a DataStore  ``qdrant_backup:state``  (entry ``config`` = non-secret config;
    entry ``runs`` = history the worker appends and the dashboard reads),
  * 3 owner-scoped Secrets  (QDRANT_API_KEY / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY),
  * a managed Script  (key ``backup``, selected-mode injection + 3 grants),
  * a Schedule.

Re-saving updates the same rows (idempotent on ``(owner_plugin, owner_key)``).
"""

import ipaddress
from pathlib import Path
from urllib.parse import urlparse

from core.plugins.api import (
    DataStoreAPI,
    EnvironmentAPI,
    ScheduleAPI,
    ScriptAPI,
    SecretAPI,
)

from .forms import SECRET_FIELDS

OWNER = "qdrant_backup"
SCRIPT_KEY = "backup"
SCRIPT_NAME = "Qdrant Backup"
STORE_KEY = "state"
DEFAULT_TIMEOUT = 3600  # large collections can take a while

# Keys of the non-secret config persisted to the DataStore (also prefill the form).
CONFIG_KEYS = ("qdrant_url", "r2_endpoint_url", "r2_bucket_name", "retention_days", "backup_prefix")


def _worker_code():
    """The managed Script's code = our bundled worker_body.py (read at provision time).

    Guards the cross-layer contract: ``worker_body.py`` is a standalone script
    (separate process — it can't import this module), so the secret env-var names
    and config keys are shared by convention. If a rename drifts them out of sync
    we fail loudly here at Save, not silently at backup time.
    """
    code = Path(__file__).with_name("worker_body.py").read_text(encoding="utf-8")
    expected = list(SECRET_FIELDS.values()) + list(CONFIG_KEYS)
    missing = [token for token in expected if token not in code]
    if missing:
        raise ValueError(
            "worker_body.py is out of sync with provisioning (missing references: "
            + ", ".join(missing)
            + "). Aborting to avoid a silently misconfigured backup."
        )
    return code


# --------------------------------------------------------------------------- #
# Reads (used by the views to render the page)
# --------------------------------------------------------------------------- #

def get_config():
    """Non-secret config dict from the owned DataStore (``{}`` if not provisioned)."""
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    if store is None:
        return {}
    return store.get("config", {}) or {}


def configured_secret_keys():
    """Set of clean env-keys that already exist as owner secrets."""
    api = SecretAPI(OWNER)
    return {k for k in SECRET_FIELDS.values() if api.get(k) is not None}


def get_script():
    return ScriptAPI(OWNER).get(SCRIPT_KEY)


def get_schedule():
    """The managed schedule (or None)."""
    scheds = ScheduleAPI(OWNER).list()
    return scheds[0] if scheds else None


def get_runs():
    """The capped run-history list the worker appends (newest last)."""
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    if store is None:
        return []
    runs = store.get("runs", [])
    return runs if isinstance(runs, list) else []


def list_environments():
    return EnvironmentAPI().list()


def initial_from_config():
    """Build the form ``initial`` dict from saved config + schedule + script."""
    cfg = get_config()
    initial = {k: cfg.get(k) for k in CONFIG_KEYS if cfg.get(k) is not None}
    if "make_zip" in cfg:
        initial["keep_zip"] = bool(cfg.get("make_zip"))

    script = get_script()
    if script is not None:
        initial["notify_on"] = script.notify_on
        initial["notify_email"] = script.notify_email
        if script.environment_id:
            initial["environment"] = script.environment.name

    sched = get_schedule()
    if sched is not None:
        initial["schedule_mode"] = sched.run_mode
        initial["timezone"] = sched.timezone
        if sched.run_mode == "daily" and sched.daily_times:
            initial["schedule_time"] = sched.daily_times[0]
        elif sched.run_mode == "weekly":
            if sched.weekly_times:
                initial["schedule_time"] = sched.weekly_times[0]
            if sched.weekly_days:
                initial["schedule_weekday"] = str(sched.weekly_days[0])
        elif sched.run_mode == "interval" and sched.interval_minutes:
            initial["schedule_interval"] = str(sched.interval_minutes)
    return initial


# --------------------------------------------------------------------------- #
# Write (the one provisioning entry point)
# --------------------------------------------------------------------------- #

def provision(data, *, created_by=None):
    """Idempotently provision/update everything from cleaned form ``data``.

    Returns ``(script, warnings)``. ``warnings`` is a list of advisory strings
    (e.g. the chosen environment is missing required packages).
    """
    warnings = []

    # 1) Owned DataStore + non-secret config entry.
    store = DataStoreAPI(OWNER).upsert(
        STORE_KEY, description="Qdrant Backup plugin config + run history", created_by=created_by
    )
    store.set("config", {
        "qdrant_url": data["qdrant_url"],
        "r2_endpoint_url": data["r2_endpoint_url"],
        "r2_bucket_name": data["r2_bucket_name"],
        "retention_days": int(data["retention_days"]),
        "backup_prefix": data.get("backup_prefix") or "qdrant-backups",
        "make_zip": bool(data.get("keep_zip")),
    })

    # 2) Owner-scoped secrets — only (re)write the ones actually supplied;
    #    a blank field keeps the existing value.
    secrets_api = SecretAPI(OWNER)
    for field_name, env_key in SECRET_FIELDS.items():
        value = (data.get(field_name) or "").strip()
        if value:
            secrets_api.upsert(env_key, value, description=f"Qdrant Backup — {env_key}")

    # 3) Environment (must exist; should carry requests + boto3).
    env = EnvironmentAPI().get(data["environment"])
    if env is None:
        raise ValueError("Select an environment (one that has requests + boto3 installed).")
    reqs = (env.requirements or "").lower()
    missing = [pkg for pkg in ("requests", "boto3") if pkg not in reqs]
    if missing:
        warnings.append(
            f"Environment '{env.name}' may be missing: {', '.join(missing)}. "
            "Add them under Environments or backups will fail at runtime."
        )

    # 4) Managed Script (selected-mode injection; isolation_mode stays 'inherit').
    script = ScriptAPI(OWNER).upsert(
        key=SCRIPT_KEY,
        name=SCRIPT_NAME,
        code=_worker_code(),
        environment=env,
        timeout_seconds=DEFAULT_TIMEOUT,
        injection_mode="selected",
        description="Managed by the Qdrant Backup plugin — edit settings on its page.",
        is_enabled=True,
        notify_on=data.get("notify_on") or "failure",
        notify_email=data.get("notify_email") or "",
        created_by=created_by,
    )

    # 5) Grant exactly the 3 owner secrets to the managed script.
    for env_key in SECRET_FIELDS.values():
        secret = secrets_api.get(env_key)
        if secret is not None:
            secrets_api.grant(script, secret)

    # 6) Schedule.
    _sync_schedule(script, data)

    return script, warnings


def queue_backup(triggered_by=None):
    """Queue a tracked Run of the managed backup script (via the RunBackend seam).

    Returns ``(run, error_message)``; ``run`` is None when not runnable.
    """
    script = get_script()
    if script is None:
        return None, "Not configured yet — save the settings below first."
    if not script.can_run:
        return None, "The backup script is disabled or archived."
    run = ScriptAPI(OWNER).queue_run(SCRIPT_KEY, triggered_by=triggered_by)
    return run, None


# --------------------------------------------------------------------------- #
# Live status + control (authoritative run state via the SDK; rich progress via
# the worker's DataStore heartbeat)
# --------------------------------------------------------------------------- #

def get_progress():
    """The worker's live progress heartbeat (or None)."""
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    return store.get("progress") if store is not None else None


def live_status():
    """A JSON-serializable snapshot for the page's status poller.

    Run state is AUTHORITATIVE via the SDK (``latest_run`` → pending/running/…);
    the per-collection progress comes from the worker's heartbeat, shown only when
    it belongs to the current run (so a previous run's bar never lingers).
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
    """Stop the latest pending/running backup (SDK → shared force-stop). Returns bool."""
    return ScriptAPI(OWNER).cancel_latest_run(SCRIPT_KEY)


def schedule_summary():
    """A small {mode, label, next_run} for the header status chip (or None)."""
    sched = get_schedule()
    if sched is None:
        return None
    mode = sched.run_mode
    if mode == "manual":
        label = "Manual"
    elif mode == "daily":
        label = f"Daily {(sched.daily_times or ['?'])[0]}"
    elif mode == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        d = (sched.weekly_days or [0])[0]
        label = f"Weekly {days[d] if 0 <= d < 7 else d} {(sched.weekly_times or ['?'])[0]}"
    elif mode == "interval":
        label = f"Every {sched.interval_minutes} min"
    else:
        label = mode
    return {"mode": mode, "label": label, "next_run": sched.next_run}


# --------------------------------------------------------------------------- #
# Test connection — instant feedback from the web process, SSRF-guarded
# --------------------------------------------------------------------------- #

def _endpoint_allowed(url):
    """Light SSRF guard: require http(s), block link-local/metadata/reserved.

    Private (RFC1918) hosts are intentionally ALLOWED — a self-hosted Qdrant on
    an internal network is a legitimate target the superuser is testing.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False, "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return False, "URL must start with http:// or https://."
    host = parsed.hostname
    if not host:
        return False, "URL has no host."
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, "That address is not allowed."
    except ValueError:
        pass  # a hostname — allowed
    return True, ""


def _secret_or_saved(values, field):
    """The submitted secret value, or the saved one when the field was left blank."""
    submitted = (values.get(field) or "").strip()
    if submitted:
        return submitted
    secret = SecretAPI(OWNER).get(SECRET_FIELDS[field])
    return secret.get_decrypted_value() if secret is not None else ""


def test_qdrant(values):
    """Probe Qdrant with the given (or saved) creds. Returns {ok, message}."""
    url = (values.get("qdrant_url") or "").strip().rstrip("/")
    ok, msg = _endpoint_allowed(url)
    if not ok:
        return {"ok": False, "message": msg}
    api_key = _secret_or_saved(values, "qdrant_api_key")
    if not api_key:
        return {"ok": False, "message": "Enter the Qdrant API key (or save it first)."}

    import requests

    try:
        resp = requests.get(f"{url}/collections", headers={"api-key": api_key}, timeout=8)
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "message": f"Couldn't reach Qdrant ({exc.__class__.__name__})."}
    if resp.status_code in (401, 403):
        return {"ok": False, "message": "Authentication failed — check the API key."}
    if resp.status_code != 200:
        return {"ok": False, "message": f"Qdrant returned HTTP {resp.status_code}."}
    try:
        cols = resp.json()["result"]["collections"]
        return {"ok": True, "message": f"Connected — {len(cols)} collection(s) found."}
    except Exception:
        return {"ok": True, "message": "Connected."}


def test_r2(values):
    """Probe R2 (head_bucket) with the given (or saved) creds. Returns {ok, message}."""
    endpoint = (values.get("r2_endpoint_url") or "").strip()
    bucket = (values.get("r2_bucket_name") or "").strip()
    ok, msg = _endpoint_allowed(endpoint)
    if not ok:
        return {"ok": False, "message": msg}
    if not bucket:
        return {"ok": False, "message": "Enter the R2 bucket name."}
    ak = _secret_or_saved(values, "r2_access_key_id")
    sk = _secret_or_saved(values, "r2_secret_access_key")
    if not (ak and sk):
        return {"ok": False, "message": "Enter the R2 access key + secret (or save them first)."}

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
        region_name="auto",
        config=Config(signature_version="s3v4", connect_timeout=8, read_timeout=8,
                      retries={"max_attempts": 0}),
    )
    try:
        client.head_bucket(Bucket=bucket)
        return {"ok": True, "message": f"Connected — bucket '{bucket}' is reachable."}
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket"):
            return {"ok": False, "message": f"Bucket '{bucket}' not found."}
        if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            return {"ok": False, "message": "Access denied — check the keys and bucket permissions."}
        return {"ok": False, "message": f"R2 error: {code or 'unknown'}."}
    except Exception as exc:  # network / endpoint / config errors
        return {"ok": False, "message": f"Couldn't reach the R2 endpoint ({exc.__class__.__name__})."}


# --------------------------------------------------------------------------- #
# Downloads — short-lived presigned R2 URLs (browser downloads direct from R2)
# --------------------------------------------------------------------------- #

def _r2_client():
    """Build an R2 (S3) client from saved config + decrypted secrets, or (None, None)."""
    cfg = get_config()
    endpoint = cfg.get("r2_endpoint_url")
    bucket = cfg.get("r2_bucket_name")
    api = SecretAPI(OWNER)
    ak = api.get("R2_ACCESS_KEY_ID")
    sk = api.get("R2_SECRET_ACCESS_KEY")
    if not (endpoint and bucket and ak and sk):
        return None, None

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ak.get_decrypted_value(),
        aws_secret_access_key=sk.get_decrypted_value(),
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return client, bucket


def presigned_url(key, *, expires=300):
    """A short-lived presigned GET URL for an R2 object key, or None if unavailable.

    Presigning is pure local signing (no network call), so it's safe in the web
    process; the browser then downloads straight from R2.
    """
    if not key:
        return None
    client, bucket = _r2_client()
    if client is None:
        return None
    try:
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
        )
    except Exception:
        return None


def resolve_download_key(date, *, collection=None, want_zip=False):
    """Return the R2 key for a recorded download, or None.

    Validated against the stored run history so only objects we actually backed
    up can be signed — never an arbitrary caller-supplied key. Scans newest-first
    so a date with multiple runs resolves to the most recent.
    """
    if not date:
        return None
    for r in reversed(get_runs()):
        if not isinstance(r, dict):
            continue
        if (r.get("date") or r.get("ts", "")[:10]) != date:
            continue
        if want_zip:
            return r.get("zip_key") or None
        for c in r.get("collections", []):
            if isinstance(c, dict) and c.get("collection") == collection:
                key = c.get("s3_key") or ""
                return key if key and key != "FAILED" else None
        return None
    return None


def _sync_schedule(script, data):
    mode = data.get("schedule_mode") or "manual"
    tz = data.get("timezone") or "UTC"
    api = ScheduleAPI(OWNER)

    if mode == "daily":
        api.sync(script, mode="daily", time_str=data.get("schedule_time") or "02:00", tz=tz)
    elif mode == "weekly":
        api.sync(
            script, mode="weekly",
            time_str=data.get("schedule_time") or "02:00",
            weekday=int(data.get("schedule_weekday") or 0),
            tz=tz,
        )
    elif mode == "interval":
        api.sync(script, mode="interval", interval_minutes=int(data.get("schedule_interval") or 360), tz=tz)
    else:
        api.sync(script, mode="manual", tz=tz)
