# Generated for tenancy Stage 3 — catch-all backfill of any still-NULL scoped rows.

from django.db import migrations


# Scoped models whose CREATION did not stamp a workspace until Stage 3. The 0029
# backfill assigned every row that existed then; this covers any created in the
# window between the seam (Stages 0-2) and creation-stamping (Stage 3) — their
# workspace is NULL. After this, the Stage 3 cpanel sweep's strict
# ``for_workspace`` filters are byte-for-byte (no scoped row is left NULL).
# Secret/DataStore/DataStoreAPIToken are handled by their own 0032/0033/0034
# data migrations; this finishes Script, Run, ScriptSchedule, Environment.
_MODELS = ["Script", "Run", "ScriptSchedule", "Environment"]


def assign_null_to_default(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    default = Workspace.objects.filter(is_default=True).order_by("created_at").first()
    if default is None:
        return
    for model_name in _MODELS:
        model = apps.get_model("core", model_name)
        model.objects.filter(workspace__isnull=True).update(workspace=default)


def noop_reverse(apps, schema_editor):
    # Irreversible data assignment (we can't tell which rows were originally NULL);
    # leaving them attached on reverse is harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_apitoken_workspace"),
    ]

    operations = [
        migrations.RunPython(assign_null_to_default, noop_reverse),
    ]
