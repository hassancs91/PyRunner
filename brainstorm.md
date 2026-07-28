- [ ]  Add Donate button
- [ ]  Add Stop Running Script + API
- [ ]  **Worker pools** — Dedicated workers for heavy scripts or agents
- [ ]  **Multiple workers** — Scale execution capacity - check if we scale with another dedicated container for Q2
- [ ]  requirements bulk install stuck
- [ ]  **Concurrency control** — Prevent same script running twice simultaneously
- [ ]  Add CRON Schedule
- [ ]  Environment variables per script — Beyond global secrets
- [ ]  Login Security
- [ ]  Multiple schedules per script — Run at 9am AND 6pm
- [ ]  **Retry on failure** Optional — Auto-retry X times with delay - **Retry with backoff** — Exponential backoff (1s, 2s, 4s, 8s...)
- [ ]  Verify Production: https://claude.ai/code/artifact/bb2859bf-88d6-4776-9cdb-5cfe7e79b473
- [ ]  Py AI Orchestrator Agent that knows everything about the plugins, scripts, data, and you can talk to + multle agent structure
- [ ]  AI Agents - sub agetns from Py AI Core
- [ ]  Prompts Library
- [ ]  Connect with vector DB
- [ ]  Maintenance windows — Pause all scripts during defined periods
- [ ]  Skills:
    - [ ]  Create Script
    - [ ]  Create Plugin
    - [ ]  Skill to Analyze a script for security
    - [ ]  Skill to Analyze a plugin for security
- [ ]  Website and Documentation - Enrich the docs ( password reset …)
- [ ]  Execution time tracking — Graph duration over time
- [ ]  Notification Channels
- [ ]  **2FA** — Two-factor authentication
- [ ]  **Dry run mode** — Validate script without executing - **Test mode** — Run with test data
- [ ]  Better Data Store explorer and editor
- [ ]  Input form — Define parameters, show form before manual run
- [ ]  SMS — Via Twilio
- [ ]  better Webhook and API System
- [ ]  **Script forking** — Duplicate a script with one click
- [ ]  **Script versioning** — View history, diff changes, rollback to previous versions
- [ ]  Create Watcher Trigger:
    - [ ]  Email
    - [ ]  File Change
    - [ ]  Web hook
    - [ ]  Database trigger
    - [ ]  Google Sheets
    - [ ]  More Intgrations

## Think about how to plan and fit:

- Live run output streaming

Pokee streams workflow progress in real time via Server-Sent Events. Your run detail page does a full-page `<meta http-equiv="refresh">` every 5 seconds (detail.html:7). A live log tail — SSE or an htmx/fetch polling partial that appends stdout as it arrives — is a modest lift with an outsized perceived-quality win; it's the difference between "watching a page blink" and "watching your script run." An SSE endpoint on the API side would also pair with #3.

- Synchronous webhook responses (script-as-API-function)

Pokee lets a workflow be deployed as an API endpoint that *returns the result*. Your webhook is fire-and-forget — it returns `{"status": "queued", "run_id": ...}` and the caller never sees the output (webhooks.py:95-99). Adding an opt-in `?sync=1` mode (wait for completion up to a timeout, return stdout/exit status) turns any PyRunner script into a lightweight API function that Zapier/Make/n8n or another server can call and consume directly. Cheap to build on what exists, and it makes PyRunner composable with the rest of a user's stack instead of only a destination.

- **AI provider fallback chains — best fit, lowest effort.**

When a model call fails (429 after retries, 5xx, auth errors immediately), Hermes automatically retries the request on a configured fallback provider+model list, restoring the primary on the next call. You already have the perfect seam for this: your AI Providers profiles. And you've already felt the pain this solves — glm-5.2 timing out during YouTube Scout triage (you worked around it with `TRIAGE_CALL_TIMEOUT=600`). A per-profile "fallback profile" FK plus failover-on-error in the provider call path would make every AI-consuming plugin (Brand Tracker, YouTube Scout, YT Comments, Py AI) more reliable with zero changes to the plugins themselves. Hermes also allows separate fallback chains per task type (vision vs. main vs. compression) — for you the per-profile version is probably enough.

- **Durable delivery ledger for channel sends.**

outbound messages are recorded and retried so a Telegram hiccup doesn't silently swallow a notification. Worth checking your `pyrunner_notify` / channel-send path: if a Telegram API call fails mid-run today, is that recorded and retried, or just logged? For a platform whose pitch includes "your script failed and you got told," at-least-once delivery with a visible sent/failed status is a credibility feature.

- Small UX nicety

Natural-language schedule entry ("every weekday at 9am" → cron) on the schedule form. You already have AI providers wired in, so this is a cheap delight feature — cron syntax is a known newbie wall, and your sandbox plan explicitly says your users include newbies.

- 
- .Script inputs + auto-generated run forms (their single best idea) — HIGH

Windmill's signature move: script parameters become a form, a webhook payload schema, and an API contract automatically. PyRunner scripts today take **no runtime inputs** — I confirmed `Script` and `Run` have no params/inputs fields. This is the one Windmill idea that transforms a script runner without cloning anything:

- Define inputs per script (a simple declared-fields list in the script sidebar, or parse a `# /// pyrunner-inputs` comment block).
- "Run now" renders a form; values injected as env vars or via a `pyrunner_inputs` helper.
- Webhook POST body maps to the same inputs; inputs are stored on the `Run` (which also gives you **re-run with same inputs** almost for free — another Windmill feature). This also upgrades every plugin: plugin-provisioned scripts could declare inputs instead of round-tripping through config DataStores. Size: M.

- Structured run results — HIGH

Windmill scripts *return* values; runs have results, not just logs. PyRunner runs capture stdout only. Add `pyrunner.set_result(dict)` (loopback helper, same pattern as `pyrunner_notify`) stored as JSON on the `Run`, shown in run detail, returned by the API. Combined with #1, a script becomes a callable endpoint: input → run → result. This is also the prerequisite for any future chaining and for your Agents plan (an agent reading a run's *result* beats parsing its stdout). Size: S–M.

- Per-script concurrency policy — HIGH

Windmill has concurrency limits and job debouncing. PyRunner has no overlap guard — a scheduled script whose previous run is still going will happily stack, and your plugins already hand-roll in-flight checks (the comment at run.py:176 acknowledges this). A simple per-script choice — "allow overlap / skip if running / queue behind" — is a small field plus a check in the atomic claim you just built for the run-lifecycle fix, so this is a natural companion to that uncommitted work. Size: S.

- Sync webhook mode ("script as API") — MEDIUM

Windmill webhooks come in async (job id) and sync (wait, return result) flavors. Once #2 exists, add ?sync=1 to your existing webhook endpoint with a sensible timeout. Turns PyRunner into a personal API backend — very on-brand for automation creators. Size: S (after #2).

- Smoother dependency UX — MEDIUM

Windmill auto-resolves imports to lockfiles and supports PEP-723 inline metadata. Your Environments/packages system is solid but manual; the everyday papercut is `ModuleNotFoundError` after pasting a script. A PyRunner-native fix: detect `ModuleNotFoundError` on a failed run and surface an "Install `X` into this environment" button on the run detail page. No lockfile machinery needed. Size: S.

- Per-run resource metrics — MEDIUM
Windmill made per-job memory tracking a free-tier feature. You track the run pid and have rlimits + the container stats dashboard, but nothing per-run. Sampling peak RSS during execution and storing it on the Run would let users actually tune their sandbox memory limits instead of guessing. Size: S–M.

- Full-text run/log search + token expiry notices — LOW, quality-of-life

Search across run output (yours is filter-only today), and an email nudge before an API token expires. Both small; grab them opportunistically.

- Lateness / stuck-run alerts (quick win)
Prefect automations do proactive detection: "flow stayed running > 30 minutes," "run is late," metric thresholds. PyRunner alerts on failures, but not on the silent failure class — a schedule that stopped firing, or a script that hangs. Two per-script thresholds ("alert if no successful run in N hours," "alert if a run exceeds N minutes") checked in the same minute-loop worker heartbeat that already drives Run.reconcile_stale, delivered through notify_channels, covers what Prefect needs a whole automation system for. Very high value-to-effort for an automation platform whose users don't watch the dashboard.

- Run artifacts — rich script outputs (highest product value)

Prefect's artifacts (markdown, tables, links, progress, images attached to runs, with per-key history) are the feature I'd steal most enthusiastically. PyRunner runs currently produce stdout/stderr, and your users — AI-automation builders — end up making their scripts print tables into logs. A pyrunner_report helper following your existing loopback pattern (like pyrunner_notify / pyrunner_storage) letting a script attach a markdown/table/link result to its Run, rendered on the run detail page with "latest result" surfaced on the script page, would immediately upgrade every example plugin: Brand Tracker posts its sentiment table, YouTube Scout posts its idea list, instead of burying them in logs. It also gives plugins a free dashboard primitive.

- Cron expression mode + free-form intervals

Prefect supports cron, arbitrary intervals, RRule, and multiple schedules per deployment, all timezone-aware. PyRunner's schedule.py has manual/interval/daily/weekly/monthly, and intervals are a fixed choice list (5 min … 12 h). Two gaps users will actually hit: no way to express "9am on weekdays" (cron string mode for power users) and no interval outside the presets. You already solved the hard part — the DST-resync daily task in tasks.py — so a cron mode can reuse that machinery. Skip RRule; that's calendar-logic overkill for your audience.

## Ideas to fold into existing plans (no new work now)

- **Agent memory + skill distillation → your Agents plan.** Hermes's signature loop: persistent curated memory (`MEMORY.md`/`USER.md`) per agent, plus a `/learn` command that distills a solved problem into a reusable skill document. When you build the Agents plan (docs/PLAN_agents.md), per-agent persistent memory should be in the schema from day one, and "skills" could ride your already-built Script Libraries seam rather than a new mechanism.
- **WhatsApp: use the Business Cloud API, not an unofficial bridge.** Hermes shipped an official WhatsApp Business Cloud adapter in v0.17 and then had to cut an emergency patch (v0.18.2) for its unofficial Baileys dependency. That's real-world confirmation of your instinct to defer WhatsApp, and a clear pointer for which adapter to write when you pick it up. Their channel roster (Telegram, Discord, Slack, WhatsApp, Signal, Email, iMessage) also validates your Slack/Discord-next ordering.
- **Browser automation demand.** Hermes ships browser automation with multiple backends (local CDP browsers + cloud). Your Browser Sessions plan is already locked and better suited to your model (persistent profiles + noVNC live view). No design change needed — just confirmation the feature is table stakes in this space and worth its v1.16.0 slot.

## Advanced:

- **Second Brain: Agent memory - commong between agents or plugins, multiple Brains if needed.**
- MCP Access
- PyRun Bot action, like stop script and other functions with auto approval gate
- Chrome Open if need to login or so inside the UI as a browser - same session and cookies for automation. - https://claude.ai/chat/21c44de4-9fa9-4ebd-9bc2-433c1cd3ef51
- **OAuth support** — Connect accounts securely - **Generic OAuth2 connection seam:**
    
    maintaining per-service integrations solo is a trap. But a *generic* OAuth2 connection row, following your established seam pattern (AIProvider, StorageConnection, SecretProvider): client ID/secret + auth/token URLs + scopes, console handles the redirect dance and token refresh, scripts/plugins get a live access token via a `pyrunner_*` helper. Your own plugins are the evidence of demand — gmail_agent and yt_comments each had to hand-roll OAuth token plumbing. One seam removes the single most painful part of writing automation scripts against Google/YouTube/etc., without you owning any specific integration.
    
    **Smaller mentions:** a simple audit trail (who ran/edited/deleted what) would strengthen the workspace story if PyRunner ever hosts small teams — low priority; and an in-app script-template gallery is a lighter cousin of your already-planned plugins catalogue.
    
- Git integration — Pull scripts from repo
- PyRunner Mobile app
    - Push notifications
    - Talk With Agent
- PyRunner Enterprise - PyRun.AI
- Push notifications
- Internal RAG
- Script Generator
- Artifact generator from data
- Observability
    - Log search — Full-text search across all run logs - **Log highlighting** — Color errors red, warnings yellow
    - Add Python library to track and output and manage progres, auto installed, and can be used in python scripts - reflects in UI.
        
        Activity log — Who did what, when - user action audit for teams
        
    - Debug mode — Extra verbose logging
    - Alerting
- API System
- Think how to make this powered by users - marketplace …
- integrate with computer use
- More Integrations:
    - Google Sheets
    - Notion
    - Airtable