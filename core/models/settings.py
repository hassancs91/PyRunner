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
        help_text="Display timezone for the console; scheduled backup times are interpreted in it too",
    )

    # Security Settings
    admin_url_slug = models.CharField(
        max_length=100,
        default="django-admin",
        help_text="URL path for Django admin interface (requires restart)",
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

    # Worker heartbeat for status detection
    worker_heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last heartbeat from django-q workers",
    )

    # Worker Settings (Q_CLUSTER configuration)
    q_workers = models.PositiveIntegerField(
        default=2,
        help_text="Number of worker processes for task queue",
    )
    q_timeout = models.PositiveIntegerField(
        default=600,
        help_text="Task timeout in seconds (0 for no timeout)",
    )
    q_retry = models.PositiveIntegerField(
        default=660,
        help_text="Seconds before a task is retried after timeout",
    )
    q_queue_limit = models.PositiveIntegerField(
        default=20,
        help_text="Maximum number of tasks in the queue",
    )
    worker_settings_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When worker settings were last updated (requires restart)",
    )

    # Setup wizard tracking
    setup_completed = models.BooleanField(
        default=False,
        help_text="Whether initial setup has been completed",
    )
    setup_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the initial setup was completed",
    )

    # Registration control
    allow_registration = models.BooleanField(
        default=True,
        help_text="Dormant/reserved: registration is invite-only by design (there is "
        "no open-registration UI). Auto-set False after the first user for legacy "
        "reasons; not consulted by any live registration path.",
    )

    # Object storage — endpoint/credential/bucket config lives in StorageConnection
    # rows (an instance can hold several). Backups use the row flagged `is_default`;
    # plugin assets use the FK below. The historical single `s3_*` connection block
    # data-migrated into one default row in 0053_storage_connections.
    assets_storage = models.ForeignKey(
        "core.StorageConnection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Connection plugins read and write through StorageAPI. Blank = the "
        "storage seam is unavailable (fails closed). Deliberately does NOT fall back "
        "to the backup default — that would put plugin objects in the backup bucket.",
    )

    # S3 Scheduled Backup Configuration
    class S3BackupSchedule(models.TextChoices):
        DISABLED = "disabled", "Disabled"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    s3_backup_enabled = models.BooleanField(
        default=False,
        help_text="Enable scheduled backups to S3",
    )
    s3_backup_schedule = models.CharField(
        max_length=20,
        choices=S3BackupSchedule.choices,
        default=S3BackupSchedule.DISABLED,
        help_text="Backup schedule frequency",
    )
    s3_backup_time = models.TimeField(
        default="02:00",
        help_text="Time of day to run backup (in instance timezone)",
    )
    s3_backup_day = models.PositiveSmallIntegerField(
        default=0,
        help_text="Day of week for weekly backups (0=Monday, 6=Sunday)",
    )
    s3_backup_prefix = models.CharField(
        max_length=255,
        blank=True,
        default="pyrunner-backups/",
        help_text="S3 key prefix for backup files",
    )
    s3_backup_retention_count = models.PositiveIntegerField(
        default=7,
        help_text="Number of backups to keep in S3 (0 = keep all)",
    )

    # Backup content options
    s3_backup_include_runs = models.BooleanField(
        default=False,
        help_text="Include run history in scheduled backups",
    )
    s3_backup_max_runs = models.PositiveIntegerField(
        default=1000,
        help_text="Maximum runs to include in backup",
    )
    s3_backup_include_datastores = models.BooleanField(
        default=True,
        help_text="Include datastores in scheduled backups",
    )

    # Backup tracking fields
    s3_backup_last_run_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last scheduled backup ran",
    )
    s3_backup_last_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Status of last backup (success/failed)",
    )
    s3_backup_last_error = models.TextField(
        blank=True,
        default="",
        help_text="Error message from last failed backup",
    )
    s3_backup_last_size = models.BigIntegerField(
        default=0,
        help_text="Size of last backup in bytes",
    )

    # AI Integration — provider profiles live in AIProvider (all keys saved);
    # this FK selects which one scripts, Py AI, and tests use. `claude_enabled`
    # keeps its historical column name but is the master AI on/off switch.
    claude_enabled = models.BooleanField(
        default=False,
        help_text="Make AI available to scripts (via the pyrunner_ai helper)",
    )
    active_ai_provider = models.ForeignKey(
        "core.AIProvider",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Provider profile used by scripts, Py AI, and connection tests",
    )

    # Py AI — built-in read-only assistant (standalone feature; uses the active
    # AI provider above). Beta: introspection-only.
    pyai_enabled = models.BooleanField(
        default=False,
        help_text="Enable the Py AI assistant (requires Claude to be configured).",
    )
    pyai_model = models.CharField(
        max_length=100,
        blank=True,
        help_text="Optional model id for Py AI (must match the active provider). "
        "Blank = the active provider's default model.",
    )
    pyai_system_prompt = models.TextField(
        blank=True,
        help_text="Optional extra system instruction appended to Py AI's prompt.",
    )

    # Update check (compares running version against the latest GitHub release tag)
    update_latest_version = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Latest PyRunner version seen on GitHub (populated by the update check)",
    )
    update_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the update check last ran successfully",
    )

    # Google reCAPTCHA v2 (login protection)
    recaptcha_enabled = models.BooleanField(
        default=False,
        help_text="Require Google reCAPTCHA v2 on the login page",
    )
    recaptcha_site_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="reCAPTCHA v2 site key (public)",
    )
    recaptcha_secret_key_encrypted = models.TextField(
        blank=True,
        help_text="reCAPTCHA v2 secret key (encrypted)",
    )

    # Execution / Isolation — the script-execution sandbox (FOUNDATIONS Seam 2).
    # Fully dashboard-managed; resolved per-run at execution time (no restart),
    # no env reliance. All defaults reproduce today's behavior byte-for-byte:
    # mode off + every rlimit 0 (unlimited) => the executor carries no limits.
    class SandboxMode(models.TextChoices):
        OFF = "off", "Off"
        OPTIONAL = "optional", "Optional"
        REQUIRED = "required", "Required"

    class SandboxCapability(models.TextChoices):
        UNKNOWN = "unknown", "Not yet tested"
        FULL = "full", "Full sandbox"
        RLIMITS_ONLY = "rlimits_only", "Resource limits only"
        NONE = "none", "Unavailable"

    sandbox_default = models.CharField(
        max_length=20,
        choices=SandboxMode.choices,
        default=SandboxMode.OFF,
        help_text="Instance-wide isolation default (off = today's behavior). "
        "Gates the filesystem/network sandbox (SandboxedSubprocessBackend); "
        "the resource limits below apply independently of this setting.",
    )
    sandbox_fail_closed = models.BooleanField(
        default=False,
        help_text="When the sandbox is required but unavailable on the host, fail "
        "the run instead of degrading to a lower tier with a warning.",
    )
    # Per-run resource caps (POSIX RLIMIT_*; a no-op on Windows). 0 = unlimited.
    sandbox_rlimit_memory_mb = models.PositiveIntegerField(
        default=0,
        help_text="Per-run memory cap in MB (RLIMIT_AS). 0 = unlimited.",
    )
    sandbox_rlimit_cpu_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Per-run CPU-time cap in seconds (RLIMIT_CPU). 0 = unlimited.",
    )
    sandbox_rlimit_nproc = models.PositiveIntegerField(
        default=0,
        help_text="Per-run process/thread cap (RLIMIT_NPROC, fork-bomb guard). 0 = unlimited.",
    )
    sandbox_rlimit_fsize_mb = models.PositiveIntegerField(
        default=0,
        help_text="Per-run max single-file write size in MB (RLIMIT_FSIZE). 0 = unlimited.",
    )
    sandbox_capability = models.CharField(
        max_length=20,
        choices=SandboxCapability.choices,
        default=SandboxCapability.UNKNOWN,
        help_text="Cached result of the host sandbox capability probe "
        "(populated by the Test button / sandbox_check command).",
    )
    sandbox_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the host sandbox capability was last probed.",
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

    def worker_restart_required(self) -> bool:
        """Check if worker restart is required due to pending settings changes."""
        if not self.worker_settings_updated_at or not self.worker_heartbeat_at:
            return False
        return self.worker_settings_updated_at > self.worker_heartbeat_at

    # Seconds since the last worker heartbeat after which a worker counts as down.
    # ONE window, shared by worker_is_alive() (inbound-webhook fast-fail) and the
    # dashboard worker-status card (system_info_service), so they never disagree.
    # The heartbeat task runs every minute → 3 min = two missed beats = genuinely down.
    WORKER_HEARTBEAT_TIMEOUT_SECONDS = 180

    def worker_is_alive(self, threshold_seconds: int | None = None) -> bool:
        """Whether a django-q worker has heartbeat-ed recently.

        Used by the inbound webhook to fast-fail with a friendly "asleep" reply
        instead of silently enqueuing a message no worker will ever process.
        Defaults to the shared ``WORKER_HEARTBEAT_TIMEOUT_SECONDS`` window.
        """
        from django.utils import timezone

        if threshold_seconds is None:
            threshold_seconds = self.WORKER_HEARTBEAT_TIMEOUT_SECONDS
        if not self.worker_heartbeat_at:
            return False
        return (timezone.now() - self.worker_heartbeat_at).total_seconds() < threshold_seconds

    def recaptcha_active(self) -> bool:
        """True when reCAPTCHA is enabled and both keys are configured."""
        return bool(
            self.recaptcha_enabled
            and self.recaptcha_site_key
            and self.recaptcha_secret_key_encrypted
        )

    def sandbox_rlimits_configured(self) -> bool:
        """True when any per-run resource cap is set (the rlimits floor is active)."""
        return bool(
            self.sandbox_rlimit_memory_mb
            or self.sandbox_rlimit_cpu_seconds
            or self.sandbox_rlimit_nproc
            or self.sandbox_rlimit_fsize_mb
        )
