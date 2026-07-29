"""
Release notes shown in-app at /cpanel/changelog/.

Keep the newest release first. Each entry groups its changes by a short tag
("Added" / "Improved" / "Fixed" / "Security"); the template maps tags to colors.
When you cut a release, bump ``pyrunner/version.py`` and add an entry here.
"""

CHANGELOG = [
    {
        "version": "1.16.0",
        "date": "July 29, 2026",
        "headline": (
            "Write a helper once and import it from every script, store files "
            "anywhere S3-compatible instead of just backups — and schedules now "
            "keep the timezone you actually picked."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Libraries — shared code your scripts import",
                "body": (
                    "Stop pasting the same helper into five scripts. Create a "
                    "library under Libraries, add one or more modules in the "
                    "tabbed editor, attach it to a script from the script's "
                    "form, and import it: from my_helpers.utils import clean. "
                    "Every save that changes code creates a new version, and a "
                    "run is pinned to the version that was current when it was "
                    "queued — so editing a library can never change what an "
                    "already-queued run executes, and each run's page shows "
                    "exactly which versions it used. Full history per library "
                    "with a diff against the current code and one-click "
                    "restore. A library can't be deleted while a script still "
                    "imports it."
                ),
            },
            {
                "tag": "Added",
                "title": "Object storage — more than one bucket, more than backups",
                "body": (
                    "The S3 settings are now a real storage service. Save "
                    "several connections (Cloudflare R2, AWS S3, Backblaze B2, "
                    "DigitalOcean Spaces, MinIO, or any S3-compatible endpoint) "
                    "instead of exactly one, switch which one backups use with "
                    "a click, and point plugins at a different bucket so your "
                    "backup bucket stays private. Your existing configuration "
                    "carries over automatically — backups keep working with "
                    "nothing to re-enter. Scripts can store files with "
                    "import pyrunner_storage, no credentials and nothing to "
                    "install, because the bytes travel over PyRunner's internal "
                    "API instead of putting S3 keys in your script."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugins can serve APIs and publish shareable pages",
                "body": (
                    "A plugin can expose read-only HTTP resources that PyRunner "
                    "serves for it, handling authentication, rate limiting, and "
                    "workspace scoping itself — the plugin never sees a raw "
                    "request or a token. New API tokens can be scoped to "
                    "exactly one plugin. A plugin can also publish a "
                    "read-only page at an unguessable link that needs no "
                    "login — useful for sharing a report with someone outside "
                    "your team — and every share is listed and revocable under "
                    "Settings, with revoking killing the link for good."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Worker settings now actually apply",
                "body": (
                    "The values saved under Settings → Workers (worker count, "
                    "task timeout, retry delay, queue limit) were never read — "
                    "the cluster always ran on the built-in defaults no matter "
                    "what the page said. \"Restart workers to apply\" is "
                    "finally true."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Long runs no longer get stuck at \"running\"",
                "body": (
                    "A script that ran longer than the worker's re-delivery "
                    "window could be handed to a second worker, which clobbered "
                    "the live run: stuck at running forever, output never "
                    "captured, force-stop broken. Duplicate deliveries are now "
                    "no-ops, the window sizes itself above your longest script "
                    "timeout, and the script form warns you when a timeout "
                    "would outrun it. Runs left behind by a killed worker "
                    "(container restart, out-of-memory) now heal themselves to "
                    "failed with an explanation instead of hanging forever."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Schedules keep the timezone you picked",
                "body": (
                    "The timezone selector appeared once per schedule mode, and "
                    "the browser submitted all of them — so the hidden copy won "
                    "and your choice was silently replaced with UTC. Setting a "
                    "23:30 daily run in Asia/Beirut ran it at 23:30 UTC, with "
                    "no error and the selector reverting on reload. There is "
                    "now one timezone selector, shared by daily, weekly, and "
                    "monthly."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Schedule history shows what actually changed",
                "body": (
                    "Changing a schedule's mode, interval, timezone, or "
                    "active state recorded nothing in its history, and an edit "
                    "that also changed run times recorded the new timezone as "
                    "though it had been the old one. History now captures the "
                    "real before and after."
                ),
            },
        ],
    },
    {
        "version": "1.15.1",
        "date": "July 28, 2026",
        "headline": (
            "Security patch: Docker images no longer contain built-in fallback "
            "keys — every deployment now runs on keys only you know. If you "
            "never set your own keys, this release makes that visible and "
            "walks you through fixing it."
        ),
        "changes": [
            {
                "tag": "Security",
                "title": "Baked-in image keys removed — check your deployment",
                "body": (
                    "Docker images up to 1.15.0 shipped with build-time "
                    "SECRET_KEY and ENCRYPTION_KEY values left inside the "
                    "image. A deployment that didn't set its own keys would "
                    "boot normally but run on those publicly-known values — "
                    "meaning sessions could be forged and secrets stored in "
                    "PyRunner could be decrypted by anyone with the public "
                    "image. The keys are now stripped from the image, and "
                    "PyRunner refuses to start on the old known values, with "
                    "clear instructions for generating real ones. If your "
                    "deployment never set its own keys: upgrade, generate "
                    "fresh keys, and rotate the secrets you had stored."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Key-generation one-liners no longer hang",
                "body": (
                    "Running a one-off command in the container (docker run "
                    "... python -c \"...\") used to ignore the command and "
                    "boot the whole server, hanging your terminal — which "
                    "broke the documented way to generate keys. The container "
                    "now runs the command you give it and exits."
                ),
            },
        ],
    },
    {
        "version": "1.15.0",
        "date": "July 15, 2026",
        "headline": (
            "PyRunner scripts get real SQL: managed PostgreSQL databases with "
            "tables, joins, and transactions — plus secrets you can pull live "
            "from Vault, AWS, Infisical, or Doppler instead of storing them here."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Databases — real SQL for scripts & plugins",
                "body": (
                    "Scripts and plugins can now use a real PostgreSQL database "
                    "— tables, joins, indexes, transactions, the whole Python DB "
                    "ecosystem — alongside the simple key-value Data Stores. "
                    "Point PyRunner at a data server with a single environment "
                    "variable (it can sit next to a SQLite core), then provision "
                    "databases from the console: each one is its own isolated "
                    "Postgres schema with a dedicated login role, so a script "
                    "granted access gets full SQL inside its own space and cannot "
                    "reach anything else. Scripts connect with the new "
                    "pyrunner_db helper; plugins provision their own through the "
                    "SDK. A read-only explorer lets you browse tables, export "
                    "CSV, and watch live activity — sessions, slow queries, and "
                    "sizes — and everything is included in backups."
                ),
            },
            {
                "tag": "Added",
                "title": "External Secret Providers — bring your own vault",
                "body": (
                    "A secret no longer has to live inside PyRunner. Save a "
                    "provider profile for HashiCorp Vault / OpenBao, AWS Secrets "
                    "Manager, Infisical, Doppler, or any HTTP JSON endpoint, then "
                    "point a secret at it with a reference like kv/myapp#API_KEY. "
                    "The real value is fetched fresh at run time, masked in logs "
                    "exactly like a local secret, and never written to disk — so "
                    "rotating it in your vault is picked up automatically, and "
                    "grants, workspaces, and the plugin SDK keep working "
                    "unchanged. If a value can't be fetched the run fails fast "
                    "with a clear error, with an optional last-known-good "
                    "fallback for automation that must not stall."
                ),
            },
            {
                "tag": "Improved",
                "title": "Container-aware resource stats + pinned meters",
                "body": (
                    "When PyRunner runs in Docker, the dashboard's CPU and "
                    "memory now show the container's own usage — matching "
                    "docker stats — with the full host/VPS as a secondary line, "
                    "and memory measured against the container's limit when one "
                    "is set. Every console page also gains a compact CPU / Mem / "
                    "Disk mini-meter in the header, updated live and "
                    "color-coded, so you can keep an eye on load from anywhere "
                    "in the app."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Scheduling, workers & Postgres correctness",
                "body": (
                    "Scheduled jobs now run in the timezone you configured "
                    "instead of always UTC (daylight-saving handled), “Pause all "
                    "schedules” no longer knocks out the built-in maintenance "
                    "jobs, and “Restart workers” actually restarts them. Weekly "
                    "and monthly schedules show up in upcoming runs and backups. "
                    "Password-strength rules now apply everywhere a password is "
                    "set, rate limiting is shared across workers and can't be "
                    "starved, and a range of PostgreSQL-specific rough edges "
                    "(datastore writes, the database-size card, setup status) "
                    "are smoothed out."
                ),
            },
            {
                "tag": "Security",
                "title": "Provider-key masking, SSRF & plugin-ZIP hardening",
                "body": (
                    "AI now works with every configured provider and "
                    "third-party provider keys are masked in run output; webhook "
                    "notification URLs that resolve to internal addresses are "
                    "blocked (SSRF); and plugin ZIP installs enforce their size "
                    "limits against real extracted bytes with hardened "
                    "path-traversal checks."
                ),
            },
        ],
    },
    {
        "version": "1.14.0",
        "date": "July 15, 2026",
        "headline": (
            "PyRunner now talks to more than Claude: save multiple AI provider "
            "profiles — Anthropic, Z.AI GLM, OpenRouter, even a local Ollama — "
            "and switch the active one with a click. Plus Telegram channels "
            "with an approval inbox, and Py AI, an assistant that knows your "
            "instance."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "AI Providers — bring any model",
                "body": (
                    "The Claude integration is now a generic AI provider system. "
                    "Save one profile per provider — Anthropic (subscription or "
                    "API key), Z.AI GLM, OpenRouter, a local Ollama, or any "
                    "Anthropic-compatible endpoint — with its own encrypted "
                    "credential and default model, and switch the active one "
                    "with a single click. Every provider gets a real Test "
                    "button: Anthropic runs a live web search, and other "
                    "providers run a real tool-call round-trip, so you know a "
                    "model can drive tools before wiring it into automations. "
                    "Existing Claude setups upgrade automatically — no "
                    "reconfiguration."
                ),
            },
            {
                "tag": "Added",
                "title": "Channels — Telegram, both directions",
                "body": (
                    "Scripts can send Telegram messages with the new "
                    "pyrunner_notify helper, and run-completion notifications "
                    "can be routed to channels. Inbound is protected by a "
                    "deny-by-default approval inbox: unknown senders queue up "
                    "until you approve them, with per-sender rate limits and a "
                    "daily reply cap."
                ),
            },
            {
                "tag": "Added",
                "title": "Py AI — ask your instance anything",
                "body": (
                    "A read-only assistant that answers questions about this "
                    "PyRunner instance — “how many scripts do I have?”, "
                    "“did the backup run last night?” — using live "
                    "data from your scripts, runs, schedules, and datastores. "
                    "Chat from the dashboard, or bind it to a Telegram channel. "
                    "It can only read: no run, write, or secret access, by "
                    "construction."
                ),
            },
            {
                "tag": "Improved",
                "title": "Operational hardening",
                "body": (
                    "A shared cache backend (database-backed by default, Redis "
                    "opt-in) makes rate limiting and webhook dedup reliable "
                    "across workers; the Docker image gains a HEALTHCHECK and "
                    "Brotli-compressed static assets; and the repo now ships "
                    "CI (tests + linting), a Brand Tracker example plugin, and "
                    "expanded .env documentation."
                ),
            },
            {
                "tag": "Security",
                "title": "Pre-release security audit fixes",
                "body": (
                    "Removed the passwordless magic-link login, gated all "
                    "settings/environment/log administration strictly on "
                    "superuser, blocked pip option injection in bulk package "
                    "installs, and added a scoped Content-Security-Policy "
                    "header. Third-party AI provider keys are now masked in run "
                    "logs, webhook URLs that resolve to internal addresses are "
                    "blocked (SSRF), and plugin-ZIP installs enforce their size "
                    "limits against actual extracted bytes."
                ),
            },
        ],
    },
    {
        "version": "1.13.0",
        "date": "June 22, 2026",
        "headline": (
            "Operational plugins can now watch and stop the runs they create — "
            "live status, run history, and a Stop button — straight from the "
            "plugin SDK, with no coupling to PyRunner's internals."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin SDK run-lifecycle surface (API 2.1)",
                "body": (
                    "ScriptAPI gains latest_run(), runs(), and "
                    "cancel_latest_run(), so a plugin can show a live “running…” "
                    "badge, list recent run history, and offer a Stop button for "
                    "the scripts it provisions — all owner- and workspace-scoped, "
                    "and without importing core.models. Runs are returned as a "
                    "small, JSON-serializable RunView read-model rather than live "
                    "database objects. Purely additive; existing plugins are "
                    "unaffected."
                ),
            },
            {
                "tag": "Improved",
                "title": "One shared force-stop path",
                "body": (
                    "Stopping a run from the plugin SDK reuses the exact same "
                    "kill path as the Tasks page Stop button (a running job's "
                    "process tree is killed, a queued one is dequeued, both marked "
                    "cancelled), so there is a single, consistent way a run is "
                    "force-stopped across the whole product."
                ),
            },
        ],
    },
    {
        "version": "1.12.0",
        "date": "June 22, 2026",
        "headline": (
            "Plugins now carry rich, packaged metadata — author, license, an "
            "icon, links, categories, and a declaration of what they create — "
            "shown on a new plugin detail page. Groundwork for a plugin marketplace."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin metadata + detail page",
                "body": (
                    "A plugin's plugin.json can now declare an author, license, "
                    "icon, summary, homepage/repository/documentation links, and "
                    "categories. Each plugin gets a detail page that surfaces all "
                    "of it, and the plugins list shows the icon and tagline. Every "
                    "field is optional, so existing plugins are unaffected."
                ),
            },
            {
                "tag": "Added",
                "title": "Bundled plugin icons",
                "body": (
                    "Plugins can ship an icon file (e.g. assets/icon.svg) referenced "
                    "from the manifest. It's served straight from the plugin folder, "
                    "so it renders even before a plugin is activated and needs no "
                    "external hosting, with an emoji fallback when none is provided."
                ),
            },
            {
                "tag": "Added",
                "title": "“What this plugin creates”",
                "body": (
                    "A plugin can declare the resources it provisions (scripts, "
                    "secrets, data stores, schedules) in its manifest. You see "
                    "“creates 1 script, 3 secrets, 1 schedule” at install time "
                    "and on the detail page — before granting it anything."
                ),
            },
            {
                "tag": "Improved",
                "title": "Doctor checks plugin metadata",
                "body": (
                    "The activation doctor now validates manifest metadata: it "
                    "refuses malformed values (bad version, an icon path that "
                    "escapes the plugin folder, a wrong-shaped provisions block) "
                    "and advises when marketplace-recommended fields are missing."
                ),
            },
        ],
    },
    {
        "version": "1.11.0",
        "date": "June 21, 2026",
        "headline": (
            "Plugin Platform v2 — develop plugins live, build them on a stable "
            "SDK, and let them own their own scripts, secrets, and data with no "
            "risk to your core database."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin Dev Mode",
                "body": (
                    "Point PYRUNNER_PLUGIN_DEV at a local plugin folder under "
                    "`manage.py runserver` and edit it live — code and templates "
                    "reload instantly, with no upload, preflight, or restart. "
                    "Triple-guarded so it never loads on a production server."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugin SDK (core.plugins.api)",
                "body": (
                    "Plugins now orchestrate scripts, secrets, datastores, "
                    "schedules, and runs through a stable, versioned SDK instead of "
                    "reaching into core internals. It auto-stamps ownership + the "
                    "workspace, is idempotent (re-saving config updates the same "
                    "rows, never duplicates), and runs everything through the real "
                    "execution + scheduling paths."
                ),
            },
            {
                "tag": "Added",
                "title": "Resource ownership + scoped secrets",
                "body": (
                    "Scripts, secrets, and datastores a plugin creates are grouped "
                    "under it with an owner pill, delete-guarded on the generic "
                    "pages (so you can't pull the rug out from under a plugin), and "
                    "removed cleanly on uninstall. Secrets can be injected per "
                    "script (Selected mode) instead of all-at-once — fully opt-in, "
                    "so existing scripts are unchanged."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugin doctor",
                "body": (
                    "Activation now runs a static-lint 'doctor' that refuses a "
                    "rule-breaking plugin with a clear per-rule report — before any "
                    "of its code or migrations can run. Plugins ship no database "
                    "models, so no plugin can ever alter a core table. Authors can "
                    "run `manage.py plugin_doctor` on a folder before shipping."
                ),
            },
            {
                "tag": "Security",
                "title": "No plugin DDL reaches the database",
                "body": (
                    "Plugins persist via owned key-value DataStores, not their own "
                    "tables. With no plugin models or migrations, the entire class "
                    "of 'a plugin migration broke the database' is removed by "
                    "construction — verified live against Postgres."
                ),
            },
        ],
    },
    {
        "version": "1.10.0",
        "date": "June 15, 2026",
        "headline": (
            "Plugins — extend PyRunner with self-contained apps you upload, "
            "validate, and activate, with a hard guarantee that a broken plugin "
            "can never take down your site."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin system",
                "body": (
                    "Install plugins from the console under Plugins (superuser "
                    "only): upload a .zip, then Activate to validate and enable it. "
                    "A plugin is a self-contained Django app that adds pages to the "
                    "sidebar and serves at /plugins/<slug>/ — no core edits, no "
                    "fork. See docs/plugins.md and examples/example_plugin for how "
                    "to write one."
                ),
            },
            {
                "tag": "Added",
                "title": "Safe by design — a broken plugin can't break the site",
                "body": (
                    "Installed is not active: uploading never imports plugin code. "
                    "Activation validates the plugin in a throwaway subprocess "
                    "first, and every boot re-checks each active plugin in "
                    "isolation, auto-quarantining any that fail. Plugin pages and "
                    "compute are sandboxed, and a kill switch "
                    "(PYRUNNER_DISABLE_PLUGINS=1) guarantees a clean recovery boot."
                ),
            },
            {
                "tag": "Added",
                "title": "Run plugin compute in an environment",
                "body": (
                    "Plugins run heavy or third-party-dependent work inside a "
                    "chosen environment's venv via run_in_environment — an isolated "
                    "subprocess with a timeout and captured output — so extra "
                    "packages never touch the main app."
                ),
            },
        ],
    },
    {
        "version": "1.9.0",
        "date": "June 14, 2026",
        "headline": (
            "A redesigned console, full mobile support, Claude AI in your "
            "scripts, and real control over running jobs."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Claude AI integration",
                "body": (
                    "Connect Claude under Services → Claude AI and call it "
                    "straight from your scripts with the bundled pyrunner_ai "
                    "helper. Token usage is tracked per run and per script so you "
                    "can see exactly what each automation costs."
                ),
            },
            {
                "tag": "Added",
                "title": "Force stop running jobs",
                "body": (
                    "Stop a running script for real — the Stop button now "
                    "kills the script's whole process tree (and any child "
                    "processes it spawned), instead of just marking it cancelled "
                    "and waiting for the timeout. The background worker is never "
                    "touched, so the queue keeps processing the moment a job is "
                    "stopped. Stop is available from the Tasks page and the Run "
                    "detail page."
                ),
            },
            {
                "tag": "Added",
                "title": "Per-task detail pages",
                "body": (
                    "Every task is now clickable — drill into queued, "
                    "completed, failed, and system tasks to see the function, "
                    "arguments, result, timing, live PID, and full tracebacks "
                    "for failures. The Run detail page gained task metadata and a "
                    "live PID while running."
                ),
            },
            {
                "tag": "Improved",
                "title": "Redesigned console",
                "body": (
                    "The entire control panel and the authentication screens "
                    "have been rebuilt on a new, consistent design system — "
                    "cleaner panels, clearer status, a refined type scale, and a "
                    "cohesive color and spacing language throughout."
                ),
            },
            {
                "tag": "Improved",
                "title": "Mobile responsive layout",
                "body": (
                    "PyRunner now works on phones and tablets. The sidebar "
                    "becomes an off-canvas drawer, and tables, editors, and forms "
                    "reflow to fit small screens so you can manage runs on the go."
                ),
            },
        ],
    },
    {
        "version": "1.8.2",
        "date": "Earlier release",
        "headline": "",
        "changes": [
            {
                "tag": "Added",
                "title": "Admin-configured auth emails",
                "body": "Authentication emails can be configured by an admin.",
            },
            {
                "tag": "Improved",
                "title": "Longer default timeout",
                "body": "Raised the default script execution timeout.",
            },
        ],
    },
    {
        "version": "1.8.1",
        "date": "Earlier release",
        "headline": "",
        "changes": [
            {
                "tag": "Security",
                "title": "S3 hardening",
                "body": "Security improvements for S3 storage and backups.",
            },
        ],
    },
]
