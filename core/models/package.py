"""
Package operation model for tracking async pip operations.
"""

import uuid

from django.conf import settings
from django.db import models


class PackageOperation(models.Model):
    """
    Tracks async package installation/uninstallation operations.
    Used to provide progress feedback in the UI.
    """

    class Operation(models.TextChoices):
        INSTALL = "install", "Install"
        UNINSTALL = "uninstall", "Uninstall"
        BULK_INSTALL = "bulk_install", "Bulk Install"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    environment = models.ForeignKey(
        "core.Environment",
        on_delete=models.CASCADE,
        related_name="package_operations",
    )

    operation = models.CharField(
        max_length=20,
        choices=Operation.choices,
    )

    package_spec = models.CharField(
        max_length=500,
        help_text="Package specification (e.g., 'requests==2.31.0') or requirements content for bulk install",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    output = models.TextField(
        blank=True,
        help_text="pip stdout",
    )

    error = models.TextField(
        blank=True,
        help_text="pip stderr",
    )

    task_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="django-q2 task ID for tracking",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="package_operations",
    )

    class Meta:
        db_table = "package_operations"
        ordering = ["-created_at"]
        verbose_name = "package operation"
        verbose_name_plural = "package operations"

    def __str__(self):
        return f"{self.get_operation_display()} {self.package_spec[:50]} ({self.status})"

    @property
    def is_finished(self) -> bool:
        """Check if the operation has completed (success or failed)."""
        return self.status in (self.Status.SUCCESS, self.Status.FAILED)

    @property
    def is_successful(self) -> bool:
        """Check if the operation completed successfully."""
        return self.status == self.Status.SUCCESS

    @property
    def duration(self):
        """Return the duration of the operation if completed."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    @property
    def duration_display(self) -> str:
        """Return a human-readable duration string."""
        duration = self.duration
        if not duration:
            return "-"
        total_seconds = int(duration.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}m {seconds}s"
