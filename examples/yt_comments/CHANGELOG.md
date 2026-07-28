# Changelog — YouTube Comments AI

## 0.5.0 (unreleased)

Stage 5 — publish-ready testimonials (grading + archived avatars):

- **Publish-worthiness grading**: every testimonial-tagged comment gets a
  second, focused AI pass — **⭐ Feature** (stands alone as social proof: a
  specific outcome, story or concrete details), **Solid** (substantive but no
  story) or **Generic** ("great video"). Each grade carries a one-line why.
  Ungraded ≠ generic: a failed batch stays ungraded and is retried — grading
  never quietly downgrades. Existing testimonials are graded automatically on
  the first 0.5.0 run.
- **Avatar archiving** (opt-out, Settings → Testimonial publishing): each
  testimonial author's profile picture is downloaded and stored in the
  instance's object storage (Services → Object Storage, SDK `pyrunner_storage`)
  under the STABLE key `avatars/<channel_id>.jpg` — overwritten when the
  author changes their picture, so a published page hot-linking it never
  breaks and updates by itself. With a public base URL on the assets
  connection the links are permanent; otherwise they are presigned and expire
  (the tab says so). Fail-soft: no storage connection just logs and skips.
- Legacy comments (fetched before 0.5.0) get their avatar URL backfilled via
  `comments.list` (1 quota unit / 50 comments, converges to a no-op); new
  fetches capture it for free.
- Testimonials tab: avatars, grade badges + one-line AI reason, grade filter
  chips with counts, best-first ordering. Exports gain `grade`, `why` and
  `avatar_url` columns (CSV) / grade + avatar links (Markdown) and honor the
  active grade filter.
- New `authors` table + `comments.avatar_url` / `testimonial_grade` /
  `testimonial_note` (additive, auto-migrated); new run phases `grading` and
  `avatars`; new config key `avatar_archive` (default on).
- Requires PyRunner 1.16.0 (`api: 2.5` — StorageAPI/`pyrunner_storage`).

## 0.4.0 (unreleased)

Stage 4 — weekly insights + ship polish:

- **Weekly insights email** (on by default, own toggle + weekday picker under
  Settings → Email alerts): the week's `question`-tagged comments are
  clustered by the AI into *most-asked questions*, each labeled **FAQ** (with
  a suggested answer drawn only from the comments themselves) or **VIDEO
  IDEA** (title + angle), plus a per-video sentiment trend vs the prior week
  and headline counts. Sections degrade independently — no AI provider means
  no clusters (the email says so), an empty week sends nothing — and the
  seed run never sends it.
- Robust weekly gate: fires with the configured weekday's run, never twice a
  day, catches up ≥8 days after a missed/failed send and then snaps back to
  the configured weekday. A failed send is retried, not lost.
- New config keys `insights_enabled` / `insights_weekday` (existing installs:
  on, Monday — re-save Settings to change); new run phase `insights` in the
  live progress banner.

## 0.3.0 (unreleased)

Stage 3 — reply automation (OAuth):

- **YouTube OAuth connect flow**: bring a Google OAuth *Web application*
  client (ID + secret as write-only secrets), Connect runs the consent
  round-trip and stores the refresh token (`YT_OAUTH_REFRESH_TOKEN`) as an
  owner secret granted to the analyzer. Setup card documents the redirect URI
  and the "publish the app to **Production**" gotcha (Testing tokens die in
  7 days). `invalid_grant` anywhere → the page shows **Reconnect**; approved
  replies wait, monitoring keeps running on the API key.
- **Reply Brain** (own tab): voice / knowledge / rules + optional per-tag
  guidance, injected into every drafting prompt. Knowledge links are the ONLY
  URLs an auto-published reply may contain.
- **Per-tag reply policy**: off / draft-for-approval / auto-publish per tag;
  `urgent` and `spam` can never be auto (locked, enforced form- and
  server-side).
- **Reply Queue tab**: approve (with edits — posts immediately via
  `comments.insert`), reject, guard notes, full audit trail with links to the
  posted replies. One reply per comment, ever (DB UNIQUE).
- **Auto-publish guardrails**: seed-run suppression, untrusted-comment
  fencing, no URLs outside the Brain, never reply to link-bearing comments,
  length + refusal-text gate, 14-day age limit, daily auto-post cap — every
  rejection demotes to the approval queue with the reason, never posts.
- **Optional spam moderation**: AI-tagged spam sent to held-for-review via
  `comments.setModerationStatus` (opt-in, ≤20/run, audited via
  `moderated_at`, never on the seed run).
- New `replies` table + `comments.moderated_at` (additive, auto-migrated by
  the worker); run history and digests now count drafted/posted replies.

## 0.2.0 (unreleased)

Stage 2 — channel alerts (transport-agnostic):

- Urgent + testimonial alerts (and optionally the digest summary) can also go
  to a messaging Channel, picked by name from the instance's Channels via the
  read-only `ChannelAPI` (SDK 2.3). Email and channel toggles are independent
  — use either or both; swapping Telegram for another provider later needs no
  plugin change.
- Compact plain-text chat formatting (5 items + deep links, "…and N more"),
  one message per run per kind, seed-run suppression, degrade-to-log on send
  failure.
- Requires PyRunner 1.16.0 (`api: 2.3`).

## 0.1.0 (unreleased)

Stage 1 — comment intelligence (read pipeline):

- Self-provisioning: one Save creates the managed analyzer Script, the
  `YT_API_KEY` secret, a managed Postgres database (`yt_comments:data`), a
  state DataStore and a daily Schedule.
- Efficient channel-wide fetch: one `commentThreads.list`
  (`allThreadsRelatedToChannelId`) sweep down to a watermark — typically 1–3
  quota units per run instead of paging every video. Replies included.
- Batched AI classification via the platform AI Provider (`pyrunner_ai`):
  user-editable tag taxonomy + sentiment + reasoning; failures stay
  `pending_analysis` and are retried next run (never mislabeled).
- Dashboard: filterable comment inbox (tag/video/status + text search),
  stat tiles, run history, live run banner with cancel.
- Testimonial collector with Markdown/CSV export.
- Batched urgent + testimonial alert emails (one per run) and a daily digest
  via the instance email backend — no Resend key needed. Seed run suppressed.
