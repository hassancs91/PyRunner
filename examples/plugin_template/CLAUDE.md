# Plugin Template — guide

The smallest correct PyRunner plugin: one superuser page that reads/writes one
value via `DataStoreAPI`. Copy it to start a new plugin.

## Contracts (the doctor enforces these)

- **SDK-only:** import `core.plugins.api`, never `core.models|services|tasks`.
- **No `models.py` / `migrations/`** — state lives in DataStores/Secrets.
- **Owner everything** by the slug (here `OWNER = "plugin_template"`). The slug
  must match the folder name, `plugin.json`, `apps.py`, and `urls.py`.
- **Single top-level folder** in the zip, named for the slug.

## Make it yours

Rename the slug in `plugin.json`, `apps.py`, `urls.py`, `views.py` (`OWNER`), and
the `templates/<slug>/` folder. Then iterate in dev mode
(`PYRUNNER_PLUGIN_DEV=/abs/path python manage.py runserver`) and validate with
`plugin_doctor --path <folder>`. Graduate to the patterns in
[sdk_showcase](../sdk_showcase/) as you add secrets, a managed script, or a
schedule.
