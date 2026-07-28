# YouTube Comments AI — plugin guide (for humans & AI agents)

Context for anyone (or any AI) working **on** this plugin. It is a
**self-provisioning** PyRunner plugin (SDK `core.plugins.api`): one form
**Save** creates and keeps in sync a managed Script, its secrets, a managed
Postgres **Database** (the first DB-native example plugin), a state DataStore
and a daily Schedule. It is the reference for a **database-backed** plugin and
consumes three platform seams: Databases (`DatabaseAPI`/`pyrunner_db`), AI
Providers (`pyrunner_ai`) and instance email (`pyrunner_notify.email(html=…)`).
Stage 3 adds **reply automation**: a Google OAuth connect flow (`oauth.py`),
a Reply Brain, per-tag reply policies with hard auto-publish guardrails, a
Reply Queue, and optional spam moderation. Stage 4 adds the **weekly insights
email** (AI-clustered questions → FAQ/content ideas + per-video sentiment
trend). Stage 5 adds **publish-ready testimonials**: a focused AI grading pass
(feature/solid/generic + why) and **avatar archiving** to the instance's
object storage (`pyrunner_storage`/`StorageAPI`, SDK 2.5).

Read this before editing — most bugs you can introduce here are silent
cross-layer desyncs, not syntax errors.

## Architecture: two execution contexts

| Runs in the **web process** | Runs in the **environment venv** (a Script) |
|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | `worker_body.py` (the managed analyzer script) |
| `provisioning.py` — all SDK calls + YouTube channel resolution at Save | — |
| `oauth.py` — Google consent flow, token exchange/refresh, immediate posting from the Reply Queue | (the worker has its own minimal copies — it can't import web modules) |

The web layer talks to PyRunner **only** through `core.plugins.api`; the
dashboard reads the plugin's own tables via `DatabaseAPI(OWNER).dsn(DB_KEY)`
+ psycopg (`provisioning.db_rows`). The worker is a standalone script that
imports only stdlib + `requests` + injected helpers (`pyrunner_db`,
`pyrunner_datastore`, `pyrunner_ai`, `pyrunner_notify`).

## Contracts you must not break

1. **Secret/config names are a cross-process contract by convention.**
   `forms.SECRET_FIELDS` env keys + `provisioning.OAUTH_TOKEN_KEY` +
   `provisioning.CONFIG_KEYS` + `provisioning.BRAIN_KEYS` must all appear
   literally in `worker_body.py`; `provisioning._worker_code()` fails **loudly
   at Save** if one goes missing. Rename on both sides or not at all.
2. **Import-light / SDK-only.** `views.py`/`provisioning.py` never
   `import core.models|services|tasks`. psycopg is imported lazily inside
   functions.
3. **No `models.py`, no `migrations/`.** SQL DDL lives ONLY in the worker's
   `SCHEMA_SQL` (`CREATE TABLE IF NOT EXISTS`, additive). The web layer must
   tolerate missing tables (`views._dashboard_data` maps `UndefinedTable` →
   the "worker hasn't run" empty state).
4. **Postgres is REQUIRED.** `provision()` fails closed with a friendly
   message when `DatabaseAPI.is_available()` is False. Don't add a DataStore
   fallback — a locked product decision (2026-07-15).
5. **Ownership + idempotency.** Everything owned by slug `yt_comments`; SDK
   upserts key on `(owner_plugin, owner_key)`; re-running `provision()` must
   never create duplicates (covered by `ProvisionTests`).
6. **Secrets are write-only in the form.** Blank = keep existing.

## Engine invariants (the bug-prone parts)

- **The watermark is the correctness core.** One channel-wide sweep
  (`commentThreads.list` + `allThreadsRelatedToChannelId`, `order=time`,
  newest first) pages down to `iso_floor(watermark, start_date)`; the
  watermark **only advances after a COMPLETE sweep** (floor reached or history
  exhausted). Quota/network abort mid-sweep ⇒ watermark stays ⇒ next run
  re-fetches (upserts dedupe). A page-cap stop counts as complete (bounded
  backfill, loudly logged). Persisting is page-by-page with per-page commits.
- **Upserts never clobber analysis.** `UPSERT_COMMENT_SQL` refreshes
  text/likes/`avatar_url` only (fetch-data); `status`/`tags`/`sentiment`/
  `testimonial_grade` are written exclusively by the AI passes.
- **A classification failure never mislabels.** Parse failure / model skip /
  over-cap ⇒ the row stays `pending_analysis` and is retried next run. There
  is NO default tag (the prototype's "default to positive" bug).
- **Grading follows the same rule: ungraded ≠ generic.** A failed grading
  batch leaves `testimonial_grade` NULL and is retried — grading must never
  quietly downgrade a testimonial. Grades are a closed set
  (`TESTIMONIAL_GRADES`: feature/solid/generic), shared by string with
  `views._GRADE_BADGE`.
- **Avatars: stable keys, testimonial authors only, fail-soft.** The storage
  key is ALWAYS `avatars/<channel_id>.jpg` (overwritten on change — published
  hot-links must never break; the stored Content-Type wins over the
  extension). `comments.avatar_url` semantics: NULL = never captured
  (backfill target, `comments.list` 1 unit/50 ids), '' = unavailable — don't
  collapse the two or the backfill loops forever. The `authors` row is only
  written on a successful sync (that's the retry mechanism). Any avatar/
  storage failure is a log line, never a failed run.
- **The web layer tolerates missing 0.5.0 columns** (upgraded install before
  the worker's first run): `views._testimonial_rows` falls back to the legacy
  SELECT on UndefinedColumn/UndefinedTable so the tab never blanks.
- **Owner comments are `skipped_owner`** (never analyzed, never alerted) —
  they'll matter for reply automation later, so they ARE stored.
- **Statuses are a closed set**: `pending_analysis` / `analyzed` /
  `skipped_owner`. The inbox filter UI and worker share them by string.
- **Reserved tags** (`urgent`, `testimonial`, `question`, `spam`) carry
  behavior; `forms.parse_tags` silently restores them if the user deletes one.
- **Alerts are batched and seed-gated.** One urgent + one testimonial email
  per RUN (not per comment), digest optional, ALL suppressed on the first
  (seed) run (`stats.seeded`). Comment text is `html.escape`d everywhere it
  enters email HTML.
- **Weekly insights are gated by `should_send_insights`** (configured weekday,
  never twice in 2 days, ≥8-day catch-up that snaps back to the weekday) and
  seed-gated like every email. `stats.insights_last` is stamped only when the
  week was HANDLED (sent, or an empty week deliberately skipped) — a failed
  SEND stays unstamped so the gate retries it. Clustering junk/AI-failure ⇒
  the section is omitted, never a crashed run; question text is fenced as
  untrusted data in the prompt and escaped in the email.
- **Prompt-injection defense**: comment text is fenced in `<comment>` blocks
  and the system prompt declares it untrusted data. Keep both if you touch
  the prompt.
- **Quota**: every `yt_get` counts a unit into `QUOTA_USED` (`yt_post` counts
  50); a 403 with a quota reason raises `QuotaExhausted` → clean stop,
  watermark kept.

## Reply-engine invariants (Stage 3 — the reputational-risk parts)

- **The seed run never drafts, posts or moderates.** A backfill triggering
  actions is the nightmare scenario — keep the `is_first_run` gate.
- **`urgent`/`spam` can never be `auto`.** Enforced three times on purpose:
  form choices, `forms.clean()`, `provisioning._clean_policies()` (raises).
- **`effective_reply_mode` is conservative**: any tag saying `draft` beats
  `auto`; urgent/spam demote auto→draft; all-off ⇒ no reply.
- **`auto_gate` rejections DEMOTE, never post**: URL-not-in-Brain-knowledge,
  link-bearing comment, over-length, refusal/meta text (+ the age check and
  daily cap in the callers). Demoted rows land `pending_approval` with a
  `guard_note` — never silently dropped.
- **One reply per comment, EVER** — `replies.comment_id UNIQUE` + `ON
  CONFLICT DO NOTHING`. Don't "fix" a failed reply by inserting a second row.
- **Reply statuses are a closed set**: `pending_approval` / `approved` /
  `posted` / `rejected` / `failed` — shared by string with `views.py`
  (`_REPLY_BADGE`) and the worker. Transient post failures stay `approved`
  (retried); only `PostRejected` (4xx) marks `failed`.
- **Posting/moderation need OAuth; reads never do.** Reads stay on the API
  key deliberately (stable, same quota) — don't switch the sweep to OAuth.
  An `invalid_grant` must degrade: record via `record_oauth_status`, skip the
  posting phase, keep monitoring. The page then shows Reconnect.
- **The Brain lives in store entry `brain`** (own save endpoint) so a
  Settings re-save can't clobber it; the `oauth` entry is read-modify-write
  from both processes (web writes connect metadata, worker writes health) —
  never replace the whole entry blindly.
- **The web layer must tolerate a missing `replies` table** (upgraded install
  before the worker's first run): `views._reply_data()` guards separately
  from the comment queries so the Inbox never blanks.

## Keep the manifest truthful

`plugin.json` must match the code: `api: "2.5"` / `min_pyrunner: "1.16.0"`
(ChannelAPI + DatabaseAPI + StorageAPI/`pyrunner_storage`); `provisions` =
1 script / 4 secrets (`YT_API_KEY`,
`YT_OAUTH_CLIENT_ID`, `YT_OAUTH_CLIENT_SECRET`, `YT_OAUTH_REFRESH_TOKEN` —
the last one written by the Connect flow, not the form) / 1 datastore /
1 database / 1 schedule. Version bumps touch **three places**: `plugin.json`,
`apps.py PyRunnerPlugin(version=…)`, `CHANGELOG.md`.

## Develop · validate · package

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/yt_comments
python manage.py runserver

python manage.py plugin_doctor --path examples/yt_comments   # must be 0-fail
python manage.py test core.test_yt_comments_plugin

cd examples && zip -r yt_comments.zip yt_comments -x '*/__pycache__/*'
```

Tests are developed in-tree (shim: `core/test_yt_comments_plugin.py`). The
worker is import-safe (`load_plugin_config` guards the store read), so its
pure helpers are unit-tested directly; the networked fetch/classify paths and
the real Postgres schema are verified by real runs, not unit-mocked.

## Don't

- Add `models.py` / `migrations/`, or import core internals from the web layer.
- Hardcode the store/db names (derive from `PYRUNNER_OWNER_PLUGIN`).
- Advance the watermark on an aborted sweep, or write `tags`/`status` from
  the fetch path.
- Give classification failures a default tag.
- Re-propose a DataStore fallback for storage (decision: Postgres required).
- Let `plugin.json` drift from the code.
- Offer `auto` for urgent/spam, post around the guardrail gate, or let
  anything reply-related run on the seed run.
- Weaken `ON CONFLICT (comment_id) DO NOTHING` on replies — it IS the
  never-reply-twice guarantee.
