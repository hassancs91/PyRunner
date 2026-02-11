# Generated manually for DataStore API Token

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_globalsettings_admin_url_slug"),
    ]

    operations = [
        migrations.CreateModel(
            name="DataStoreAPIToken",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "token",
                    models.CharField(
                        db_index=True,
                        help_text="API token value (auto-generated)",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="Friendly name for this token",
                        max_length=100,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "last_used_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Last time this token was used",
                        null=True,
                    ),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Optional expiration date. Leave empty for no expiration.",
                        null=True,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Inactive tokens cannot be used for API access",
                    ),
                ),
                (
                    "datastore",
                    models.ForeignKey(
                        blank=True,
                        help_text="If set, token only grants access to this datastore. Leave empty for global access.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="api_tokens",
                        to="core.datastore",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_api_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "API token",
                "verbose_name_plural": "API tokens",
                "db_table": "datastore_api_tokens",
                "ordering": ["-created_at"],
            },
        ),
    ]
