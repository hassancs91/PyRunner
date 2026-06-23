# Plugin Template (PyRunner)

The smallest **correct** PyRunner plugin — a starting point you copy and rename.
It's a single superuser page that stores and reads one value through the SDK
(`core.plugins.api`), with no models, no migrations, and import-light.

For fuller examples see [sdk_showcase](../sdk_showcase/) (every SDK capability,
annotated) and [qdrant_backup](../qdrant_backup/) (a real plugin end-to-end).

## Make it yours

1. **Copy** this folder and rename it to your slug (lowercase letter, then
   letters/digits/underscores), e.g. `examples/my_plugin/`.
2. **Rename the slug everywhere it appears** so they all match the folder name:
   - `plugin.json` → `"slug"`
   - `apps.py` → `name = "plugins.<slug>"`, `label = "<slug>"`, `PyRunnerPlugin(slug=…)`, and the `url_name`
   - `urls.py` → `app_name = "<slug>"`
   - `views.py` → `OWNER = "<slug>"`
   - the `templates/<slug>/` folder name (and the `render(...)` path)
3. **Develop in dev mode** (live reload, no upload):
   ```bash
   export DEBUG=True PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/my_plugin
   python manage.py runserver
   ```
4. **Validate** before shipping:
   ```bash
   python manage.py plugin_doctor --path examples/my_plugin
   ```

## The rules (enforced by the doctor)

- Persist state in **DataStores / Secrets via the SDK** — never `models.py` /
  `migrations/`.
- Import **`core.plugins.api`** only — not `core.models` / `core.services` /
  `core.tasks` (import-light).
- The packaged zip must contain a **single top-level folder** named for the slug.

See `docs/plugins.md` for the full author guide.
