# Generated manually for weekly/monthly scheduling

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_datastore_api_token"),
    ]

    operations = [
        # Add weekly_days field
        migrations.AddField(
            model_name="scriptschedule",
            name="weekly_days",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Days of week [0-6] where 0=Monday, e.g., [0, 2, 4] for Mon/Wed/Fri",
            ),
        ),
        # Add weekly_times field
        migrations.AddField(
            model_name="scriptschedule",
            name="weekly_times",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="List of times in HH:MM format for weekly mode",
            ),
        ),
        # Add monthly_days field
        migrations.AddField(
            model_name="scriptschedule",
            name="monthly_days",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Days of month [1-31], e.g., [1, 15] for 1st and 15th",
            ),
        ),
        # Add monthly_times field
        migrations.AddField(
            model_name="scriptschedule",
            name="monthly_times",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="List of times in HH:MM format for monthly mode",
            ),
        ),
        # Update run_mode choices to include weekly and monthly
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
                ],
                default="manual",
                max_length=20,
            ),
        ),
    ]
