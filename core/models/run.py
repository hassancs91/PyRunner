"""
Run model for tracking script execution history.
"""

import uuid

from django.conf import settings
from django.db import models

from .script import Script


class Run(models.Model):
    """
    Represents a single execution of a script.
    Tracks timing, output, and status of each run.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        TIMEOUT = "timeout", "Timeout"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        Script,
        on_delete=models.CASCADE,
        related_name="runs",
    )

    # Execution status
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    exit_code = models.IntegerField(
        null=True,
        blank=True,
        help_text="Process exit code (0 = success)",
    )

    # Output capture
    stdout = models.TextField(
        blank=True,
        help_text="Standard output from script execution",
    )
    stderr = models.TextField(
        blank=True,
        help_text="Standard error from script execution",
    )

    # Timing
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When execution started",
    )
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When execution ended",
    )

    # Snapshot of script code at execution time (for audit trail)
    code_snapshot = models.TextField(
        blank=True,
        help_text="Copy of script code at time of execution",
    )

    # Who triggered the run
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_runs",
    )

    # django-q2 task tracking
    task_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="django-q2 task ID for tracking async execution",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "runs"
        verbose_name = "run"
        verbose_name_plural = "runs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["script", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"Run {self.id} - {self.script.name} ({self.status})"

    @property
    def duration(self) -> float | None:
        """Return the duration in seconds, or None if not completed."""
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None

    @property
    def duration_display(self) -> str:
        """Return a human-readable duration string."""
        d = self.duration
        if d is None:
            return "-"
        if d < 60:
            return f"{d:.1f}s"
        minutes = int(d // 60)
        seconds = d % 60
        if minutes < 60:
            return f"{minutes}m {seconds:.0f}s"
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}h {minutes}m"

    @property
    def is_finished(self) -> bool:
        """Check if the run has completed (successfully or not)."""
        return self.status in [
            self.Status.SUCCESS,
            self.Status.FAILED,
            self.Status.TIMEOUT,
            self.Status.CANCELLED,
        ]

    @property
    def is_successful(self) -> bool:
        """Check if the run completed successfully."""
        return self.status == self.Status.SUCCESS

    @property
    def has_output(self) -> bool:
        """Check if there is any output (stdout or stderr)."""
        return bool(self.stdout or self.stderr)

    def get_stdout_preview(self, max_lines: int = 10) -> str:
        """Return a preview of stdout (last N lines)."""
        if not self.stdout:
            return ""
        lines = self.stdout.split("\n")
        if len(lines) <= max_lines:
            return self.stdout
        return "...\n" + "\n".join(lines[-max_lines:])

    def get_stderr_preview(self, max_lines: int = 10) -> str:
        """Return a preview of stderr (last N lines)."""
        if not self.stderr:
            return ""
        lines = self.stderr.split("\n")
        if len(lines) <= max_lines:
            return self.stderr
        return "...\n" + "\n".join(lines[-max_lines:])
