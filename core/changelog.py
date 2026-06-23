"""
Release notes shown in-app at /cpanel/changelog/.

Keep the newest release first. Each entry groups its changes by a short tag
("Added" / "Improved" / "Fixed" / "Security"); the template maps tags to colors.
When you cut a release, bump ``pyrunner/version.py`` and add an entry here.
"""

CHANGELOG = [
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
