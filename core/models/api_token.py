"""
API Token model for datastore API access.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models


class DataStoreAPIToken(models.Model):
    """
    API token for accessing datastore data via REST API.
    Tokens can be scoped to a single datastore or grant access to all datastores.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The actual token value (64-char URL-safe string)
    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="API token value (auto-generated)",
    )

    # Friendly name to identify this token
    name = models.CharField(
        max_length=100,
        help_text="Friendly name for this token",
    )

    # Optional: Restrict to specific datastore (null = global access to all datastores)
    datastore = models.ForeignKey(
        "DataStore",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_tokens",
        help_text="If set, token only grants access to this datastore. Leave empty for global access.",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this token was used",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional expiration date. Leave empty for no expiration.",
    )

    # Who created this token
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_api_tokens",
    )

    # Active flag for soft-disable
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive tokens cannot be used for API access",
    )

    class Meta:
        db_table = "datastore_api_tokens"
        verbose_name = "API token"
        verbose_name_plural = "API tokens"
        ordering = ["-created_at"]

    def __str__(self):
        if self.datastore:
            return f"{self.name} ({self.datastore.name})"
        return f"{self.name} (global)"

    @staticmethod
    def generate_token() -> str:
        """Generate a secure random API token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)

    def get_masked_token(self) -> str:
        """Return a masked version of the token for display."""
        if len(self.token) <= 12:
            return "*" * len(self.token)
        return f"{self.token[:8]}...{self.token[-4:]}"

    @property
    def is_global(self) -> bool:
        """Return True if this is a global token (not scoped to a datastore)."""
        return self.datastore is None

    @property
    def scope_display(self) -> str:
        """Return a human-readable scope description."""
        if self.datastore:
            return f"Datastore: {self.datastore.name}"
        return "All datastores"
