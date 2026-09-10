# Brand Tracker (PyRun plugin)

Track mentions of your **brand or keywords** across the web and communities on a
**weekly** schedule — configured once in a form. Like
[`qdrant_backup`](../qdrant_backup/), it is **self-provisioning**: one **Save**
creates and keeps in sync a managed Script, its secrets, a data store, and a
weekly schedule, using the v2 plugin SDK (`core.plugins.api`).

It is the first example plugin that consumes the **Claude AI integration**
(optional sentiment enrichment), and a reference for building a content-feed
plugin on the Plugin Platform v2.

## What it does

- **Form → provision.** Enter keywords, your Serper key, pick sources + a weekly
  day/time and an environment. Saving provisions (idempotently):
  - a managed **Script** `Brand Tracker` (owner `brand_tracker`, `injection_mode=selected`),
  - **owner-scoped Secrets** — `SERPER_API_KEY` (required), and only when used
    `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`, `OPENROUTER_API_KEY`,
    `RESEND_API_KEY` — granted only to that script (clean env names),
  - an owned **DataStore** `brand_tracker:state` (entry `config` = settings;
    `seen` = dedup index, `mentions` = the feed, `stats`/`credits`/`runs` =
    dashboard data),
  - a **weekly Schedule**.
- **Run now** queues a tracked Run through the normal RunBackend.
- **Dashboard** — a live **mention feed** (browse by keyword/date, filter by
  source), stat cards, a **Serper-credit** gauge, and run history.
- **Operational alerts** use PyRun's built-in notifications (the managed
  script's `notify_on` + `notify_email`) — separate from the optional content
  email below.

## Sources & credits

The tracker searches each keyword as an **exact phrase** over the last week and
**dedupes across sources** (the same article via web and news collapses to one):

| Source | Cost | Notes |
|---|---|---|
| **Serper web** (Google organic) | Serper credits | always on |
| **Serper news** (Google News) | Serper credits | toggle |
| **Hacker News** (Algolia) | free | toggle |
| **Reddit** (OAuth search) | free | toggle — needs a Reddit "script" app's Client ID/Secret |

Only Serper web + news cost credits. A request is **1 Serper credit** at the
default ≤10 results, **2** above that. With weekly runs, the cost is
`keywords × (web + news) × (1 or 2)` per week — the Settings form shows a live
estimate, and an optional **monthly credit cap** pauses Serper for the month
when reached (free sources keep running).

> **Scope:** this tracks the *indexed web + open communities*. Walled social
> (X/Twitter, Instagram, TikTok, LinkedIn) is **not** reachable via search APIs
> and is out of scope — the source layer is pluggable if you add a paid provider
> later.

## AI enrichment (optional)

Tag each mention with a **source type** (news/blog/forum/social/docs/other) and
**sentiment** (positive/neutral/negative). Pick a provider in the form:

- **Off** (default) — feed shows mentions without tags.
- **Claude** — uses your PyRun **Claude AI** integration (Services → Claude
  AI); needs `claude-agent-sdk` in the environment. No extra key.
- **OpenRouter** — your own `OPENROUTER_API_KEY` + a model (e.g.
  `openai/gpt-4o-mini`); a single-shot HTTP call.

Enrichment runs **only on new mentions**, batched (≤25/call) with a per-run
ceiling (100), and **degrades silently** — if the provider is unavailable or a
batch fails, mentions are stored without tags and the run still succeeds.

## Email report (optional)

By default the dashboard feed *is* the product (zero email setup). Optionally,
tick **Email me a report** and add a Resend key + a verified from-address to get
an HTML report of new mentions each run. The **first run never emails** — it
seeds the feed silently so a fresh install doesn't flood you.

## Install

1. **Environment** — under *Environments*, create/choose one that installs
   `requests` (add `claude-agent-sdk` too if you want Claude enrichment).
2. **Upload** `brand_tracker.zip` (Plugins → Upload), **Activate** (runs the
   doctor + isolated preflight), then **Restart**.
3. Open **Brand Tracker** in the sidebar (superuser only), add keywords + your
   Serper key, **Save**, then **Run now** (or let the weekly schedule run it).

## Develop locally (dev mode)

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/brand_tracker
python manage.py runserver
```

Validate anytime without uploading:

```bash
python manage.py plugin_doctor --path examples/brand_tracker
```

## Package the zip

From the repo root:

```bash
cd examples
zip -r brand_tracker.zip brand_tracker -x '*/__pycache__/*'
```

The archive must contain a single top-level folder (`brand_tracker/`) — the slug,
matching `plugin.json` and the `apps.py` descriptor.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Save warns "Environment … may be missing 'requests'" | The chosen environment lacks the worker's package. Add `requests` under *Environments*, or runs fail. |
| **Test Serper key** → "Authentication failed" | Wrong `SERPER_API_KEY`. "Couldn't reach Serper" → network/firewall. |
| Reddit returns nothing | Reddit needs a *script* app's Client ID + Secret (free at reddit.com/prefs/apps); without them Reddit is skipped. |
| Feed empty after the first run | The first run **seeds silently** (no email). New mentions appear from the second run on — or click **Run now** again later. |
| Mentions have no sentiment tags | Enrichment is Off, or the provider is unavailable (Claude not enabled / `claude-agent-sdk` missing, or no OpenRouter key). It degrades safely. |
| Run history shows a **cap** flag | The monthly Serper credit cap was reached; Serper searches paused for the month, free sources still ran. |
| Schedule never runs | Check the next-run chip in the header and that PyRun's scheduler isn't globally paused. |

## Tests

```bash
python manage.py test core.test_brand_tracker_plugin
```

The tests live in [`tests.py`](tests.py) and run via a thin shim in
`core/test_brand_tracker_plugin.py`. They cover canonical-URL dedup, proper
domain exclusion, retention pruning, the AI-enrichment provider/parse/ceiling
logic, form validation, the worker secret/config contract, and idempotent
provisioning.

## Files

| File | Where it runs | Purpose |
|---|---|---|
| `apps.py`, `urls.py`, `views.py`, `forms.py`, `templates/` | web process | the plugin page (config form + mention feed) |
| `provisioning.py` | web process | all SDK calls (idempotent provision + reads) |
| `worker_body.py` | environment venv | the managed tracker script's code |
| `tests.py` | test runner | unit tests for the engine + web/provisioning surface |

See `docs/plugins.md` for the full author guide.
