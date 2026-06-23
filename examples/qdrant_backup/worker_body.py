"""
=============================================================================
 QDRANT BACKUP TO CLOUDFLARE R2  —  managed by the "Qdrant Backup" plugin
=============================================================================

 This is the backup worker the plugin provisions as a PyRunner Script. You do
 NOT edit or run it by hand — configure everything on the plugin page
 (/plugins/qdrant_backup/) and it provisions this script, its secrets, a data
 store, and a schedule for you.

 WHAT IT DOES (every run)
   1. Lists all Qdrant collections
   2. Snapshots each one, downloads it, uploads it to Cloudflare R2 by date
   3. Cleans the snapshot off the Qdrant server
   4. Deletes R2 backups older than the retention window
   5. Records a compact run summary into the `qdrant_backup:state` data store,
      which the plugin dashboard reads.

 ALERTS are handled by PyRunner itself: the managed Script has notify_on set, so
 PyRunner emails through the provider configured in Settings -> Notifications.
 There is no bundled email integration here.

 SECRETS (the only sensitive values) come from the plugin's owner-scoped secrets,
 injected as clean env vars (selected-mode injection):
   QDRANT_API_KEY, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

 NON-SECRET CONFIG (Qdrant URL, R2 endpoint/bucket, retention, prefix) is read
 from the `qdrant_backup:state` data store (entry "config"), so this script body
 is identical for every install and the plugin page can show/edit it in plain text.

 ENVIRONMENT must provide: requests, boto3
=============================================================================
"""

import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
import requests
from botocore.config import Config

# Emoji in log lines crash on a non-UTF-8 console (e.g. a Windows cp1252 shell).
# PyRunner captures stdout as UTF-8, but make standalone runs robust too.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Scratch dir for snapshot downloads / the combined zip (portable, not hardcoded /tmp).
SCRATCH_DIR = tempfile.gettempdir()


# =============================================================================
# CONFIGURATION
# =============================================================================

# The 3 sensitive credentials — injected as clean env vars by selected-mode grants.
QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]

# The plugin's owner slug + owned data store. Derive the slug from the env var
# PyRunner injects for owned-script runs (PYRUNNER_OWNER_PLUGIN) so the store name
# is never hardcoded; fall back to the literal slug for local/standalone runs.
OWNER = os.environ.get("PYRUNNER_OWNER_PLUGIN") or "qdrant_backup"
STATE_STORE = f"{OWNER}:state"
RUN_ID = os.environ.get("PYRUNNER_RUN_ID", "")  # ties live progress to this run
HISTORY_LIMIT = 50  # keep only the most recent N runs in the dashboard

REQUEST_TIMEOUT = 300

# R2 upload robustness. Large uploads can fail with "SSLV3_ALERT_BAD_RECORD_MAC"
# — a corrupted TLS record on the wire (not an auth/data problem), typically a
# flaky/intercepting network path that scrambles bytes during big transfers. We
# do our OWN multipart upload with PER-PART retry on a fresh TLS connection, so a
# scrambled part only re-sends THAT part (not the whole file) and survives an
# occasional bad record. boto3's upload_file aborts the whole transfer on one
# part, which is why a corrupted part there killed the entire backup.
PART_SIZE = 16 * 1024 * 1024        # 16 MB per part (R2 requires >= 5 MB except last)
MULTIPART_THRESHOLD = 16 * 1024 * 1024  # files >= this go multipart; smaller = one PUT
PART_MAX_ATTEMPTS = 6               # per-part retries (fresh connection each time)
UPLOAD_MAX_ATTEMPTS = 5             # retries for the single-PUT (small file) path
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"})


def _s3_client():
    """A fresh R2 (S3-compatible) client with retry config."""
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=_BOTO_CONFIG,
    )


def load_plugin_config():
    """Read non-secret config (endpoints, retention, prefix) from the data store.

    Raises if the store/config is missing while running under PyRunner — the
    backup can't proceed without the Qdrant URL / R2 endpoint / bucket. Falls
    back to empty defaults only when not running under PyRunner (local testing).
    """
    defaults = {
        "qdrant_url": "",
        "r2_endpoint_url": "",
        "r2_bucket_name": "",
        "retention_days": 30,
        "backup_prefix": "qdrant-backups",
        "make_zip": False,
    }
    try:
        from pyrunner_datastore import DataStore
    except Exception:
        return defaults  # not under PyRunner
    cfg = DataStore(STATE_STORE).get("config", {}) or {}
    merged = {**defaults, **cfg}
    merged["retention_days"] = int(merged.get("retention_days") or 30)
    merged["backup_prefix"] = merged.get("backup_prefix") or "qdrant-backups"
    merged["make_zip"] = bool(merged.get("make_zip"))
    return merged


_cfg = load_plugin_config()
QDRANT_URL = _cfg["qdrant_url"]
R2_ENDPOINT_URL = _cfg["r2_endpoint_url"]
R2_BUCKET_NAME = _cfg["r2_bucket_name"]
RETENTION_DAYS = _cfg["retention_days"]
R2_BACKUP_PREFIX = _cfg["backup_prefix"]
MAKE_ZIP = _cfg["make_zip"]


# =============================================================================
# HELPER: Logging with timestamps
# =============================================================================

def log(message):
    """Print a timestamped log message to stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


# =============================================================================
# DASHBOARD: record this run into the qdrant_backup:state data store (best effort)
# =============================================================================

def write_progress(state, *, index=0, total=0, collection="", phase=""):
    """Best-effort live-progress heartbeat the plugin page polls while a backup
    runs. Writes a single `progress` entry to the state store; tagged with this
    run's id so the dashboard only shows progress for the current run. A failure
    here never affects the backup."""
    try:
        from pyrunner_datastore import DataStore

        DataStore(STATE_STORE)["progress"] = {
            "run_id": RUN_ID,
            "state": state,  # "running" | "done"
            "index": index,
            "total": total,
            "collection": collection,
            "phase": phase,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        pass


def record_run(status, results, total_time, deleted_old=0, error=None,
               run_date=None, zip_key=None):
    """Append a compact record of this run to the plugin's data store.

    Best-effort: a missing/unavailable data store never breaks the backup — it
    only means the dashboard won't update for this run. `results` items use the
    same shape main() builds: {"collection", "size_mb", "s3_key", "error"?}.
    `run_date`/`zip_key` let the dashboard build download links (per-collection
    snapshot + the optional combined zip).
    """
    try:
        from pyrunner_datastore import DataStore
    except Exception:
        # Not running under PyRunner (e.g. local testing) — skip silently.
        return

    try:
        store = DataStore(STATE_STORE)
    except Exception as exc:
        log(f"⚠️  Dashboard: data store '{STATE_STORE}' unavailable ({exc}).")
        return

    collections = []
    ok_count = 0
    failed_count = 0
    total_size = 0.0
    for r in results:
        is_ok = r.get("s3_key") != "FAILED" and not r.get("error")
        if is_ok:
            ok_count += 1
            total_size += r.get("size_mb", 0) or 0
        else:
            failed_count += 1
        collections.append({
            "collection": r.get("collection", "—"),
            "size_mb": round(r.get("size_mb", 0) or 0, 2),
            "status": "ok" if is_ok else "failed",
            "error": r.get("error", ""),
            # The R2 object key — used by the dashboard's per-collection download
            # link (empty / "FAILED" rows are simply not linkable).
            "s3_key": r.get("s3_key", "") if is_ok else "",
        })

    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": run_date or datetime.now().strftime("%Y-%m-%d"),
        "status": status,  # "success" | "partial" | "failed"
        "duration_s": round(total_time, 1),
        "collection_count": ok_count,
        "failed_count": failed_count,
        "total_size_mb": round(total_size, 2),
        "deleted_old": deleted_old,
        "error": str(error) if error else "",
        "zip_key": zip_key or "",
        "collections": collections,
    }

    try:
        runs = store.get("runs", [])
        if not isinstance(runs, list):
            runs = []
        runs.append(record)
        store["runs"] = runs[-HISTORY_LIMIT:]
        log(f"📊 Dashboard: recorded run (status={status})")
    except Exception as exc:
        log(f"⚠️  Dashboard: failed to record run ({exc}).")


def derive_status(results):
    """success = all ok (or nothing to do); failed = all failed; else partial."""
    if not results:
        return "success"
    failed = sum(1 for r in results if r.get("error") or r.get("s3_key") == "FAILED")
    if failed == 0:
        return "success"
    if failed == len(results):
        return "failed"
    return "partial"


# =============================================================================
# STEP 1: Get all collection names from Qdrant
# =============================================================================

def get_all_collections():
    """Fetch the list of ALL collections from the Qdrant instance."""
    log("📂 Fetching list of all collections from Qdrant...")

    url = f"{QDRANT_URL}/collections"
    headers = {"api-key": QDRANT_API_KEY}

    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    data = response.json()
    collections = [c["name"] for c in data["result"]["collections"]]

    log(f"   Found {len(collections)} collection(s): {', '.join(collections)}")
    return collections


# =============================================================================
# STEP 2: Create a snapshot for a collection
# =============================================================================

def create_snapshot(collection_name):
    """Trigger Qdrant to create a snapshot for the given collection."""
    log(f"📸 Creating snapshot for collection: '{collection_name}'...")

    url = f"{QDRANT_URL}/collections/{collection_name}/snapshots"
    headers = {"api-key": QDRANT_API_KEY}

    response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    snapshot_info = response.json()["result"]
    snapshot_name = snapshot_info["name"]
    snapshot_size = snapshot_info.get("size", "unknown")

    log(f"   ✅ Snapshot created: {snapshot_name} (size: {snapshot_size})")
    return snapshot_name


# =============================================================================
# STEP 3: Download the snapshot file from Qdrant
# =============================================================================

def download_snapshot(collection_name, snapshot_name):
    """Download the snapshot file from Qdrant server (streamed)."""
    log(f"⬇️  Downloading snapshot '{snapshot_name}' from Qdrant...")

    url = f"{QDRANT_URL}/collections/{collection_name}/snapshots/{snapshot_name}"
    headers = {"api-key": QDRANT_API_KEY}

    response = requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    local_path = os.path.join(SCRATCH_DIR, snapshot_name)
    total_bytes = 0

    with open(local_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            total_bytes += len(chunk)

    size_mb = total_bytes / (1024 * 1024)
    log(f"   ✅ Downloaded {size_mb:.2f} MB to {local_path}")
    return local_path


# =============================================================================
# STEP 4: Upload the snapshot to Cloudflare R2
# =============================================================================

def _put_simple(local_path, s3_key):
    """Single PUT for a small file, retried on a fresh connection each attempt."""
    last_err = None
    with open(local_path, "rb") as f:
        data = f.read()
    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            _s3_client().put_object(Bucket=R2_BUCKET_NAME, Key=s3_key, Body=data)
            return s3_key
        except Exception as e:
            last_err = e
            if attempt < UPLOAD_MAX_ATTEMPTS:
                wait = min(2 ** attempt, 10)
                log(f"   ⚠️  Upload attempt {attempt}/{UPLOAD_MAX_ATTEMPTS} failed "
                    f"({e}). Retrying in {wait}s...")
                time.sleep(wait)
    raise last_err


def _upload_part(s3_key, upload_id, part_number, data):
    """Upload one part, retrying on a FRESH client (= fresh TLS connection) so a
    single corrupted record ('bad record mac') doesn't doom the whole file."""
    last_err = None
    for attempt in range(1, PART_MAX_ATTEMPTS + 1):
        try:
            resp = _s3_client().upload_part(
                Bucket=R2_BUCKET_NAME, Key=s3_key, UploadId=upload_id,
                PartNumber=part_number, Body=data,
            )
            return resp["ETag"]
        except Exception as e:
            last_err = e
            if attempt < PART_MAX_ATTEMPTS:
                wait = min(2 ** attempt, 15)
                log(f"   ⚠️  Part {part_number} attempt {attempt}/{PART_MAX_ATTEMPTS} "
                    f"failed ({e}). Retrying in {wait}s...")
                time.sleep(wait)
    raise last_err


def _put_multipart(local_path, s3_key):
    """Manual multipart upload with per-part retry; aborts cleanly on give-up."""
    client = _s3_client()
    upload_id = client.create_multipart_upload(Bucket=R2_BUCKET_NAME, Key=s3_key)["UploadId"]
    parts = []
    try:
        with open(local_path, "rb") as f:
            part_number = 1
            while True:
                chunk = f.read(PART_SIZE)
                if not chunk:
                    break
                etag = _upload_part(s3_key, upload_id, part_number, chunk)
                parts.append({"PartNumber": part_number, "ETag": etag})
                part_number += 1
        _s3_client().complete_multipart_upload(
            Bucket=R2_BUCKET_NAME, Key=s3_key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return s3_key
    except Exception:
        # Don't leave a dangling multipart upload accruing storage on R2.
        try:
            _s3_client().abort_multipart_upload(
                Bucket=R2_BUCKET_NAME, Key=s3_key, UploadId=upload_id
            )
        except Exception:
            pass
        raise


def _put_to_r2(local_path, s3_key):
    """Upload one local file to R2: a single PUT for small files, a per-part-retry
    multipart for large ones (robust against a flaky link's 'bad record mac')."""
    if os.path.getsize(local_path) < MULTIPART_THRESHOLD:
        result = _put_simple(local_path, s3_key)
    else:
        result = _put_multipart(local_path, s3_key)
    log(f"   ✅ Uploaded successfully to: s3://{R2_BUCKET_NAME}/{s3_key}")
    return result


def upload_to_r2(local_path, collection_name, run_date):
    """Upload a collection snapshot to R2 under the run's date folder."""
    s3_key = f"{R2_BACKUP_PREFIX}/{run_date}/{collection_name}.snapshot"
    log(f"☁️  Uploading to R2: {s3_key}...")
    return _put_to_r2(local_path, s3_key)


# =============================================================================
# STEP 5: Delete the snapshot from Qdrant server (cleanup)
# =============================================================================

def delete_qdrant_snapshot(collection_name, snapshot_name):
    """Remove the snapshot file from the Qdrant server to free disk space."""
    log(f"🗑️  Cleaning up snapshot from Qdrant server: {snapshot_name}...")

    url = f"{QDRANT_URL}/collections/{collection_name}/snapshots/{snapshot_name}"
    headers = {"api-key": QDRANT_API_KEY}

    response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    log(f"   ✅ Snapshot removed from Qdrant server")


# =============================================================================
# STEP 6: Clean up old backups from R2 (retention policy)
# =============================================================================

def cleanup_old_backups():
    """Delete backup folders in R2 older than RETENTION_DAYS. Returns the count."""
    log(f"🧹 Cleaning up backups older than {RETENTION_DAYS} days from R2...")

    s3_client = _s3_client()

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    log(f"   Cutoff date: {cutoff_date.strftime('%Y-%m-%d')}")

    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=f"{R2_BACKUP_PREFIX}/")

    objects_to_delete = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) >= 2:
                date_str = parts[1]
                try:
                    backup_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if backup_date < cutoff_date:
                        objects_to_delete.append({"Key": key})
                except ValueError:
                    continue

    if objects_to_delete:
        for i in range(0, len(objects_to_delete), 1000):
            batch = objects_to_delete[i:i + 1000]
            s3_client.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": batch})
        log(f"   ✅ Deleted {len(objects_to_delete)} old backup file(s)")
    else:
        log(f"   ✅ No old backups to clean up")

    return len(objects_to_delete)


# =============================================================================
# STEP 7: Remove local temp file
# =============================================================================

def cleanup_local_file(local_path):
    """Remove the temporary local snapshot file after upload."""
    try:
        os.remove(local_path)
        log(f"   🗑️  Removed local temp file: {local_path}")
    except OSError:
        log(f"   ⚠️  Could not remove temp file: {local_path}")


# =============================================================================
# MAIN: Orchestrate the full backup pipeline
# =============================================================================

def main():
    log("=" * 60)
    log("🚀 QDRANT BACKUP TO CLOUDFLARE R2 — Starting...")
    log("=" * 60)
    log(f"   Qdrant URL:   {QDRANT_URL}")
    log(f"   R2 Bucket:    {R2_BUCKET_NAME}")
    log(f"   Prefix:       {R2_BACKUP_PREFIX}")
    log(f"   Retention:    {RETENTION_DAYS} days")
    log("")

    start_time = time.time()
    results = []
    run_date = datetime.now().strftime("%Y-%m-%d")  # one date for all of this run
    zipf = None
    zip_local_path = None

    try:
        # ---- Step 1: Get all collections ----
        collections = get_all_collections()

        if not collections:
            log("⚠️  No collections found in Qdrant. Nothing to back up.")
            total_time = time.time() - start_time
            record_run("success", results, total_time, deleted_old=0, run_date=run_date)
            write_progress("done", total=0)
            return

        total = len(collections)
        write_progress("running", index=0, total=total, phase="starting")

        # Optional: bundle every snapshot into one downloadable zip (config make_zip).
        if MAKE_ZIP:
            zip_local_path = os.path.join(SCRATCH_DIR, f"backup-{run_date}.zip")
            zipf = zipfile.ZipFile(zip_local_path, "w", zipfile.ZIP_STORED)
            log(f"🧰 Combined zip enabled → backup-{run_date}.zip")
            log("")

        # ---- Step 2: Backup each collection ----
        for i, collection_name in enumerate(collections, 1):
            log("")
            log(f"{'─' * 40}")
            log(f"📦 Processing collection {i}/{len(collections)}: '{collection_name}'")
            log(f"{'─' * 40}")
            write_progress("running", index=i, total=total, collection=collection_name, phase="backing up")

            try:
                snapshot_name = create_snapshot(collection_name)
                local_path = download_snapshot(collection_name, snapshot_name)
                file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
                # Add to the combined zip BEFORE the per-file upload, so the zip
                # captures every downloaded snapshot even if one R2 upload blips.
                if zipf is not None:
                    zipf.write(local_path, arcname=f"{collection_name}.snapshot")
                s3_key = upload_to_r2(local_path, collection_name, run_date)
                delete_qdrant_snapshot(collection_name, snapshot_name)
                cleanup_local_file(local_path)

                results.append({
                    "collection": collection_name,
                    "size_mb": file_size_mb,
                    "s3_key": s3_key,
                })
                log(f"   ✅ Collection '{collection_name}' backed up successfully!")

            except Exception as e:
                log(f"   ❌ ERROR backing up '{collection_name}': {str(e)}")
                results.append({
                    "collection": collection_name,
                    "size_mb": 0,
                    "s3_key": "FAILED",
                    "error": str(e),
                })

            if i < len(collections):
                time.sleep(2)

        # ---- Step 3: Upload the combined zip (if enabled) ----
        zip_key = None
        if zipf is not None:
            zipf.close()
            zipf = None
            try:
                log("")
                log(f"🧰 Uploading combined zip: backup-{run_date}.zip ...")
                zip_key = _put_to_r2(
                    zip_local_path, f"{R2_BACKUP_PREFIX}/{run_date}/backup-{run_date}.zip"
                )
            except Exception as e:
                log(f"   ⚠️  Combined zip upload failed ({e}) — individual snapshots are still backed up.")
            finally:
                cleanup_local_file(zip_local_path)

        # ---- Step 4: Clean up old backups from R2 ----
        log("")
        deleted_count = cleanup_old_backups()

        # ---- Step 5: Record to dashboard ----
        log("")
        total_time = time.time() - start_time
        status = derive_status(results)
        record_run(status, results, total_time, deleted_old=deleted_count,
                   run_date=run_date, zip_key=zip_key)
        write_progress("done", index=total, total=total)

        # ---- Final Summary ----
        ok = sum(1 for r in results if r.get("s3_key") != "FAILED" and not r.get("error"))
        log("")
        log("=" * 60)
        log(f"🏁 BACKUP COMPLETE!")
        log(f"   Status:                {status}")
        log(f"   Collections backed up: {ok}")
        log(f"   Old files cleaned up:  {deleted_count}")
        log(f"   Total time:            {total_time:.1f} seconds")
        log("=" * 60)

        # A partial backup is still a failure worth alerting on.
        if status != "success":
            sys.exit(1)

    except Exception as e:
        log(f"💥 CRITICAL ERROR: {str(e)}")
        if zipf is not None:
            try:
                zipf.close()
            except Exception:
                pass
        total_time = time.time() - start_time
        record_run("failed", results, total_time, deleted_old=0, error=e, run_date=run_date)
        write_progress("done")
        sys.exit(1)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()
