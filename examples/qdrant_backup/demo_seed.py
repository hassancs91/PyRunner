"""
Demo seed for the Qdrant Backup dashboard — populate the `qdrant_backup:state`
data store with a few fake runs so you can see the dashboard with NO real Qdrant
or R2 credentials.

HOW TO USE
  1. Open the Qdrant Backup plugin page and click "Save settings" once. That
     provisions the `qdrant_backup:state` data store (and the managed script).
     You can put placeholder credentials in — we won't run the real backup here.
  2. Scripts -> new script, paste this file, pick any environment, and Run it.
  3. Open the Qdrant Backup page — the dashboard now shows sample history.

`pyrunner_datastore` is provided by PyRunner automatically — no install needed.
This writes ONLY to the `runs` key, so it never clobbers your saved `config`.
"""

from datetime import datetime, timedelta

from pyrunner_datastore import DataStore

STATE_STORE = "qdrant_backup:state"

# A small, realistic-looking history: a healthy run, a partial, and a clean run.
now = datetime.now()


def _run(minutes_ago, status, collections, deleted_old=0, error=""):
    ok = [c for c in collections if c["status"] == "ok"]
    return {
        "ts": (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "duration_s": round(2.5 * len(collections) + 4, 1),
        "collection_count": len(ok),
        "failed_count": len(collections) - len(ok),
        "total_size_mb": round(sum(c["size_mb"] for c in ok), 2),
        "deleted_old": deleted_old,
        "error": error,
        "collections": collections,
    }


history = [
    _run(2880, "success", [
        {"collection": "products", "size_mb": 41.2, "status": "ok", "error": ""},
        {"collection": "support_kb", "size_mb": 12.8, "status": "ok", "error": ""},
    ], deleted_old=2),
    _run(1440, "partial", [
        {"collection": "products", "size_mb": 43.7, "status": "ok", "error": ""},
        {"collection": "support_kb", "size_mb": 0, "status": "failed",
         "error": "ReadTimeout: snapshot download timed out"},
    ]),
    _run(30, "success", [
        {"collection": "products", "size_mb": 44.1, "status": "ok", "error": ""},
        {"collection": "support_kb", "size_mb": 13.0, "status": "ok", "error": ""},
    ], deleted_old=1),
]

store = DataStore(STATE_STORE)
store["runs"] = history
print(f"Seeded {len(history)} demo runs into '{STATE_STORE}'. Open the Qdrant Backup page.")
