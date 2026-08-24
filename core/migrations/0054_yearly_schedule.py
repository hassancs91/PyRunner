from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0053_storage_connections")]

    operations = [
        migrations.AddField(
            model_name="scriptschedule",
            name="yearly_month",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, help_text="Month for yearly mode (1-12)"
            ),
        ),
        migrations.AddField(
            model_name="scriptschedule",
            name="yearly_day",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, help_text="Day of month for yearly mode (1-31)"
            ),
        ),
        migrations.AddField(
            model_name="scriptschedule",
            name="yearly_time",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Time for yearly mode (HH:MM)",
                max_length=5,
            ),
        ),
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
                    ("yearly", "Yearly"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
    ]
