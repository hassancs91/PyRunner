"""
=============================================================================
 SDK SHOWCASE — demo worker  (managed by the "SDK Showcase" plugin)
=============================================================================

 A deliberately tiny, STANDARD-LIBRARY-ONLY script the plugin provisions to
 demonstrate the *worker side* of the plugin SDK. You do NOT run this by hand —
 the plugin page provisions it, its secret, a data store, and a schedule.

 Every run it:
   1. reads an injected secret from the environment ($DEMO_TOKEN), redacted,
   2. reads non-secret config (message, steps) from the owned data store,
   3. loops `steps` times, writing a live progress heartbeat each step,
   4. records a compact run summary into the data store (the dashboard reads it).

 It needs NO third-party packages, so it runs in any environment.
=============================================================================
"""

import os
import sys
import time
from datetime import datetime

# Emoji/log lines crash on a non-UTF-8 console; PyRunner captures stdout as UTF-8
# but make standalone runs robust too.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The injected secret (selected-mode grant) — present when run under PyRunner.
DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")

# Derive the owner slug from the env var PyRunner injects for owned-script runs,
# so the data-store name is never hardcoded (fall back to the literal slug).
OWNER = os.environ.get("PYRUNNER_OWNER_PLUGIN") or "sdk_showcase"
STATE_STORE = f"{OWNER}:state"
RUN_ID = os.environ.get("PYRUNNER_RUN_ID", "")  # ties live progress to this run
HISTORY_LIMIT = 20


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


def _store():
    """The owned data store handle, or None when not running under PyRunner."""
    try:
        from pyrunner_datastore import DataStore

        return DataStore(STATE_STORE)
    except Exception:
        return None  # local/standalone run — degrade gracefully


def load_config():
    """Read non-secret config (message, steps) from the data store."""
    defaults = {"message": "", "steps": 5}
    store = _store()
    if store is None:
        return defaults
    cfg = store.get("config", {}) or {}
    return {
        "message": cfg.get("message", defaults["message"]),
        "steps": int(cfg.get("steps") or defaults["steps"]),
    }


def write_progress(state, *, index=0, total=0, phase=""):
    """Best-effort live heartbeat the plugin page polls; tagged with this run id.
    A failure here never affects the run."""
    store = _store()
    if store is None:
        return
    try:
        store["progress"] = {
            "run_id": RUN_ID, "state": state, "index": index, "total": total,
            "phase": phase, "ts": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        }
    except Exception:
        pass


def record_run(status, steps, token_seen, message, duration_s):
    """Append a compact run summary to the data store (best effort)."""
    store = _store()
    if store is None:
        return
    try:
        record = {
            "ts": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "status": status, "steps": steps, "token_seen": token_seen,
            "message": message, "duration_s": round(duration_s, 1),
        }
        runs = store.get("runs", [])
        if not isinstance(runs, list):
            runs = []
        runs.append(record)
        store["runs"] = runs[-HISTORY_LIMIT:]
    except Exception as exc:
        log(f"could not record run: {exc}")


def main():
    start = time.time()
    cfg = load_config()
    steps = cfg["steps"]
    message = cfg["message"]
    token_seen = bool(DEMO_TOKEN)
    if len(DEMO_TOKEN) >= 2:
        redacted = DEMO_TOKEN[:1] + "***" + DEMO_TOKEN[-1:]
    else:
        redacted = "***" if token_seen else "(none)"

    log("=" * 52)
    log("SDK SHOWCASE — demo worker starting")
    log(f"  owner slug:   {OWNER}")
    log(f"  DEMO_TOKEN:   {redacted}   (injected secret)")
    log(f"  message:      {message!r}   (from the data store)")
    log(f"  steps:        {steps}")
    log("=" * 52)

    write_progress("running", index=0, total=steps, phase="starting")
    for i in range(1, steps + 1):
        log(f"  step {i}/{steps} …")
        write_progress("running", index=i, total=steps, phase="working")
        time.sleep(1)

    duration = time.time() - start
    record_run("success", steps, token_seen, message, duration)
    write_progress("done", index=steps, total=steps)
    log(f"done in {duration:.1f}s — recorded a run summary to '{STATE_STORE}'.")


if __name__ == "__main__":
    main()
