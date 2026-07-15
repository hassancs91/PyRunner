# Generated manually for raw cron scheduling

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_plugin_ownership"),
    ]

    operations = [
        # Add the raw cron expression field
        migrations.AddField(
            model_name="scriptschedule",
            name="cron_expression",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    'Raw cron expression, e.g. "0 9 * * 1-5" '
                    "(minute hour day-of-month month day-of-week)"
                ),
                max_length=100,
            ),
        ),
        # Update run_mode choices to include cron
        migrations.AlterField(
            model_name="scriptschedule",
            name="run_mode",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("interval", "Interval"),
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                    ("monthly", "Monthly"),
                    ("cron", "Cron expression"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
    ]
