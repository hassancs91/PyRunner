# Changelog

All notable changes to PyRunner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The authoritative version number lives in `pyrunner/version.py`. This changelog
begins tracking at the current release; earlier history is in the git log.

## [Unreleased]

## [1.15.1] — July 28, 2026

### Security
- **Docker images no longer ship baked-in keys** — the Dockerfile set
  `SECRET_KEY` and `ENCRYPTION_KEY` as `ENV` for the build-time `collectstatic`
  step, and Docker `ENV` persists into the published image. Every image up to
  and including 1.15.0 therefore contained a publicly-known Django `SECRET_KEY`
  (session/cookie forgery) and a publicly-known constant Fernet
  `ENCRYPTION_KEY` (all "encrypted" secrets decryptable by anyone with the
  image) — and because the variables were never empty, the entrypoint's
  "required keys" check could never fire, so a deployment that set neither key
  booted and ran silently on the known values. The keys are now scoped to the
  single `RUN collectstatic` build step and never persist, the entrypoint
  rejects the two known build-time literal values in addition to empty (so a
  deployment still running on the leaked keys fails fast with instructions
  instead of starting), and the README quickstart now generates and passes both
  required keys. **If you deployed from the Docker Hub image without setting
  your own `SECRET_KEY`/`ENCRYPTION_KEY`, upgrade, generate fresh keys, and
  rotate any secrets stored in PyRunner.**

### Fixed
- **`docker run ... <command>` now runs the command** — the entrypoint ignored
  its arguments and always booted the full app, so the documented key-generation
  one-liners (`docker run --rm ... python -c "..."`) hung the terminal on a full
  server boot instead of printing a key. The entrypoint now `exec`s any passed
  command (before the key checks, so generating a key doesn't require one).

## [1.15.0] — July 15, 2026

### Added
- **External Secret Providers** — a secret's value can now be fetched live from
  an external secrets manager instead of stored locally. Configure a provider
  profile under Services → Secret Providers (HashiCorp Vault / OpenBao, AWS
  Secrets Manager, Infisical, Doppler, or any HTTP JSON endpoint), then point a
  secret at it with a reference like `kv/myapp#API_KEY`. The value is fetched at
  run start, cached in-process per a per-profile TTL (never in a shared cache, so
  plaintext is never persisted at rest), and injected **and masked** exactly like
  a local secret — so grants, workspace scoping, and the plugin SDK all keep
  working unchanged. Rotating a value in the provider is picked up automatically
  on the next run past the TTL. Resolution is fail-closed: a value that can't be
  fetched fails the run before it starts with a clear, named error (with an opt-in
  "serve last cached value" fallback for availability-first automation). Adding a
  new backend is one adapter file — no schema, form, or migration changes. Needs
  no new environment variables or dependencies; existing local secrets are
  untouched. Provider profiles and external-secret links are included in instance
  backups (format 1.6.0, relinked by name on restore); older backups still
  restore.
- **Databases** — managed PostgreSQL databases for scripts and plugins,
  complementing the key-value Data Stores with real SQL (tables, joins,
  transactions, the whole Python DB ecosystem). Attach a data server with one
  env var (`PYRUNNER_DATA_DB_URL` — works with a SQLite core, no migration);
  Owners/Admins provision databases from the console, each one a dedicated
  Postgres schema + login role so isolation is enforced by Postgres itself.
  Scripts connect through explicit per-script grants with the new
  `pyrunner_db` helper (`connect()` / `dsn()` / `sqlalchemy_url()`); plugins
  provision their own via the SDK's `DatabaseAPI` (API 2.2). Includes a
  read-only explorer — table browser with CSV export plus a Monitor page
  (live sessions, blocked/long-running queries, sizes, and slow-query history
  via `pg_stat_statements` when available) — and full backup/restore: the
  workspace backup (format 1.5.0) now carries database metadata, grants, and
  per-schema `pg_dump` data. `docker-compose.postgres.yml` sets the data
  server up automatically on first boot.
- **Pinned resource meters** — compact CPU / Mem / Disk mini-bars with live
  percentages on every cpanel page (desktop widths), polled every 30s, colored
  by the same 70/90% thresholds as the dashboard card, with a hover tooltip
  showing exact usage (and VPS totals when running in Docker). Clicking opens
  the dashboard.

### Changed
- Dashboard system resources are now container-aware: when PyRunner runs in
  Docker, the primary CPU and Memory numbers are the container's own usage
  (read from cgroups v2/v1, matching `docker stats` semantics), with the full
  host/VPS shown as a smaller secondary line. Memory is reported against the
  container's limit when one is set (labelled "limit"), otherwise against
  host RAM; CPU is normalized to the container's quota when set, otherwise to
  host cores. Non-Docker installs render exactly as before.

### Fixed
- Scheduled jobs now run in the schedule's configured timezone (they previously
  always fired in UTC); daylight-saving shifts are re-synced daily, and backup
  times honor the instance timezone too.
- "Pause all schedules" no longer permanently disables the built-in
  worker-heartbeat, update-check, backup, and cleanup schedules — they are
  re-ensured on resume.
- "Restart workers" now actually restarts the worker and reports failure
  honestly, instead of silently doing nothing and reporting success.
- The General Settings instance name and timezone are now applied everywhere —
  the header, page titles, email subjects, and datetime display; the two
  non-functional date-format/time-format fields were removed.
- Django's configured password validators (length, common-password, numeric,
  attribute-similarity) now run wherever a password is set, and changing your
  password no longer errors.
- Rate limiters use a fixed window that can't be starved by steady sub-limit
  traffic and is shared across workers (database/Redis cache), not per-process.
- Weekly and monthly schedules now appear in the dashboard's upcoming runs, are
  included in backups, and record edit history.
- Backups round-trip weekly/monthly schedule times, script tags, archived
  state, and sandbox isolation mode (backup format 1.4.0; older backups still
  restore).
- The datastore HTTP API used by scripts on Postgres/sandboxed runs now raises
  on server/auth errors instead of silently dropping writes; setup status, the
  queued-tasks list, and the database-size card work correctly on PostgreSQL
  (the size card shows "N/A" rather than a bogus 0 B on non-SQLite engines).
- The last-backup size can exceed 2 GB, and backup file listing is no longer
  capped at 1000 objects.
- The test-email action now requires an administrator; saving AI settings with a
  since-deleted provider shows an error instead of silently disabling AI, and a
  malformed provider id no longer errors.
- Fonts are self-hosted instead of loaded from the Google Fonts CDN; the README
  (invite-only auth, real scheduling modes) and `.env.example` (`DEBUG=False` by
  default) were corrected.
- A missing `SECRET_KEY` now raises a clear `ImproperlyConfigured` with the
  generate command instead of a bare `KeyError` (helps first-run source checkouts).
- Per-IP rate limits (webhooks, inbound channels) can key on the real client IP
  behind a reverse proxy: set `RATELIMIT_TRUSTED_PROXY_DEPTH` to the number of
  trusted proxy hops (default `0` keeps the previous `REMOTE_ADDR` behavior).

### Security
- AI in scripts now works with every configured provider, and third-party
  provider credentials (Z.AI/OpenRouter/custom, carried on
  `ANTHROPIC_AUTH_TOKEN`) are masked in run output.
- Webhook notification URLs are DNS-resolved and rejected when they resolve to
  internal/private addresses (SSRF), matching the S3 endpoint check.
- Plugin ZIP installation enforces its size limits against the actual extracted
  bytes and hardens path-traversal containment (`Path.is_relative_to`).

## [1.14.0] — July 15, 2026

### Added
- **AI Providers** — generic multi-provider AI integration. Saved provider
  profiles (Anthropic Claude, Z.AI GLM, OpenRouter, local Ollama, or any
  Anthropic-compatible endpoint), each with its own encrypted credential and
  default model; switch the active provider with one click. Existing Claude
  configurations migrate automatically (migration 0043). Connection tests are
  provider-aware: Anthropic runs a real web search, other providers run an MCP
  tool-call round-trip that validates the model's tool calling. Usage rows are
  attributed to the serving provider.
- **Channels** — messaging subsystem (Telegram first). Outbound messages from
  scripts via the `pyrunner_notify` helper and `notify_channels` routing;
  inbound webhook with a deny-by-default approval inbox, per-sender rate
  limits, and a dispatch worker (migrations 0039–0040).
- **Py AI** — a read-only in-app assistant that answers questions about this
  instance (scripts, runs, schedules, datastores) via in-process MCP tools.
  Available as a dashboard chat and as a Telegram channel handler
  (migration 0041).
- **Brand Tracker example plugin** — weekly brand-mention tracking (Serper
  web/news, Hacker News, Reddit) with AI sentiment analysis and credit caps.
- Whole-app Beta badge system driven by a single `IS_BETA` flag.
- Shared cache backend: `DatabaseCache` by default (no extra service), with an
  opt-in `REDIS_URL` to use Redis. Fixes rate-limiting and webhook dedup that
  previously used a per-process cache and were not shared across workers.
- `HEALTHCHECK` in the `Dockerfile` (mirrors docker-compose) so `docker run`
  users also get container health reporting.
- Brotli-compressed static assets via WhiteNoise (`.br` in addition to gzip).
- CI: GitHub Actions (tests + Ruff), pre-commit hooks, Dependabot, editorconfig,
  and open-source community files.

### Changed
- Expanded `.env.example` to document the previously-undocumented settings
  (HSTS, DB connection age, gunicorn/PORT, login/API rate limits, per-run
  resource limits).
- README: the Claude AI section is now "AI in your scripts" with a provider
  table and per-provider setup notes.

### Security
- Removed passwordless magic-link login; fixed a channels XSS.
- Gated global-settings handlers, environment/package management, and
  application-log reads on superuser.
- Reject pip option lines in bulk requirements install.
- Added a scoped Content-Security-Policy header (defense-in-depth).

## [1.13.0]

Headline capabilities available in this release:

- **Script management** — create, edit, schedule, and monitor Python scripts.
- **Environments** — isolated per-script virtualenvs with custom pip packages.
- **Secrets** — encrypted environment variables and secrets (Fernet).
- **Claude AI for scripts** — call Claude from scripts (`pyrunner_ai`) using a
  Claude subscription token or an Anthropic API key.
- **Channels** — outbound + inbound messaging (Telegram) with an approval inbox.
- **Py AI** — a read-only in-app Claude assistant over your scripts/runs.
- **Plugins** — installable, self-provisioning Django-app plugins with an SDK.
- **Sandbox & isolation** — opt-in bubblewrap/rlimit isolation for runs.
- **Tenancy** — workspaces with scoped resources and RBAC.
- **Postgres support** — opt-in via `DATABASE_URL`; SQLite stays the default.
- **Notifications** — email, webhook, and Telegram alerts on completion/failure.
