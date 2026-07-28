"""
Run model for tracking script execution history.
"""

import datetime
import uuid

from django.conf import settings
from django.db import models

from .script import Script
from .workspace import WorkspaceScopedManager


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

    class TriggerType(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"
        API = "api", "API"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        Script,
        on_delete=models.CASCADE,
        related_name="runs",
    )

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="runs",
        help_text="Workspace this run belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

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

    # Which Library revision each attached library was pinned to, stamped at
    # QUEUE time: {library_key: version}. code_snapshot's counterpart for shared
    # code — the executor materializes these exact revisions, so editing a
    # library after queueing cannot change what an already-queued run imports.
    # A tiny int map rather than a content copy: storage scales with edits, not
    # runs. NULL (old runs, scripts with no libraries) => nothing to materialize.
    library_versions = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Library revisions pinned at queue time: {library_key: version}.",
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

    # OS process tracking (for force-kill). Set while the script subprocess is
    # alive, cleared on completion. Used to kill the job's process tree without
    # touching the django-q worker that spawned it.
    pid = models.IntegerField(
        null=True,
        blank=True,
        help_text="OS PID of the running script subprocess (for kill).",
    )

    # How this run was triggered
    trigger_type = models.CharField(
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.MANUAL,
        help_text="How this run was triggered",
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

    # Grace beyond a run's own timeout before a still-'running' row is declared
    # dead. A live worker HARD-kills the subprocess at timeout_seconds and
    # finalizes within seconds (the per-task cushion is +60s), so five minutes
    # past the deadline nothing legitimate can still be executing.
    RECONCILE_GRACE_SECONDS = 300
    # How old a PENDING run must be before it counts as lost — but only while
    # workers are demonstrably alive: a live cluster re-delivers unacknowledged
    # tasks within its `retry` window, so a day without pickup means the queued
    # task no longer exists (e.g. a worker was killed between queue and ack).
    RECONCILE_PENDING_AFTER = datetime.timedelta(hours=24)

    @classmethod
    def reconcile_stale(cls) -> int:
        """Fail runs whose worker died without finalizing them.

        A worker killed while busy (container stop SIGKILLs workers by design —
        see entrypoint.sh — plus OOM kills and crashes) leaves its Run at
        'running' forever: stdout is never captured, force-stop has no live pid,
        and anything gating on "a run is in flight" (plugin concurrency guards,
        queue checks) stays blocked. Nothing in-process can catch that, so this
        reconciler runs out-of-band: every minute on the worker heartbeat and on
        the runs list page. It is the Run counterpart of
        ``PackageOperation.reconcile_stale``.

        Status-only by design: it never kills OS processes. A detached script
        may genuinely still be running, but its recorded pid may also have been
        recycled to an innocent process — reporting honestly beats killing
        blind.

        Returns the number of runs reconciled.
        """
        from django.utils import timezone

        from core.models.settings import GlobalSettings

        now = timezone.now()
        reconciled = 0

        # RUNNING: dead once well past started_at + the script's own timeout +
        # grace. The deadline is per-run, and the candidate set is tiny by
        # construction, so iterate instead of fighting cross-database duration
        # arithmetic. The conditional UPDATE re-checks status so a worker
        # finalizing concurrently (or an external cancel) always wins.
        grace = datetime.timedelta(seconds=cls.RECONCILE_GRACE_SECONDS)
        candidates = cls.objects.filter(
            status=cls.Status.RUNNING,
            started_at__isnull=False,
            started_at__lt=now - grace,
        ).select_related("script")
        for run in candidates:
            deadline = (
                run.started_at
                + datetime.timedelta(seconds=run.script.timeout_seconds)
                + grace
            )
            if now < deadline:
                continue
            marker = (
                "\n[RECONCILED: the worker executing this run stopped before "
                "finalizing it (e.g. a worker restart or kill mid-run), so its "
                "output was never captured. Marked failed "
                f"{cls.RECONCILE_GRACE_SECONDS}s past the script's "
                f"{run.script.timeout_seconds}s timeout. A detached script "
                "process may have kept running after the worker died.]"
            )
            reconciled += cls.objects.filter(
                pk=run.pk, status=cls.Status.RUNNING
            ).update(
                status=cls.Status.FAILED,
                exit_code=-1,
                stderr=(run.stderr or "") + marker,
                ended_at=now,
                pid=None,
            )

        # PENDING: only reconciled while workers are alive — with the cluster
        # down, "queued" is still the truth and these runs will execute when it
        # returns.
        if GlobalSettings.get_settings().worker_is_alive():
            reconciled += cls.objects.filter(
                status=cls.Status.PENDING,
                created_at__lt=now - cls.RECONCILE_PENDING_AFTER,
            ).update(
                status=cls.Status.FAILED,
                exit_code=-1,
                stderr=(
                    "[RECONCILED: queued for over 24 hours while workers were "
                    "alive — the queued task was lost before pickup (e.g. a "
                    "worker restart between queue and execution). Re-run the "
                    "script.]"
                ),
                ended_at=now,
            )

        return reconciled

    @property
    def duration(self) -> float | None:
        """Return the duration in seconds, or None if not completed."""
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None

    @property
    def duration_display(self) -> str:
        """Return a human-readable duration string."""
        from core.formatting import format_duration

        return format_duration(self.duration)

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
