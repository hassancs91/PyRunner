"""
Schedule models for script execution scheduling.
"""

import uuid

from django.conf import settings
from django.db import models

from .script import Script


class ScriptSchedule(models.Model):
    """
    Represents a schedule configuration for a Script.
    Links to django-q2 Schedule objects for actual task scheduling.
    """

    class RunMode(models.TextChoices):
        MANUAL = "manual", "Manual"
        INTERVAL = "interval", "Interval"
        DAILY = "daily", "Daily"

    class IntervalChoice(models.IntegerChoices):
        FIVE_MINUTES = 5, "Every 5 minutes"
        TEN_MINUTES = 10, "Every 10 minutes"
        FIFTEEN_MINUTES = 15, "Every 15 minutes"
        THIRTY_MINUTES = 30, "Every 30 minutes"
        ONE_HOUR = 60, "Every hour"
        TWO_HOURS = 120, "Every 2 hours"
        SIX_HOURS = 360, "Every 6 hours"
        TWELVE_HOURS = 720, "Every 12 hours"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # One-to-one relationship with Script
    script = models.OneToOneField(
        Script,
        on_delete=models.CASCADE,
        related_name="schedule",
    )

    # Run mode selection
    run_mode = models.CharField(
        max_length=20,
        choices=RunMode.choices,
        default=RunMode.MANUAL,
    )

    # Interval mode configuration
    interval_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        choices=IntervalChoice.choices,
        help_text="Interval in minutes (for interval mode)",
    )

    # Daily mode configuration - stored as JSON array of time strings
    daily_times = models.JSONField(
        default=list,
        blank=True,
        help_text='List of times in HH:MM format, e.g., ["09:00", "18:00"]',
    )

    # Timezone for daily schedules
    timezone = models.CharField(
        max_length=50,
        default="UTC",
        help_text="Timezone for daily schedules (e.g., 'America/New_York')",
    )

    # Schedule state
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this schedule is active (can be paused without deleting)",
    )

    # Link to django-q2 Schedule objects (can be multiple for daily with multiple times)
    # Stored as JSON array of django-q2 Schedule IDs
    q_schedule_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="django-q2 Schedule object IDs",
    )

    # Tracking
    next_run = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Cached next scheduled run time",
    )
    last_scheduled_run = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the schedule last triggered a run",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_schedules",
    )

    class Meta:
        db_table = "script_schedules"
        verbose_name = "script schedule"
        verbose_name_plural = "script schedules"

    def __str__(self):
        return f"Schedule for {self.script.name} ({self.run_mode})"

    @property
    def is_scheduled(self) -> bool:
        """Check if this script has an active schedule (not manual)."""
        return self.run_mode != self.RunMode.MANUAL and self.is_active

    @property
    def schedule_display(self) -> str:
        """Human-readable schedule description."""
        if self.run_mode == self.RunMode.MANUAL:
            return "Manual"
        elif self.run_mode == self.RunMode.INTERVAL:
            # Find the display label from choices
            for value, label in self.IntervalChoice.choices:
                if value == self.interval_minutes:
                    return label
            return f"Every {self.interval_minutes} minutes"
        elif self.run_mode == self.RunMode.DAILY:
            times = ", ".join(self.daily_times) if self.daily_times else "No times set"
            return f"Daily at {times} ({self.timezone})"
        return "Unknown"


class ScheduleHistory(models.Model):
    """
    Tracks changes to script schedules for audit purposes.
    """

    class ChangeType(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        ENABLED = "enabled", "Enabled"
        DISABLED = "disabled", "Disabled"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    schedule = models.ForeignKey(
        ScriptSchedule,
        on_delete=models.CASCADE,
        related_name="history",
    )

    change_type = models.CharField(
        max_length=20,
        choices=ChangeType.choices,
    )

    # Snapshot of schedule configuration at time of change
    previous_config = models.JSONField(
        null=True,
        blank=True,
        help_text="Previous schedule configuration",
    )
    new_config = models.JSONField(
        null=True,
        blank=True,
        help_text="New schedule configuration",
    )

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "schedule_history"
        verbose_name = "schedule history"
        verbose_name_plural = "schedule history entries"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.change_type} - {self.schedule.script.name} at {self.created_at}"
