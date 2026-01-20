"""
Secret model for encrypted credential storage.
"""

import uuid

from django.conf import settings
from django.db import models


class Secret(models.Model):
    """
    Stores encrypted secrets (API keys, credentials) that are injected
    as environment variables when scripts run.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Key name - must be uppercase with underscores (e.g., API_KEY, DATABASE_URL)
    key = models.CharField(
        max_length=100,
        unique=True,
        help_text="Environment variable name (uppercase, underscores allowed)",
    )

    # Encrypted value - stores the Fernet-encrypted bytes as base64 string
    encrypted_value = models.TextField(
        help_text="Fernet-encrypted secret value",
    )

    # Optional description to help remember what this secret is for
    description = models.TextField(
        blank=True,
        help_text="Optional description of what this secret is used for",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Who created this secret
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_secrets",
    )

    class Meta:
        db_table = "secrets"
        verbose_name = "secret"
        verbose_name_plural = "secrets"
        ordering = ["key"]

    def __str__(self):
        return self.key

    def get_masked_value(self) -> str:
        """
        Return a masked preview of the decrypted value.
        Shows first 3 and last 3 characters with ... in between.
        Example: "sk-abc123xyz789" -> "sk-...789"
        """
        from core.services import EncryptionService

        try:
            value = EncryptionService.decrypt(self.encrypted_value)
            if len(value) <= 8:
                return "*" * len(value)
            return f"{value[:3]}...{value[-3:]}"
        except Exception:
            return "[decryption error]"

    def get_decrypted_value(self) -> str:
        """Return the decrypted secret value."""
        from core.services import EncryptionService

        return EncryptionService.decrypt(self.encrypted_value)

    def set_value(self, plaintext: str) -> None:
        """Encrypt and store a new value."""
        from core.services import EncryptionService

        self.encrypted_value = EncryptionService.encrypt(plaintext)
