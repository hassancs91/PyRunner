# Sales Dashboard (sample plugin + script)

The canonical PyRun plugin pattern: **a script produces data, a plugin
visualizes it.** The script writes synthetic orders to a DataStore; the plugin
reads that DataStore and renders KPIs, a per-product breakdown, and recent
orders — plus a "Run collector now" button that triggers the script.

## How the pieces connect

```
Sales Collector (script, runs in an env venv)
   └─ from pyrunner_datastore import DataStore   # PyRun-provided
       └─ writes orders ──►  DataStore "sales_data"  (shared SQLite)
                                     ▲
Sales Dashboard (plugin, runs in the web process)
   └─ from core.models import DataStore           # reads the same store
```

## Setup

1. **Data Stores** → create one named exactly `sales_data`.
2. **Scripts** → create a script named exactly `Sales Collector`, paste
   [`collector_script.py`](collector_script.py), pick an environment, save, run it.
3. **Plugins** → upload this folder zipped (single top-level `sales_dashboard/`
   folder), **Activate**, then **Restart**.
4. Open **Sales Dashboard** in the sidebar. Use **Run collector now** to add data.

> The plugin looks up the script by the name `Sales Collector` and the store by
> `sales_data` — keep those names, or edit `STORE_NAME` / `SCRIPT_NAME` in
> `views.py`.

See `docs/plugins.md` for the full author guide.
