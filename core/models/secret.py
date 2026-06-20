"""
Secret model for encrypted credential storage.
"""

import uuid

from django.conf import settings
from django.db import models

from .workspace import WorkspaceScopedManager


class Secret(models.Model):
    """
    Stores encrypted secrets (API keys, credentials) that are injected
    as environment variables when scripts run.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Tenancy seam (Phase A): nullable, backfilled to the default workspace.
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="secrets",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Key name - must be uppercase with underscores (e.g., API_KEY, DATABASE_URL)
    # Tenancy Stage 3: unique PER WORKSPACE (not globally), so two workspaces can
    # each own an API_KEY. Enforced by the Meta constraints below (mirrors
    # DataStore.name, Decision 2B).
    key = models.CharField(
        max_length=100,
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
        constraints = [
            # Per-workspace key uniqueness (mirrors DataStore Decision 2B). The
            # partial constraint reproduces "globally unique among un-scoped rows"
            # because NULLs are SQL-distinct in a multi-column UNIQUE.
            models.UniqueConstraint(
                fields=["workspace", "key"],
                name="uniq_secret_workspace_key",
            ),
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(workspace__isnull=True),
                name="uniq_secret_key_when_no_workspace",
            ),
        ]

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
