"""
Script model for user-created Python scripts.
"""

import secrets
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

    # Webhook
    webhook_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique token for webhook URL (auto-generated)",
    )

    # Notification settings
    class NotifyOn(models.TextChoices):
        NEVER = "never", "Never"
        FAILURE = "failure", "On Failure"
        SUCCESS = "success", "On Success"
        BOTH = "both", "On Success and Failure"

    notify_on = models.CharField(
        max_length=20,
        choices=NotifyOn.choices,
        default=NotifyOn.NEVER,
        help_text="When to send notifications for this script",
    )
    notify_email = models.EmailField(
        blank=True,
        help_text="Override email for this script (uses global default if empty)",
    )
    notify_webhook_url = models.URLField(
        blank=True,
        max_length=500,
        help_text="URL to POST notification webhooks to",
    )
    notify_webhook_enabled = models.BooleanField(
        default=False,
        help_text="Enable webhook notifications for this script",
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

    @staticmethod
    def generate_webhook_token() -> str:
        """Generate a secure random webhook token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)  # 48 bytes = 64 chars in base64

    def create_webhook_token(self) -> str:
        """Create and save a new webhook token for this script."""
        self.webhook_token = self.generate_webhook_token()
        self.save(update_fields=["webhook_token", "updated_at"])
        return self.webhook_token

    def regenerate_webhook_token(self) -> str:
        """Regenerate the webhook token, invalidating the old one."""
        return self.create_webhook_token()

    def clear_webhook_token(self) -> None:
        """Remove the webhook token, disabling webhook access."""
        self.webhook_token = None
        self.save(update_fields=["webhook_token", "updated_at"])

    @property
    def has_webhook(self) -> bool:
        """Check if this script has a webhook token configured."""
        return bool(self.webhook_token)
