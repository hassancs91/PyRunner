# Scheduling scripts

PyRunner can run a script automatically on a schedule. Every script has one
schedule, configured in the **Schedule** panel of the script edit form. There
are six run modes:

| Mode | What it does |
|------|--------------|
| **Manual** | No automatic runs — you trigger it yourself (or via webhook). |
| **Interval** | Every N minutes, from a fixed list of intervals. |
| **Daily** | One or more `HH:MM` times, every day. |
| **Weekly** | One or more days of the week, at one or more `HH:MM` times. |
| **Monthly** | One or more days of the month, at one or more `HH:MM` times. |
| **Cron expression** | Any standard 5-field cron expression. |

Under the hood all non-manual modes compile to a
[django-q2](https://django-q2.readthedocs.io/) schedule. The daily/weekly/monthly
modes build a cron expression for you; **Cron expression** mode simply lets you
type that expression directly, for schedules the guided modes can't express
(e.g. "every 15 minutes during business hours on weekdays").

---

## Cron expression mode

### Using it

1. Open a script → **Edit** → the **Schedule** panel.
2. Choose **Cron expression** as the run mode.
3. Type a standard **5-field** cron expression, for example `0 9 * * 1-5`.
4. As you type, PyRunner validates the expression and shows the **next three run
   times** so you can confirm it before saving.
5. Make sure **Schedule Active** is ticked, then **Save**.

The next run time is shown on the script detail page and in the dashboard's
**Upcoming runs** list.

### Cron syntax

A cron expression has five whitespace-separated fields:

```
┌───────────── minute        (0-59)
│ ┌───────────── hour        (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month     (1-12)
│ │ │ │ ┌───────────── day of week (0-6, 0 = Sunday)
│ │ │ │ │
* * * * *
```

Each field accepts:

- `*` — every value
- a single number — e.g. `5`
- a list — `1,15,30`
- a range — `1-5`
- a step — `*/15` (every 15), or `0-30/10` (0,10,20,30)

**Examples:**

| Expression | Meaning |
|------------|---------|
| `*/5 * * * *` | Every 5 minutes |
| `0 * * * *` | Every hour, on the hour |
| `0 9 * * 1-5` | 09:00, Monday–Friday |
| `30 8,17 * * *` | 08:30 and 17:30 every day |
| `0 0 1 * *` | Midnight on the 1st of every month |
| `0 0 * * 0` | Midnight every Sunday |
| `15 */2 * * *` | Every 2 hours, at quarter past |

Need help building one? Try [crontab.guru](https://crontab.guru) — the form
also links to it.

### What is *not* accepted

To keep behaviour predictable, PyRunner intentionally rejects:

- **Named shortcuts** like `@daily`, `@hourly`, `@reboot`.
- **6-field (seconds) expressions** — use exactly five fields.
- Anything `croniter` considers invalid (bad ranges, out-of-bounds values, etc.).

You'll see the specific reason inline in the form.

### Timezone

Cron expressions are interpreted in the **server's timezone** (the Django
`TIME_ZONE` / `Q_CLUSTER` timezone of your deployment, UTC by default). This is
the same behaviour as the daily/weekly/monthly modes, whose cron expressions are
also generated in server time. The "Timezone" selector on the other modes is a
display/label aid and is **not** applied to the underlying cron schedule; if you
need runs pinned to a specific timezone, account for the offset in the
expression, or set your deployment's `TIME_ZONE`.

The **next-run preview** in the form is computed in the same timezone the
scheduler uses, so what you preview is what you get.

---

## Pausing & resuming

- Untick **Schedule Active** (or use the toggle on the script detail page) to
  pause a single schedule without losing its configuration.
- Admins can pause/resume **all** schedules globally from Settings; cron
  schedules are included in the global pause/resume.

Every change is recorded in the script's **schedule history** (including the
cron expression before/after), so you can audit who changed what and when.

---

## Backup & restore

Cron schedules — and the full weekly/monthly configuration — are included in
PyRunner backups and are recreated on restore. The django-q2 schedule objects
themselves are regenerated from the stored configuration after import, so
`next_run` is recomputed on the restored instance.

---

## Scheduling from a plugin (SDK)

Plugins can schedule the scripts they provision via `ScheduleAPI.sync()` in
`core.plugins.api`:

```python
from core.plugins.api import ScheduleAPI
from core.models import ScriptSchedule

# `script` is a Script the plugin created via ScriptAPI.upsert(...)
ScheduleAPI("my-plugin").sync(
    script,
    mode=ScriptSchedule.RunMode.CRON,
    cron="0 9 * * 1-5",   # weekday mornings
)
```

An invalid `cron` value raises `ValueError` with the validation reason. The
other modes are unchanged:

```python
# interval
ScheduleAPI("my-plugin").sync(script, mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=60)
# daily
ScheduleAPI("my-plugin").sync(script, mode=ScriptSchedule.RunMode.DAILY, time_str="09:00")
```

---

## How it works (internals)

| Piece | Where |
|-------|-------|
| `ScriptSchedule.run_mode` + `cron_expression` field | `core/models/schedule.py` |
| Validation & next-run preview helpers | `ScheduleService.validate_cron_expression()` / `preview_cron_runs()` in `core/services/schedule_service.py` |
| django-q2 schedule creation | `ScheduleService._create_cron_schedule()` |
| Form field + validation | `ScheduleForm` in `core/forms.py` |
| Live preview endpoint | `cron_preview_view` → `cpanel:cron_preview` (`/cpanel/scripts/cron/preview/`) |
| Form UI + preview JS | `templates/cpanel/scripts/_form_sidebar.html`, `static/js/script_form.js` |
| Migration | `core/migrations/0039_scriptschedule_cron.py` |
| Tests | `core/test_cron_scheduling.py` |

`croniter` (already a dependency, used by django-q2) handles cron parsing and
next-run computation. When a schedule is saved, `ScheduleService.sync_schedule()`
deletes the script's old django-q2 schedules and creates a single `CRON`-type
schedule whose `cron` field is the raw expression. The django-q2 worker then
fires `core.tasks.execute_scheduled_run` at each matching time.
