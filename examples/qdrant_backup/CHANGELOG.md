# Changelog

All notable changes to the Qdrant Backup plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-22

First release — a self-provisioning PyRunner plugin (SDK / `core.plugins.api`).

### Added
- One-form setup that idempotently provisions a managed Script, 3 owner-scoped
  secrets, an owned DataStore, and a schedule.
- Backup worker: snapshots every Qdrant collection and uploads to Cloudflare R2,
  with a retention sweep.
- **Two-tab UI** (Settings · Backups) with a header status chip (schedule, next
  run, last backup).
- **Live status** while a backup runs — authoritative run state via
  `ScriptAPI.latest_run` plus a worker DataStore heartbeat for per-collection
  progress; a **Stop** button via `ScriptAPI.cancel_latest_run`.
- **Test connection** for Qdrant and R2 (SSRF-guarded, web-process).
- **Downloads** — per-collection snapshots and an optional combined `.zip` per
  backup, served via short-lived presigned R2 URLs (browser → R2 direct).
- Alerts via PyRunner's built-in notifications (no bundled email integration).
- Test suite (`tests.py`) over the web/provisioning + security surface, and
  README **Security** + **Troubleshooting** sections.

### Notes
- Requires PyRunner **1.13.0+** (plugin SDK API `2.1`: run-lifecycle surface).
- Backup runs in a PyRunner environment that has `requests` and `boto3`.
- R2 uploads use a manual multipart with per-part retry (resilient to transient
  TLS/network blips; one bad part re-sends just that part).
