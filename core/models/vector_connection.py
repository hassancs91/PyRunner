"""
Vector store connections (vector seam).

An instance-global registry row per vector backend, shaped like
``SecretProvider`` / ``StorageConnection``: ``provider_type`` picks the adapter,
``config`` holds the non-secret settings (Qdrant: ``url``, ``collection_prefix``;
Local: nothing), ``credentials_encrypted`` the API key. A migration seeds one
**Local** row (enabled, default) so a fresh instance can create a collection with
zero setup; Qdrant rows are added from Services → Vector stores once
``PYRUNNER_ALLOW_PRIVATE_NETWORK`` (or a public URL) makes the server reachable.

Exactly one row may be ``is_default`` — enforced both in ``save()`` (clear the
others first, in the same transaction) and by a partial unique constraint, so a
second default can't appear through any code path.
"""

import json
import uuid

from django.db import models, transaction


class VectorConnection(models.Model):
    class ProviderType(models.TextChoices):
        LOCAL = "local", "Local (SQLite + numpy)"
        QDRANT = "qdrant", "Qdrant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_type = models.CharField(
        max_length=20,
        choices=ProviderType.choices,
        default=ProviderType.LOCAL,
        help_text="Which vector backend this connection talks to",
    )
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Display name, e.g. 'Local' or 'Qdrant (compose)'",
    )
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Non-secret backend settings (Qdrant: url, collection_prefix)",
    )
    credentials_encrypted = models.TextField(
        blank=True,
        help_text="Fernet-encrypted JSON of the backend's credential fields (Qdrant: api_key)",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Switch off without losing the configuration",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="The connection new collections use unless another is chosen. Exactly one row carries this.",
    )
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this connection last tested successfully",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vector_connections"
        ordering = ["name"]
        verbose_name = "vector connection"
        verbose_name_plural = "vector connections"
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="uniq_vector_connection_single_default",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_provider_type_display()})"

    def save(self, *args, **kwargs):
        """Persist, keeping at most one ``is_default`` row.

        The others are cleared BEFORE this row is written so the partial unique
        constraint never sees two defaults mid-transaction.
        """
        with transaction.atomic():
            if self.is_default:
                VectorConnection.objects.filter(is_default=True).exclude(pk=self.pk).update(
                    is_default=False
                )
            super().save(*args, **kwargs)

    @classmethod
    def get_default(cls):
        """The default connection (enabled), or None when nothing usable is configured."""
        return cls.objects.filter(is_default=True, enabled=True).first()

    @property
    def is_local(self) -> bool:
        return self.provider_type == self.ProviderType.LOCAL

    @property
    def url(self) -> str:
        return (self.config or {}).get("url", "")

    @property
    def collection_prefix(self) -> str:
        return (self.config or {}).get("collection_prefix", "")

    def get_credentials(self) -> dict:
        """Decrypt and return the credential fields as a dict (``{}`` if unset)."""
        if not self.credentials_encrypted:
            return {}
        from core.services import EncryptionService

        return json.loads(EncryptionService.decrypt(self.credentials_encrypted))

    def set_credentials(self, creds: dict) -> None:
        """Fernet-encrypt and store the credential fields (blank blob if empty)."""
        from core.services import EncryptionService

        creds = {k: v for k, v in (creds or {}).items() if v}
        self.credentials_encrypted = EncryptionService.encrypt(json.dumps(creds)) if creds else ""
