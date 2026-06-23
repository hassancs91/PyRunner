# Qdrant Backup (PyRunner plugin)

Schedule automatic backups of **every Qdrant collection** to **Cloudflare R2**
(S3-compatible) — configured once in a form. Unlike the simpler
`qdrant_backup_monitor` example (which only *visualizes* a script you wire up by
hand), this plugin is **self-provisioning**: one **Save** creates and keeps in
sync a managed Script, its secrets, a data store, and a schedule for you, using
the v2 plugin SDK (`core.plugins.api`).

It is the first real, installable PyRunner plugin and a reference for the
Plugin Platform v2 (dev mode · SDK · ownership + scoped secrets · doctor).

## What it does

- **Form → provision.** Enter the Qdrant URL/key, R2 endpoint/keys/bucket,
  retention, schedule, and an environment. Saving provisions (idempotently):
  - a managed **Script** `Qdrant Backup` (owner `qdrant_backup`, `injection_mode=selected`),
  - **3 owner-scoped Secrets** — `QDRANT_API_KEY`, `R2_ACCESS_KEY_ID`,
    `R2_SECRET_ACCESS_KEY` — granted only to that script (clean env names),
  - an owned **DataStore** `qdrant_backup:state` (entry `config` = non-secret
    settings; entry `runs` = backup history),
  - a **Schedule** (manual / daily / weekly / every-N-hours).
- **Back up now** queues a tracked Run through the normal RunBackend.
- **Dashboard** — a health banner + recent-run history + the latest run's
  per-collection sizes, read from the `runs` history the worker appends.
- **Alerts** use PyRunner's built-in notifications (the managed script's
  `notify_on` + `notify_email`), sent via the provider in **Settings →
  Notifications** — no bundled email integration, no Resend secret.

Non-secret config (Qdrant URL, R2 endpoint/bucket, retention, prefix) lives in
the data store so it stays visible/editable; only the 3 real credentials are
encrypted secrets.

## Downloads

The dashboard can hand you the backed-up snapshots straight from R2:

- **Per-collection** (always available) — each collection in the *latest run*
  table has a `↓` link to that snapshot.
- **Combined `.zip`** (opt-in) — tick **“Also store a combined .zip per backup”**
  in the form. The worker then bundles each run’s snapshots into one
  `backup-<date>.zip` (stored, not recompressed — snapshots are already dense)
  and uploads it next to the per-collection files. A **“Download .zip”** button
  appears in the header (latest run) and per row in the history table.

**How it works (and why it’s safe):** the download view is superuser-only and
resolves the object key from the recorded run history — it never signs an
arbitrary key. It then mints a **short-lived (5-minute) presigned R2 URL** and
redirects you to it, so the file downloads **browser → R2 directly** and never
streams through the PyRunner process (no memory/bandwidth cost, scales to large
snapshots). Presigning is pure local signing — no extra credentials beyond the
R2 secrets you already configured.

Notes:
- Download links only appear for runs created **after** this feature was added
  (older run records don’t carry the object keys) — make a fresh backup first.
- The combined `.zip` roughly **doubles R2 storage** for those runs (individual
  snapshots **and** the zip). Retention cleanup prunes the zip too, since it
  lives in the same dated folder.

## Security

The plugin is designed so a superuser can run it without handing PyRunner more
trust than it needs:

- **Superuser-only.** Every view is gated (`user_passes_test(is_superuser)`) —
  no part of the page or its JSON/download endpoints is reachable otherwise.
- **Credentials are encrypted, scoped secrets.** The 3 keys
  (`QDRANT_API_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`) are stored as
  owner-scoped Secrets and **granted only to the managed backup script**
  (selected-mode injection), so they reach that one script as clean env vars and
  nothing else. Non-secret config (URLs, bucket, retention) lives in the data
  store in plain text — only real credentials are encrypted.
- **Write-only credential fields.** The form never renders a stored secret back;
  leaving a field blank keeps the existing value.
- **Downloads can't be coerced.** The download view never signs a key from the
  query string — it resolves the object key from the **recorded run history**,
  mints a **5-minute presigned R2 URL**, and redirects. So only files this
  plugin actually backed up are ever downloadable, and only briefly.
- **Test-connection is SSRF-guarded.** "Test Qdrant/R2" only allows `http(s)`
  and blocks link-local/metadata (`169.254.169.254`), multicast, reserved and
  unspecified addresses. Private (RFC1918) hosts are intentionally allowed so a
  self-hosted Qdrant on an internal network can be tested.

These boundaries are covered by `tests.py` (`EndpointGuardTests`,
`ResolveDownloadKeyTests`) so a future change can't quietly weaken them.

## Install

1. **Environment** — under *Environments*, create/choose one that installs
   `requests` and `boto3`.
2. **Upload** `qdrant_backup.zip` (Plugins → Upload), **Activate** (runs the
   doctor + isolated preflight), then **Restart**.
3. Open **Qdrant Backup** in the sidebar (superuser only), fill the form, **Save**,
   then **Back up now** (or let the schedule run it).

> Want to see the dashboard without real credentials? Save the form once (placeholder
> creds are fine — it just provisions the data store), then run
> [`demo_seed.py`](demo_seed.py) as a script to populate sample history.

## Develop locally (dev mode)

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/qdrant_backup
python manage.py runserver
```

Validate anytime without uploading:

```bash
python manage.py plugin_doctor --path examples/qdrant_backup
```

## Package the zip

From the repo root:

```bash
cd examples
zip -r qdrant_backup.zip qdrant_backup -x '*/__pycache__/*'
```

The archive must contain a single top-level folder (`qdrant_backup/`) — the slug,
matching `plugin.json` and the `apps.py` descriptor.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Save warns "Environment … may be missing: boto3/requests" | The chosen environment lacks the worker's packages. Add `requests` + `boto3` under *Environments*, or backups fail at runtime. |
| **Test Qdrant** → "Authentication failed" | Wrong `QDRANT_API_KEY`. "Couldn't reach Qdrant" → wrong URL / network / firewall. |
| **Test R2** → "Access denied" | Wrong R2 keys or insufficient bucket permissions. "Bucket not found" → wrong bucket name or endpoint. |
| Backup status is **partial** | Some collections succeeded, some failed — open the run row to see the per-collection error; the run also exits non-zero so your `notify_on` alert fires. |
| No **download** links on a run | Links only appear for runs created *after* the download feature shipped (older records don't carry object keys) — run a fresh backup. Downloads are superuser-only. |
| Large uploads fail with `SSLV3_ALERT_BAD_RECORD_MAC` | A flaky/intercepting network path corrupting TLS records. The worker already retries each multipart part on a fresh connection; if it persists, the network path itself is the problem. |
| Schedule never runs | Check the schedule mode/next-run in the header chip, and that PyRunner's scheduler isn't globally paused. |
| Dashboard is empty but you've saved | No backups have run yet — click **Back up now**, or seed sample history with [`demo_seed.py`](demo_seed.py). |

## Tests

```bash
python manage.py test core.test_qdrant_backup_plugin
```

The tests live in [`tests.py`](tests.py) and run via a thin shim in
`core/test_qdrant_backup_plugin.py` (so they're picked up by `manage.py test
core`). They cover the SSRF guard, download-key resolution, the worker
secret/config contract, form validation, and idempotent provisioning.

## Files

| File | Where it runs | Purpose |
|---|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | web process | the plugin page (config form + dashboard) |
| `provisioning.py` | web process | all SDK calls (idempotent provision + reads) |
| `worker_body.py` | environment venv | the managed backup script's code |
| `demo_seed.py` | environment venv | optional — fake history to preview the dashboard |
| `tests.py` | test runner | unit tests for the web/provisioning + security surface |

See `docs/plugins.md` for the full author guide.
