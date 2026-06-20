# Generated for tenancy Stage 3 — workspace FK on DataStoreAPIToken.

import django.db.models.deletion
from django.db import migrations, models


def assign_null_tokens_to_default(apps, schema_editor):
    """Scope every existing API token to the default workspace.

    Pre-tenancy tokens carry no workspace; binding them to the default keeps a
    single-workspace instance byte-for-byte (their datastores are all in the
    default workspace) and ensures a pre-tenancy global token does NOT gain
    access to workspaces created later. Idempotent.
    """
    Workspace = apps.get_model("core", "Workspace")
    DataStoreAPIToken = apps.get_model("core", "DataStoreAPIToken")

    default = Workspace.objects.filter(is_default=True).order_by("created_at").first()
    if default is None:
        return
    DataStoreAPIToken.objects.filter(workspace__isnull=True).update(workspace=default)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_secret_per_workspace_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="datastoreapitoken",
            name="workspace",
            field=models.ForeignKey(
                blank=True,
                help_text="Workspace this token is scoped to (tenancy; nullable).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="api_tokens",
                to="core.workspace",
            ),
        ),
        migrations.RunPython(assign_null_tokens_to_default, noop_reverse),
    ]
