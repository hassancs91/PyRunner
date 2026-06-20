"""
Dashboard service for gathering statistics and widgets data.
"""

from datetime import timedelta
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone


class DashboardService:
    """Service for gathering dashboard statistics and widget data."""

    @classmethod
    def get_statistics(cls, workspace=None) -> dict:
        """
        Get all dashboard statistics.

        Args:
            workspace: when given, script/run counts are scoped to it (tenancy
                Stage 3). ``None`` keeps the legacy instance-wide behavior.

        Returns dict with:
        - total_scripts: Total number of scripts
        - active_scripts: Scripts with is_enabled=True
        - runs_today: Runs created today
        - runs_this_week: Runs created in last 7 days
        - success_rate: Percentage of successful runs (or None if no runs)
        - queue_size: Number of tasks in queue
        """
        from django_q.models import OrmQ

        from core.models import Run, Script

        now = timezone.now()
        today = now.date()
        week_ago = now - timedelta(days=7)

        scripts = Script.objects
        runs = Run.objects
        if workspace is not None:
            scripts = scripts.for_workspace(workspace)
            runs = runs.for_workspace(workspace)

        # Script counts
        total_scripts = scripts.count()
        active_scripts = scripts.filter(is_enabled=True).count()

        # Run counts
        total_runs = runs.count()
        runs_today = runs.filter(created_at__date=today).count()
        runs_this_week = runs.filter(created_at__gte=week_ago).count()

        # Success rate
        success_rate = None
        if total_runs > 0:
            success_count = runs.filter(status=Run.Status.SUCCESS).count()
            success_rate = round((success_count / total_runs) * 100, 1)

        # Queue size
        try:
            queue_size = OrmQ.objects.count()
        except Exception:
            queue_size = 0

        return {
            "total_scripts": total_scripts,
            "active_scripts": active_scripts,
            "runs_today": runs_today,
            "runs_this_week": runs_this_week,
            "success_rate": success_rate,
            "queue_size": queue_size,
        }

    @classmethod
    def get_recent_failures(cls, limit: int = 5, workspace=None) -> QuerySet:
        """
        Get recent failed and timeout runs.

        Args:
            limit: Maximum number of runs to return
            workspace: when given, scope to the active workspace (tenancy Stage 3)

        Returns:
            QuerySet of Run objects ordered by most recent
        """
        from core.models import Run

        runs = Run.objects
        if workspace is not None:
            runs = runs.for_workspace(workspace)
        return (
            runs.filter(status__in=[Run.Status.FAILED, Run.Status.TIMEOUT])
            .select_related("script")
            .order_by("-created_at")[:limit]
        )

    @classmethod
    def get_upcoming_scheduled_runs(cls, limit: int = 5, workspace=None) -> QuerySet:
        """
        Get upcoming scheduled script runs.

        Args:
            limit: Maximum number of schedules to return
            workspace: when given, scope to the active workspace (tenancy Stage 3).
                Schedules are scoped by ``script__workspace`` — the authoritative
                source — since a schedule's own ``workspace`` FK may predate
                stamping.

        Returns:
            QuerySet of ScriptSchedule objects with upcoming runs
        """
        from core.models import ScriptSchedule

        now = timezone.now()

        schedules = ScriptSchedule.objects.filter(
            next_run__isnull=False,
            next_run__gt=now,
            is_active=True,
            run_mode__in=[ScriptSchedule.RunMode.INTERVAL, ScriptSchedule.RunMode.DAILY],
        )
        if workspace is not None:
            schedules = schedules.filter(script__workspace=workspace)
        return schedules.select_related("script").order_by("next_run")[:limit]

    @classmethod
    def get_system_health(cls) -> dict:
        """
        Get system health status.

        Returns dict with:
        - worker_status: str ("running", "stopped", "unknown")
        - worker_status_text: str (human-readable)
        - schedules_paused: bool
        - queue_size: int
        """
        from core.models import GlobalSettings
        from core.services.system_info_service import SystemInfoService

        worker_info = SystemInfoService.get_worker_status()
        global_settings = GlobalSettings.get_settings()

        return {
            "worker_status": worker_info["status"],
            "worker_status_text": worker_info["status_text"],
            "schedules_paused": global_settings.schedules_paused,
            "queue_size": worker_info["queued_tasks"],
        }
