# PyRunner plugins

This directory is the install location for PyRunner plugins. On a deployed
instance it is a **persistent volume** (`pyrunner_plugins` → `/app/plugins`), so
plugins you install survive redeploys of the stock image.

A plugin is a self-contained Django app:

```
plugins/
  __init__.py          # tracked — makes `plugins` an importable package
  README.md            # tracked — this file
  <slug>/              # one folder per plugin (NOT tracked in git)
    __init__.py
    apps.py            # subclass core.plugins.PluginAppConfig
    plugin.py          # (optional) a PyRunnerPlugin descriptor
    urls.py            # app_name = "<slug>"; auto-mounted at /plugins/<slug>/
    views.py
    models.py          # optional
    migrations/
    templates/<slug>/...
    plugin.json        # manifest
```

## The safety contract

- **Installed ≠ active.** Dropping files here does nothing until a `Plugin` row
  has `status=active`. The running server only ever imports active plugins.
- **Preflight before load.** Activation (and every boot) validates each active
  plugin in an isolated subprocess (`manage.py plugin_preflight`). A plugin that
  fails import / migrate / URL resolution is flipped to `ERRORED` and skipped —
  the live site is never affected.
- **Kill switch.** Boot with `PYRUNNER_DISABLE_PLUGINS=1` to load zero plugins.

## Developing a plugin here

Because `plugins/` is a real package, a plugin folder dropped here imports
natively as `plugins.<slug>` — so it doubles as a convenient (gitignored)
workspace for plugins under development: `manage.py test plugins.<slug>` works
directly, and Dev Mode (`PYRUNNER_PLUGIN_DEV`, see the plugin author guide in
`docs/plugins.md`) can point at the folder for live-reload iteration.

See `docs/PLAN_plugin_system.md` for the full design.
