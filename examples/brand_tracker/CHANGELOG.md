# Changelog

All notable changes to the Brand Tracker plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-07-15

External read-only API via PyRunner's plugin API seam (SDK 2.3, PyRunner ≥ 1.16).

### Added
- **External API** at `/api/v1/plugins/brand_tracker/…` (plugin-scoped API
  token required; create one under Settings → API Tokens):
  - `mentions/` — the deduped mention feed, filterable by
    `keyword` / `source` / `sentiment` / `since` and paginated
    (`page` / `page_size`, max 100).
  - `stats/` — window/all-time totals, per-keyword and per-source counters,
    and the recent run history.
- Manifest now declares the resources under `provides.api_resources`
  (`api: "2.3"`, `min_pyrunner: "1.16.0"`).
- **Shareable public report** — a "Share report" button on the dashboard
  publishes a read-only, script-free mentions report at a private
  `/p/<token>/` capability URL (no login). Revoking kills the link
  permanently; re-sharing issues a new URL. Manage links under
  Settings → API Tokens.

## [1.0.0] - 2026-06-28

First release — a self-provisioning PyRunner plugin (SDK / `core.plugins.api`).

### Added
- One-form setup that idempotently provisions a managed Script, owner-scoped
  secrets, an owned DataStore, and a **weekly** schedule.
- **Multi-source tracking** with a pluggable source layer: Serper web + news
  (credit-bearing), Hacker News (Algolia) and Reddit (OAuth) — all free. Each
  keyword is searched as an exact phrase over the past week.
- **Canonical-URL dedup** across sources (https/www/m./AMP/utm + click-IDs
  collapse) so the same article is never reported twice; proper domain exclusion
  (subdomain-aware, not substring).
- **90-day retention** prune of the dedup index + feed, with a 2000-item cap.
- **First-run seed** — the first run fills the feed without emailing, so a fresh
  install never floods you.
- **Serper credit tracking** — a per-month counter (1 credit ≤10 results, 2
  above) with an optional monthly cap that pauses Serper while free sources keep
  running; a live cost estimate in the form.
- **Two-tab UI** (Mentions · Settings): a live mention feed with source pills +
  client-side filters, stat cards, a credit gauge, run history, **Run now / Stop**
  (`ScriptAPI.queue_run` / `cancel_latest_run`), a live status poller, and a
  **Test Serper key** button.
- **Optional AI enrichment** (source type + sentiment): `off` / Claude
  (platform `pyrunner_ai`) / OpenRouter (BYO key). Only new mentions, batched
  ≤25/call with a 100/run ceiling, and degrades silently when unavailable.
- **Optional email report** via Resend (the dashboard feed works with no email
  setup); operational failure alerts via PyRunner's built-in notifications.
- Test suite (`tests.py`) over the engine (dedup/exclusion/retention/enrichment)
  and the web/provisioning surface, plus README **Troubleshooting**.

### Notes
- Requires PyRunner **1.13.0+** (plugin SDK API `2.1`: run-lifecycle surface).
- Runs in a PyRunner environment that has `requests` (add `claude-agent-sdk` for
  Claude enrichment).
- Tracks the indexed web + open communities; walled social (X/Instagram/TikTok/
  LinkedIn) is out of scope (the source layer is pluggable for a future paid
  provider).
