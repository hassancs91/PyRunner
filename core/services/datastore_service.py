"""
Service for datastore statistics and operations.
"""

from django.db.models import Sum
from django.db.models.functions import Coalesce, Length

from core.models import DataStore, DataStoreEntry
from core.services.environment_service import EnvironmentService


class DatastoreService:
    """Service for datastore statistics and operations."""

    @classmethod
    def get_datastores_with_stats(cls):
        """
        Get all datastores annotated with size.

        Returns:
            QuerySet of DataStore objects with annotations:
            - size_bytes: Total size of value_json fields in bytes

        Note: entry_count is provided by the DataStore model property.
        """
        return DataStore.objects.annotate(
            size_bytes=Coalesce(Sum(Length("entries__value_json")), 0),
        ).order_by("name")

    @classmethod
    def get_total_size(cls) -> int:
        """
        Get total size of all datastore entries in bytes.

        Returns:
            Total size in bytes
        """
        result = DataStoreEntry.objects.aggregate(
            total=Coalesce(Sum(Length("value_json")), 0)
        )
        return result["total"]

    @classmethod
    def format_size(cls, size_bytes: int) -> str:
        """Format size in human-readable format."""
        return EnvironmentService.format_disk_usage(size_bytes)
