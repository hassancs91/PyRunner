"""
Task service for managing Django-Q2 tasks.
"""

import logging
import pickle
from datetime import timedelta
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)


class TaskService:
    """Service for managing and monitoring Django-Q2 tasks."""

    @classmethod
    def get_queued_tasks(cls) -> list[dict[str, Any]]:
        """
        Get all tasks currently in the queue (pending execution).

        Returns list of dicts with task info including linked Run if applicable.
        """
        from django_q.models import OrmQ

        from core.models import Run

        queued = []
        for q in OrmQ.objects.all().order_by("lock"):
            task_info = {
                "id": q.key,
                "name": "Unknown",
                "func": "Unknown",
                "queued_at": q.lock,
                "linked_run": None,
                "type": "system",
            }

            # Try to decode payload to get task details
            try:
                payload = pickle.loads(q.payload)
                task_info["name"] = payload.get("name", q.key)
                task_info["func"] = payload.get("func", "Unknown")
            except Exception:
                task_info["name"] = q.key

            # Try to find linked Run
            try:
                run = Run.objects.filter(task_id=q.key).select_related("script").first()
                if run:
                    task_info["linked_run"] = run
                    task_info["type"] = "script_run"
            except Exception as e:
                logger.debug(f"Error finding linked run: {e}")

            queued.append(task_info)

        return queued

    @classmethod
    def get_completed_tasks(
        cls,
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Get completed tasks from Django-Q2 Task model.

        Args:
            status_filter: "success" or "failed" to filter by status
            limit: Maximum number of tasks to return
            offset: Number of tasks to skip

        Returns:
            Tuple of (tasks list, total count)
        """
        from django_q.models import Task

        from core.models import Run

        qs = Task.objects.all().order_by("-started")

        if status_filter == "success":
            qs = qs.filter(success=True)
        elif status_filter == "failed":
            qs = qs.filter(success=False)

        total = qs.count()
        tasks = qs[offset : offset + limit]

        result = []
        for task in tasks:
            duration = None
            if task.started and task.stopped:
                duration = (task.stopped - task.started).total_seconds()

            task_info = {
                "id": task.id,
                "name": task.name or "Unknown",
                "func": task.func or "Unknown",
                "success": task.success,
                "started": task.started,
                "stopped": task.stopped,
                "duration": duration,
                "duration_display": cls._format_duration(duration),
                "result": task.result,
                "linked_run": None,
                "type": "system",
            }

            # Try to find linked Run by task_id
            try:
                run = Run.objects.filter(task_id=task.id).select_related("script").first()
                if run:
                    task_info["linked_run"] = run
                    task_info["type"] = "script_run"
            except Exception as e:
                logger.debug(f"Error finding linked run: {e}")

            result.append(task_info)

        return result, total

    @classmethod
    def get_stuck_tasks(cls, threshold_minutes: int = 5) -> list[dict[str, Any]]:
        """
        Identify tasks that appear to be stuck.

        A task is considered stuck if:
        1. It's in OrmQ with lock timestamp older than threshold
        2. It has a linked Run with status='running' but started long ago

        Args:
            threshold_minutes: Minutes after which a queued task is considered stuck

        Returns:
            List of stuck task info dicts
        """
        from django_q.models import OrmQ

        from core.models import Run

        now = timezone.now()
        stale_threshold = now - timedelta(minutes=threshold_minutes)

        stuck = []

        # Check OrmQ for stale entries
        try:
            for q in OrmQ.objects.filter(lock__lt=stale_threshold):
                task_info = {
                    "id": q.key,
                    "type": "queued_stale",
                    "queued_at": q.lock,
                    "stuck_minutes": int((now - q.lock).total_seconds() / 60),
                    "linked_run": None,
                }

                # Try to find linked Run
                run = Run.objects.filter(task_id=q.key).select_related("script").first()
                if run:
                    task_info["linked_run"] = run

                stuck.append(task_info)
        except Exception as e:
            logger.error(f"Error checking stuck queued tasks: {e}")

        # Check for Runs stuck in "running" state without corresponding queue entry
        try:
            for run in Run.objects.filter(
                status=Run.Status.RUNNING,
                started_at__lt=stale_threshold,
            ).select_related("script"):
                # Check if task is still in queue
                in_queue = OrmQ.objects.filter(key=run.task_id).exists() if run.task_id else False

                if not in_queue:
                    # Task not in queue but Run is still "running" - might be stuck
                    stuck_minutes = int((now - run.started_at).total_seconds() / 60)

                    # Check against script timeout if configured
                    timeout = run.script.timeout_seconds if run.script else 300
                    if stuck_minutes > (timeout / 60) + 2:  # 2 minute grace period
                        stuck.append({
                            "id": run.task_id or str(run.id),
                            "type": "running_overtime",
                            "queued_at": run.started_at,
                            "stuck_minutes": stuck_minutes,
                            "linked_run": run,
                        })
        except Exception as e:
            logger.error(f"Error checking stuck running tasks: {e}")

        return stuck

    @classmethod
    def get_task_statistics(cls) -> dict[str, int]:
        """
        Get task queue statistics.

        Returns dict with:
        - queued_count: Tasks waiting in queue
        - running_count: Tasks currently running (based on Run status)
        - completed_today: Tasks completed successfully in last 24h
        - failed_today: Tasks failed in last 24h
        - stuck_count: Number of stuck tasks
        """
        from django_q.models import OrmQ, Task

        from core.models import Run

        now = timezone.now()
        today_start = now - timedelta(hours=24)

        stats = {
            "queued_count": 0,
            "running_count": 0,
            "completed_today": 0,
            "failed_today": 0,
            "stuck_count": 0,
        }

        try:
            stats["queued_count"] = OrmQ.objects.count()
        except Exception:
            pass

        try:
            stats["running_count"] = Run.objects.filter(status=Run.Status.RUNNING).count()
        except Exception:
            pass

        try:
            stats["completed_today"] = Task.objects.filter(
                started__gte=today_start,
                success=True,
            ).count()
        except Exception:
            pass

        try:
            stats["failed_today"] = Task.objects.filter(
                started__gte=today_start,
                success=False,
            ).count()
        except Exception:
            pass

        try:
            stats["stuck_count"] = len(cls.get_stuck_tasks())
        except Exception:
            pass

        return stats

    @classmethod
    def cancel_queued_task(cls, task_id: str) -> tuple[bool, str]:
        """
        Cancel a task that's still in the queue.

        Removes the task from OrmQ and updates any linked Run to cancelled status.

        Args:
            task_id: The task ID to cancel

        Returns:
            Tuple of (success, message)
        """
        from django_q.models import OrmQ

        from core.models import Run

        try:
            # Delete from OrmQ
            deleted_count = OrmQ.objects.filter(key=task_id).delete()[0]

            if deleted_count == 0:
                return False, "Task not found in queue"

            # Update linked Run if exists
            run = Run.objects.filter(
                task_id=task_id,
                status__in=[Run.Status.PENDING, Run.Status.RUNNING],
            ).first()

            if run:
                run.status = Run.Status.CANCELLED
                run.ended_at = timezone.now()
                run.stderr = (run.stderr or "") + "\n[Task cancelled by user]"
                run.save(update_fields=["status", "ended_at", "stderr"])

            return True, "Task cancelled successfully"

        except Exception as e:
            logger.error(f"Error cancelling task {task_id}: {e}")
            return False, str(e)

    @classmethod
    def force_stop_task(cls, task_id: str) -> tuple[bool, str]:
        """
        Force stop a task that may be running.

        Note: Django-Q2 doesn't support killing running processes on Windows.
        This will mark the Run as cancelled, but the underlying process may
        continue until it times out.

        Args:
            task_id: The task ID to stop

        Returns:
            Tuple of (success, message)
        """
        from django_q.models import OrmQ

        from core.models import Run

        try:
            # Delete from OrmQ if still there
            OrmQ.objects.filter(key=task_id).delete()

            # Update Run status
            run = Run.objects.filter(
                task_id=task_id,
                status=Run.Status.RUNNING,
            ).first()

            if run:
                run.status = Run.Status.CANCELLED
                run.ended_at = timezone.now()
                run.stderr = (run.stderr or "") + "\n[Task forcefully stopped by user]"
                run.save(update_fields=["status", "ended_at", "stderr"])
                return True, (
                    "Run marked as cancelled. Note: The underlying process may "
                    "continue until it times out."
                )

            # Check if it's a pending run
            pending_run = Run.objects.filter(
                task_id=task_id,
                status=Run.Status.PENDING,
            ).first()

            if pending_run:
                pending_run.status = Run.Status.CANCELLED
                pending_run.ended_at = timezone.now()
                pending_run.save(update_fields=["status", "ended_at"])
                return True, "Pending run cancelled successfully"

            return False, "No running or pending task found with this ID"

        except Exception as e:
            logger.error(f"Error force stopping task {task_id}: {e}")
            return False, str(e)

    @classmethod
    def _format_duration(cls, seconds: float | None) -> str:
        """Format duration in seconds to human-readable string."""
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        secs = seconds % 60
        if minutes < 60:
            return f"{minutes}m {secs:.0f}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"
