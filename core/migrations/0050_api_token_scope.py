# Plugin API Stage 1 — generalize DataStoreAPIToken into APIToken.
#
# The rename is STATE-ONLY: db_table = "datastore_api_tokens" is pinned on the
# model, so RenameModel rewrites migration state without touching SQL. New
# `scope` + `plugin_slug` fields are additive; legacy rows are backfilled from
# the datastore FK (FK set → "datastore", else → "datastores") so no existing
# token changes reach. Plugin scope ("plugin") is only ever set on NEW tokens —
# a legacy token must NOT silently gain plugin API access.

from django.db import migrations, models


def backfill_scope(apps, schema_editor):
    """Map pre-scope rows onto the new scope field from their datastore FK."""
    APIToken = apps.get_model("core", "APIToken")
    APIToken.objects.filter(datastore__isnull=False).update(scope="datastore")
    # FK-less rows keep the AddField default "datastores" (today's "global").


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0049_secret_providers"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="DataStoreAPIToken",
            new_name="APIToken",
        ),
        migrations.AddField(
            model_name="apitoken",
            name="scope",
            field=models.CharField(
                choices=[
                    ("datastore", "Single datastore"),
                    ("datastores", "All datastores"),
                    ("plugin", "Plugin API"),
                ],
                default="datastores",
                help_text="What this token grants access to",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="plugin_slug",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Plugin this token is scoped to (scope=plugin only)",
                max_length=100,
            ),
        ),
        migrations.RunPython(backfill_scope, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="apitoken",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(scope="datastore", datastore__isnull=False)
                    | (
                        ~models.Q(scope="datastore")
                        & models.Q(datastore__isnull=True)
                    )
                ),
                name="apitoken_scope_datastore_fk",
            ),
        ),
        migrations.AddConstraint(
            model_name="apitoken",
            constraint=models.CheckConstraint(
                condition=(
                    (models.Q(scope="plugin") & ~models.Q(plugin_slug=""))
                    | (~models.Q(scope="plugin") & models.Q(plugin_slug=""))
                ),
                name="apitoken_scope_plugin_slug",
            ),
        ),
    ]
