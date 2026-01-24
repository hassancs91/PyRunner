# Generated migration for registration control and user invites

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_globalsettings_setup_completed"),
    ]

    operations = [
        # Add allow_registration to GlobalSettings
        migrations.AddField(
            model_name="globalsettings",
            name="allow_registration",
            field=models.BooleanField(
                default=True,
                help_text="Allow new users to register without an invite (auto-disabled after first user)",
            ),
        ),
        # Create UserInvite model
        migrations.CreateModel(
            name="UserInvite",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        help_text="Email address of the invited user",
                        max_length=254,
                        unique=True,
                    ),
                ),
                (
                    "token",
                    models.CharField(db_index=True, max_length=64, unique=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sent_invites",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "used_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="received_invite",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "user invite",
                "verbose_name_plural": "user invites",
                "db_table": "user_invites",
            },
        ),
        migrations.AddIndex(
            model_name="userinvite",
            index=models.Index(fields=["token"], name="user_invite_token_idx"),
        ),
        migrations.AddIndex(
            model_name="userinvite",
            index=models.Index(fields=["email"], name="user_invite_email_idx"),
        ),
    ]
