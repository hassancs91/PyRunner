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

    class EmailBackend(models.TextChoices):
        DISABLED = "disabled", "Disabled"
        SMTP = "smtp", "SMTP"
        RESEND = "resend", "Resend API"

    # Schedule settings
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

    # Email notification settings
    email_backend = models.CharField(
        max_length=20,
        choices=EmailBackend.choices,
        default=EmailBackend.DISABLED,
        help_text="Email backend for notifications",
    )

    # SMTP configuration
    smtp_host = models.CharField(
        max_length=255,
        blank=True,
        help_text="SMTP server hostname",
    )
    smtp_port = models.PositiveIntegerField(
        default=587,
        help_text="SMTP server port",
    )
    smtp_username = models.CharField(
        max_length=255,
        blank=True,
        help_text="SMTP username",
    )
    smtp_password_encrypted = models.TextField(
        blank=True,
        help_text="SMTP password (encrypted)",
    )
    smtp_use_tls = models.BooleanField(
        default=True,
        help_text="Use TLS for SMTP connection",
    )
    smtp_from_email = models.EmailField(
        blank=True,
        help_text="From email address for SMTP",
    )

    # Resend configuration
    resend_api_key_encrypted = models.TextField(
        blank=True,
        help_text="Resend API key (encrypted)",
    )
    resend_from_email = models.EmailField(
        blank=True,
        help_text="From email address for Resend",
    )

    # Default notification email
    default_notification_email = models.EmailField(
        blank=True,
        help_text="Default email address for all notifications",
    )

    # General Settings
    instance_name = models.CharField(
        max_length=100,
        default="PyRunner",
        blank=True,
        help_text="Instance name displayed in header and emails",
    )
    timezone = models.CharField(
        max_length=50,
        default="UTC",
        help_text="Default timezone for the instance",
    )

    class DateFormat(models.TextChoices):
        ISO = "YYYY-MM-DD", "YYYY-MM-DD (ISO)"
        US = "MM/DD/YYYY", "MM/DD/YYYY (US)"
        EU = "DD/MM/YYYY", "DD/MM/YYYY (EU)"
        DOT = "DD.MM.YYYY", "DD.MM.YYYY"

    date_format = models.CharField(
        max_length=20,
        choices=DateFormat.choices,
        default=DateFormat.ISO,
        help_text="Date display format",
    )

    class TimeFormat(models.TextChoices):
        H24 = "24h", "24-hour"
        H12 = "12h", "12-hour"

    time_format = models.CharField(
        max_length=10,
        choices=TimeFormat.choices,
        default=TimeFormat.H24,
        help_text="Time display format",
    )

    # Log Retention Settings
    retention_days = models.PositiveIntegerField(
        default=0,
        help_text="Delete runs older than X days (0 = keep forever)",
    )
    retention_count = models.PositiveIntegerField(
        default=0,
        help_text="Keep last X runs per script (0 = unlimited)",
    )
    auto_cleanup_enabled = models.BooleanField(
        default=False,
        help_text="Automatically clean up old runs daily",
    )
    last_cleanup_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last cleanup was performed",
    )

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
