"""
DataStore models for script data persistence.
"""

import json
import uuid

from django.conf import settings
from django.db import models


class DataStore(models.Model):
    """
    A named data store that scripts can use for simple key-value storage.
    Data stores are backed by the instance SQLite database.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Unique name for this data store (used in scripts)",
    )

    description = models.TextField(
        blank=True,
        help_text="Optional description of what this data store is used for",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Who created this data store
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_datastores",
    )

    class Meta:
        db_table = "datastores"
        verbose_name = "data store"
        verbose_name_plural = "data stores"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def entry_count(self) -> int:
        """Return the number of entries in this data store."""
        return self.entries.count()


class DataStoreEntry(models.Model):
    """
    A key-value entry in a data store.
    Values are stored as JSON to support various data types.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    datastore = models.ForeignKey(
        DataStore,
        on_delete=models.CASCADE,
        related_name="entries",
    )

    key = models.CharField(
        max_length=255,
        help_text="Unique key within this data store",
    )

    # Value stored as JSON text (supports strings, numbers, lists, dicts, etc.)
    value_json = models.TextField(
        help_text="JSON-encoded value",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datastore_entries"
        verbose_name = "data store entry"
        verbose_name_plural = "data store entries"
        unique_together = [["datastore", "key"]]
        ordering = ["key"]
        indexes = [
            models.Index(fields=["datastore", "key"]),
        ]

    def __str__(self):
        return f"{self.datastore.name}:{self.key}"

    def get_value(self):
        """Deserialize and return the stored value."""
        return json.loads(self.value_json)

    def set_value(self, value) -> None:
        """Serialize and store a value."""
        self.value_json = json.dumps(value)

    def get_display_value(self) -> str:
        """Return a displayable version of the value (truncated if too long)."""
        value = self.value_json
        if len(value) > 100:
            return value[:100] + "..."
        return value
