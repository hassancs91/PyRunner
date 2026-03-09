# Generated migration for S3 scheduled backup settings

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_s3_storage_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enable scheduled backups to S3",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_schedule",
            field=models.CharField(
                choices=[
                    ("disabled", "Disabled"),
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                ],
                default="disabled",
                max_length=20,
                help_text="Backup schedule frequency",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_time",
            field=models.TimeField(
                default="02:00",
                help_text="Time of day to run backup (in instance timezone)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_day",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text="Day of week for weekly backups (0=Monday, 6=Sunday)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_prefix",
            field=models.CharField(
                blank=True,
                default="pyrunner-backups/",
                max_length=255,
                help_text="S3 key prefix for backup files",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_retention_count",
            field=models.PositiveIntegerField(
                default=7,
                help_text="Number of backups to keep in S3 (0 = keep all)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_include_runs",
            field=models.BooleanField(
                default=False,
                help_text="Include run history in scheduled backups",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_max_runs",
            field=models.PositiveIntegerField(
                default=1000,
                help_text="Maximum runs to include in backup",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_include_datastores",
            field=models.BooleanField(
                default=True,
                help_text="Include datastores in scheduled backups",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_last_run_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When the last scheduled backup ran",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_last_status",
            field=models.CharField(
                blank=True,
                default="",
                max_length=20,
                help_text="Status of last backup (success/failed)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_last_error",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Error message from last failed backup",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_backup_last_size",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Size of last backup in bytes",
            ),
        ),
    ]
