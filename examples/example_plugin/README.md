# Example Plugin

A minimal, working PyRun plugin you can install as-is to see the system end to
end. It adds a sidebar entry and a page that runs a snippet inside a chosen
environment's venv via `run_in_environment` (an isolated subprocess — never the
web process).

## Install it

1. Zip the folder so the archive has a single top-level `example_plugin/` folder:
   ```bash
   cd examples
   zip -r example_plugin.zip example_plugin
   ```
   (On Windows: right-click the `example_plugin` folder → Send to → Compressed folder.)
2. In PyRun: **Plugins → Upload plugin**, choose the `.zip`.
3. Click **Activate** (this validates it in an isolated preflight), then **Restart now**.
4. Open **Example Plugin** in the sidebar.

## What to copy

- `plugin.json` — manifest (`slug` must match the folder name).
- `apps.py` — subclass `PluginAppConfig`, set `name`/`label`/`plugin`.
- `urls.py` — `app_name = "<slug>"`; auto-mounted at `/plugins/<slug>/`.
- `views.py` — the `run_in_environment(env, code=...)` compute pattern.
- `templates/<slug>/…` — extend `base.html` and `{% include "cpanel/_sidebar.html" %}`.

See `docs/plugins.md` for the full author guide.
