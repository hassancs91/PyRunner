"""
Schedule models for script execution scheduling.
"""

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .script import Script
from .workspace import WorkspaceScopedManager


class ScriptSchedule(models.Model):
    """
    Represents a schedule configuration for a Script.
    Links to django-q2 Schedule objects for actual task scheduling.
    """

    class RunMode(models.TextChoices):
        MANUAL = "manual", "Manual"
        INTERVAL = "interval", "Interval"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        CRON = "cron", "Cron expression"
        YEARLY = "yearly", "Yearly"

    # Interval bounds. The ceiling is django-q2's: ``Schedule.minutes`` is a
    # PositiveSmallIntegerField, so anything above 32767 would fail at sync time
    # rather than at save time. Quick-pick presets are a UI convenience only;
    # any value in range is valid.
    INTERVAL_MIN_MINUTES = 1
    INTERVAL_MAX_MINUTES = 32767
    INTERVAL_PRESETS = (
        (5, "5 min"),
        (15, "15 min"),
        (30, "30 min"),
        (60, "1 hour"),
        (360, "6 hours"),
        (1440, "24 hours"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # One-to-one relationship with Script
    script = models.OneToOneField(
        Script,
        on_delete=models.CASCADE,
        related_name="schedule",
    )

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="schedules",
        help_text="Workspace this schedule belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

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
        validators=[
            MinValueValidator(INTERVAL_MIN_MINUTES),
            MaxValueValidator(INTERVAL_MAX_MINUTES),
        ],
        help_text="Interval in minutes (for interval mode)",
    )

    # Daily mode configuration - stored as JSON array of time strings
    daily_times = models.JSONField(
        default=list,
        blank=True,
        help_text='List of times in HH:MM format, e.g., ["09:00", "18:00"]',
    )

    # Timezone for scheduled runs
    timezone = models.CharField(
        max_length=50,
        default="UTC",
        help_text="Timezone for scheduled runs (e.g., 'America/New_York')",
    )

    # Weekly mode configuration
    weekly_days = models.JSONField(
        default=list,
        blank=True,
        help_text='Days of week [0-6] where 0=Monday, e.g., [0, 2, 4] for Mon/Wed/Fri',
    )
    weekly_times = models.JSONField(
        default=list,
        blank=True,
        help_text='List of times in HH:MM format for weekly mode',
    )

    # Monthly mode configuration
    monthly_days = models.JSONField(
        default=list,
        blank=True,
        help_text='Days of month [1-31], e.g., [1, 15] for 1st and 15th',
    )
    monthly_times = models.JSONField(
        default=list,
        blank=True,
        help_text='List of times in HH:MM format for monthly mode',
    )

    # Cron mode configuration - a raw 5-field cron expression
    cron_expression = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text='Raw cron expression, e.g. "0 9 * * 1-5" (minute hour day-of-month month day-of-week)',
    )

    # Yearly mode configuration (one calendar date and wall-clock time)
    yearly_month = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Month for yearly mode (1-12)"
    )
    yearly_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Day of month for yearly mode (1-31)"
    )
    yearly_time = models.CharField(
        max_length=5, blank=True, default="", help_text="Time for yearly mode (HH:MM)"
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

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    @staticmethod
    def describe_interval(minutes) -> str:
        """Human-readable "Every …" label for an interval in minutes.

        Whole hours and days read as such ("Every 2 hours", "Every day");
        anything else stays in minutes ("Every 7 minutes", "Every 90 minutes").
        """
        if not minutes:
            return "Every ? minutes"
        if minutes % 1440 == 0:
            days = minutes // 1440
            return "Every day" if days == 1 else f"Every {days} days"
        if minutes % 60 == 0:
            hours = minutes // 60
            return "Every hour" if hours == 1 else f"Every {hours} hours"
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"

    @property
    def schedule_display(self) -> str:
        """Human-readable schedule description."""
        if self.run_mode == self.RunMode.MANUAL:
            return "Manual"
        elif self.run_mode == self.RunMode.INTERVAL:
            return self.describe_interval(self.interval_minutes)
        elif self.run_mode == self.RunMode.DAILY:
            times = ", ".join(self.daily_times) if self.daily_times else "No times set"
            return f"Daily at {times} ({self.timezone})"
        elif self.run_mode == self.RunMode.WEEKLY:
            days = ", ".join(self.DAY_NAMES[d] for d in sorted(self.weekly_days)) if self.weekly_days else "No days set"
            times = ", ".join(self.weekly_times) if self.weekly_times else "No times set"
            return f"Weekly on {days} at {times} ({self.timezone})"
        elif self.run_mode == self.RunMode.MONTHLY:
            days = ", ".join(str(d) for d in sorted(self.monthly_days)) if self.monthly_days else "No days set"
            times = ", ".join(self.monthly_times) if self.monthly_times else "No times set"
            return f"Monthly on day {days} at {times} ({self.timezone})"
        elif self.run_mode == self.RunMode.CRON:
            expr = self.cron_expression or "No expression set"
            return f"Cron: {expr}"
        elif self.run_mode == self.RunMode.YEARLY:
            if self.yearly_month and self.yearly_day and self.yearly_time:
                return f"Yearly on {self.yearly_month:02d}/{self.yearly_day:02d} at {self.yearly_time} ({self.timezone})"
            return "Yearly (not configured)"
        return "Unknown"

    def config_snapshot(self) -> dict:
        """Serialize the schedule's mode configuration for audit history diffs.

        Includes EVERY mode's fields (interval/daily/weekly/monthly/yearly), not just the
        active mode's — ``ScheduleHistory`` compares the before/after snapshots to
        decide whether a change occurred, so a weekly- or monthly-only edit used to
        be invisible (and wrote no history entry) when those fields were omitted.
        """
        return {
            "run_mode": self.run_mode,
            "interval_minutes": self.interval_minutes,
            "daily_times": self.daily_times,
            "weekly_days": self.weekly_days,
            "weekly_times": self.weekly_times,
            "monthly_days": self.monthly_days,
            "monthly_times": self.monthly_times,
            "cron_expression": self.cron_expression,
            "yearly_month": self.yearly_month,
            "yearly_day": self.yearly_day,
            "yearly_time": self.yearly_time,
            "timezone": self.timezone,
            "is_active": self.is_active,
        }


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
