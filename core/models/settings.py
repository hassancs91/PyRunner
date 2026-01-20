"""
Global application settings model.
"""

from django.conf import settings
from django.db import models


class GlobalSettings(models.Model):
    """
    Singleton model for global application settings.
    Uses get_solo pattern - always ID=1.
    """

    schedules_paused = models.BooleanField(
        default=False,
        help_text="Global pause for all scheduled script executions",
    )

    schedules_paused_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When schedules were paused",
    )

    schedules_paused_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "global_settings"
        verbose_name = "global settings"
        verbose_name_plural = "global settings"

    def __str__(self):
        status = "paused" if self.schedules_paused else "active"
        return f"Global Settings (schedules: {status})"

    def save(self, *args, **kwargs):
        # Enforce singleton pattern
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        """Get or create the singleton settings instance."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
