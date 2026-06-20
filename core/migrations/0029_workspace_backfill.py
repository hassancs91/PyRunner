"""
Default-workspace backfill (tenancy seam, Phase A).

Creates exactly one default Workspace and assigns every existing scoped row to
it, so a single-workspace instance behaves exactly like today. Written to be
idempotent (safe to re-run) and reversible. Uses historical models via
``apps.get_model`` — never the live model classes.
"""

from django.db import migrations

# The scoped models that carry a nullable ``workspace`` FK (added in 0028).
SCOPED_MODELS = ["Script", "Secret", "Run", "DataStore", "Environment", "ScriptSchedule"]


def backfill_default_workspace(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")

    # Idempotent: reuse an existing default if one is already present.
    default = Workspace.objects.filter(is_default=True).order_by("created_at").first()
    if default is None:
        default = Workspace.objects.create(name="Default Workspace", is_default=True)

    # Assign only un-scoped rows, so a re-run (or a partially-applied state)
    # never reassigns rows that already belong to a workspace.
    for model_name in SCOPED_MODELS:
        Model = apps.get_model("core", model_name)
        Model.objects.filter(workspace__isnull=True).update(workspace=default)


def unbackfill(apps, schema_editor):
    # Reverse cleanly: detach rows, then drop the default workspace.
    for model_name in SCOPED_MODELS:
        Model = apps.get_model("core", model_name)
        Model.objects.filter(workspace__isnull=False).update(workspace=None)

    Workspace = apps.get_model("core", "Workspace")
    Workspace.objects.filter(is_default=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_workspace_seam"),
    ]

    operations = [
        migrations.RunPython(backfill_default_workspace, unbackfill),
    ]
