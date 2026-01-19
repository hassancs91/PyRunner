"""
Script model for user-created Python scripts.
"""

import uuid

from django.conf import settings
from django.db import models

from .environment import Environment


class Script(models.Model):
    """
    Represents a Python script that can be executed.
    Scripts are associated with an environment and can be run manually or on schedule.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # The actual Python code
    code = models.TextField(help_text="Python code to execute")

    # Execution settings
    environment = models.ForeignKey(
        Environment,
        on_delete=models.PROTECT,
        related_name="scripts",
        help_text="Python environment to use for execution",
    )
    timeout_seconds = models.PositiveIntegerField(
        default=300,  # 5 minutes default
        help_text="Maximum execution time in seconds (default: 5 minutes)",
    )

    # Status
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether this script can be executed",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scripts",
    )

    class Meta:
        db_table = "scripts"
        verbose_name = "script"
        verbose_name_plural = "scripts"
        ordering = ["-updated_at"]

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.name} ({status})"

    @property
    def last_run(self):
        """Return the most recent run for this script."""
        return self.runs.order_by("-created_at").first()

    @property
    def last_successful_run(self):
        """Return the most recent successful run for this script."""
        return self.runs.filter(status="success").order_by("-created_at").first()

    @property
    def run_count(self) -> int:
        """Return the total number of runs for this script."""
        return self.runs.count()

    @property
    def success_rate(self) -> float | None:
        """Return the success rate as a percentage, or None if no runs."""
        total = self.run_count
        if total == 0:
            return None
        successful = self.runs.filter(status="success").count()
        return (successful / total) * 100

    def get_code_preview(self, max_lines: int = 5) -> str:
        """Return a preview of the script code (first N lines)."""
        lines = self.code.split("\n")[:max_lines]
        preview = "\n".join(lines)
        if len(self.code.split("\n")) > max_lines:
            preview += "\n..."
        return preview
