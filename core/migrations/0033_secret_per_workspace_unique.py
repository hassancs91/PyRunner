# Generated for tenancy Stage 3 — per-workspace Secret.key uniqueness.

from django.db import migrations, models


def assign_null_secrets_to_default(apps, schema_editor):
    """Attach any still-unassigned secrets to the default workspace.

    The 0029 backfill assigned every secret that existed then; this covers any
    created in the window between the tenancy seam and this migration (their
    ``workspace`` is NULL). After this, per-workspace key uniqueness and the
    workspace-scoped list both work without relying on the NULL fallback.
    Idempotent; keys were globally unique before, so attaching can never collide.
    """
    Workspace = apps.get_model("core", "Workspace")
    Secret = apps.get_model("core", "Secret")

    default = Workspace.objects.filter(is_default=True).order_by("created_at").first()
    if default is None:
        return
    Secret.objects.filter(workspace__isnull=True).update(workspace=default)


def noop_reverse(apps, schema_editor):
    # Irreversible data assignment (we can't tell which rows were originally NULL);
    # leaving them attached on reverse is harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_datastore_per_workspace_unique"),
    ]

    operations = [
        # Drop the global-unique on key (replaced by the composite + partial below).
        migrations.AlterField(
            model_name="secret",
            name="key",
            field=models.CharField(
                help_text="Environment variable name (uppercase, underscores allowed)",
                max_length=100,
            ),
        ),
        migrations.RunPython(assign_null_secrets_to_default, noop_reverse),
        migrations.AddConstraint(
            model_name="secret",
            constraint=models.UniqueConstraint(
                fields=("workspace", "key"), name="uniq_secret_workspace_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="secret",
            constraint=models.UniqueConstraint(
                condition=models.Q(("workspace__isnull", True)),
                fields=("key",),
                name="uniq_secret_key_when_no_workspace",
            ),
        ),
    ]
