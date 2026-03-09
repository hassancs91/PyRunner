# Generated migration for S3 storage settings

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_add_weekly_monthly_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsettings",
            name="s3_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enable S3-compatible storage for backups",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_endpoint_url",
            field=models.CharField(
                blank=True,
                max_length=500,
                help_text="S3 endpoint URL (leave empty for AWS S3)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_region",
            field=models.CharField(
                blank=True,
                default="us-east-1",
                max_length=50,
                help_text="S3 region",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_bucket_name",
            field=models.CharField(
                blank=True,
                max_length=255,
                help_text="S3 bucket name",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_access_key_encrypted",
            field=models.TextField(
                blank=True,
                help_text="S3 access key (encrypted)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_secret_key_encrypted",
            field=models.TextField(
                blank=True,
                help_text="S3 secret key (encrypted)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_use_ssl",
            field=models.BooleanField(
                default=True,
                help_text="Use SSL/TLS for S3 connections",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_path_style",
            field=models.BooleanField(
                default=False,
                help_text="Use path-style addressing (required for MinIO)",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="s3_last_tested_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When S3 connection was last successfully tested",
            ),
        ),
    ]
