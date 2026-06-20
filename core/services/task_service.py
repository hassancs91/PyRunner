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

    @staticmethod
    def _hidden_task_ids(workspace) -> set:
        """task_ids of Runs that belong to OTHER workspaces (tenancy Stage 3).

        django-q's ``Task``/``OrmQ`` rows carry no workspace — only the linked
        ``Run`` is scopable — so the tasks page hides a task whose linked run is
        in another workspace by excluding these ids. A task with no linked run
        (system/infra: package installs, cleanup) is NOT hidden. A NULL-workspace
        run stays visible (transitional, keeps a single-workspace instance
        byte-for-byte). ``workspace`` None ⇒ no hiding (legacy/cross-workspace).

        NOTE: this scans cross-workspace run task_ids per request — acceptable at
        current scale; revisit if the runs table grows very large.
        """
        if workspace is None:
            return set()
        from django.db.models import Q

        from core.models import Run

        return set(
            Run.objects.exclude(Q(workspace=workspace) | Q(workspace__isnull=True))
            .exclude(task_id__isnull=True)
            .exclude(task_id="")
            .values_list("task_id", flat=True)
        )

    @staticmethod
    def _run_workspace_filter(workspace):
        """Q matching runs visible in ``workspace`` (the run's ws, or NULL).

        Mirrors the transitional rule used across the tenancy sweep so legacy
        NULL-workspace runs stay visible/stoppable on a single-workspace instance.
        """
        from django.db.models import Q

        return Q(workspace=workspace) | Q(workspace__isnull=True)

    @classmethod
    def _task_in_workspace(cls, task_id, workspace) -> bool:
        """Whether a task may be controlled from ``workspace`` (tenancy Stage 3).

        True when there is no linked run (system/infra task) or the linked run is
        visible in ``workspace`` (its own workspace, or NULL — transitional).
        False ONLY when a linked run exists and belongs to another workspace —
        the control-plane guard that stops a tenant from killing another tenant's
        job. ``workspace`` None ⇒ always True (legacy/unscoped caller).
        """
        if workspace is None:
            return True
        from core.models import Run

        linked = Run.objects.filter(task_id=task_id)
        if not linked.exists():
            return True
        return linked.filter(cls._run_workspace_filter(workspace)).exists()

    @classmethod
    def get_queued_tasks(cls, workspace=None) -> list[dict[str, Any]]:
        """
        Get all tasks currently in the queue (pending execution).

        Returns list of dicts with task info including linked Run if applicable.
        """
        from django_q.models import OrmQ

        from core.models import Run

        hidden = cls._hidden_task_ids(workspace)

        queued = []
        for q in OrmQ.objects.exclude(key__in=hidden).order_by("lock"):
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
    def get_running_tasks(cls, workspace=None) -> list[dict[str, Any]]:
        """
        Get runs that are currently executing (status=RUNNING).

        These are surfaced so the UI can offer a real Stop button on jobs that
        are actively running (not just on ones already flagged as stuck).
        """
        from core.models import Run

        qs = Run.objects.filter(status=Run.Status.RUNNING)
        if workspace is not None:
            qs = qs.filter(cls._run_workspace_filter(workspace))

        running = []
        for run in qs.select_related("script").order_by("started_at"):
            running.append({
                "id": run.task_id or str(run.id),
                "type": "script_run",
                "started_at": run.started_at,
                "pid": run.pid,
                "linked_run": run,
            })

        return running

    @classmethod
    def get_completed_tasks(
        cls,
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
        workspace=None,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Get completed tasks from Django-Q2 Task model.

        Args:
            status_filter: "success" or "failed" to filter by status
            limit: Maximum number of tasks to return
            offset: Number of tasks to skip
            workspace: when given, hide tasks whose linked run is in another
                workspace (tenancy Stage 3). Excluded at the queryset level so
                pagination/total stay correct. System tasks (no linked run) and
                NULL-workspace runs remain visible.

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

        hidden = cls._hidden_task_ids(workspace)
        if hidden:
            qs = qs.exclude(id__in=hidden)

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
    def get_stuck_tasks(cls, threshold_minutes: int = 5, workspace=None) -> list[dict[str, Any]]:
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

        hidden = cls._hidden_task_ids(workspace)

        stuck = []

        # Check OrmQ for stale entries
        try:
            for q in OrmQ.objects.filter(lock__lt=stale_threshold).exclude(key__in=hidden):
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
            overtime = Run.objects.filter(
                status=Run.Status.RUNNING,
                started_at__lt=stale_threshold,
            )
            if workspace is not None:
                overtime = overtime.filter(cls._run_workspace_filter(workspace))
            for run in overtime.select_related("script"):
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
    def get_task_statistics(cls, workspace=None) -> dict[str, int]:
        """
        Get task queue statistics.

        When ``workspace`` is given, counts are scoped to the active workspace
        (tenancy Stage 3): tasks whose linked run is in another workspace are
        excluded; system tasks (no linked run) and NULL-workspace runs stay
        counted, keeping a single-workspace instance byte-for-byte.

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

        hidden = cls._hidden_task_ids(workspace)

        stats = {
            "queued_count": 0,
            "running_count": 0,
            "completed_today": 0,
            "failed_today": 0,
            "stuck_count": 0,
        }

        try:
            stats["queued_count"] = OrmQ.objects.exclude(key__in=hidden).count()
        except Exception:
            pass

        try:
            running = Run.objects.filter(status=Run.Status.RUNNING)
            if workspace is not None:
                running = running.filter(cls._run_workspace_filter(workspace))
            stats["running_count"] = running.count()
        except Exception:
            pass

        try:
            stats["completed_today"] = Task.objects.filter(
                started__gte=today_start,
                success=True,
            ).exclude(id__in=hidden).count()
        except Exception:
            pass

        try:
            stats["failed_today"] = Task.objects.filter(
                started__gte=today_start,
                success=False,
            ).exclude(id__in=hidden).count()
        except Exception:
            pass

        try:
            stats["stuck_count"] = len(cls.get_stuck_tasks(workspace=workspace))
        except Exception:
            pass

        return stats

    @classmethod
    def cancel_queued_task(cls, task_id: str, workspace=None) -> tuple[bool, str]:
        """
        Cancel a task that's still in the queue.

        Removes the task from OrmQ and updates any linked Run to cancelled status.

        Args:
            task_id: The task ID to cancel
            workspace: when given, a task whose linked run is in ANOTHER workspace
                is refused before anything is mutated (tenancy Stage 3 control-
                plane guard) — a tenant cannot cancel another tenant's job.

        Returns:
            Tuple of (success, message)
        """
        from django_q.models import OrmQ

        from core.models import Run

        # Cross-workspace guard FIRST (before touching the queue), so a denied
        # request never removes another workspace's queue entry.
        if not cls._task_in_workspace(task_id, workspace):
            return False, "Task not found in queue"

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
    def force_stop_task(cls, task_id: str, workspace=None) -> tuple[bool, str]:
        """
        Force stop a task.

        For a RUNNING run, this kills the script's OS process tree (the job
        only — the long-lived django-q worker keeps running) and marks the Run
        as CANCELLED. For a not-yet-started run it removes the queue entry and
        marks the Run CANCELLED.

        Args:
            task_id: The task ID to stop
            workspace: when given, a task whose linked run is in ANOTHER workspace
                is refused before the OS process is touched (tenancy Stage 3
                control-plane guard) — a tenant cannot kill another tenant's job.

        Returns:
            Tuple of (success, message)
        """
        from django_q.models import OrmQ

        from core.executor import _kill_process_tree
        from core.models import Run

        # Cross-workspace guard FIRST — never kill another tenant's process tree
        # or remove their queue entry.
        if not cls._task_in_workspace(task_id, workspace):
            return False, "No running or pending task found with this ID"

        try:
            # Delete from OrmQ if still there (covers pending / not-yet-claimed)
            OrmQ.objects.filter(key=task_id).delete()

            # Running run -> kill the actual job process tree.
            run = Run.objects.filter(
                task_id=task_id,
                status=Run.Status.RUNNING,
            ).first()

            if run:
                # Belt-and-suspenders: only kill while still RUNNING (avoids
                # killing an unrelated, reused PID if the job just finished).
                if run.pid:
                    _kill_process_tree(run.pid)
                run.status = Run.Status.CANCELLED
                run.ended_at = timezone.now()
                run.stderr = (run.stderr or "") + "\n[Killed by user]"
                run.pid = None
                run.save(update_fields=["status", "ended_at", "stderr", "pid"])
                return True, "Run stopped — the script process was killed."

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

    @staticmethod
    def _decode_ormq_payload(payload: Any) -> dict[str, Any]:
        """
        Decode a queued task's payload into its dict form.

        django-q signs+pickles the OrmQ payload, so the correct path is
        SignedPackage.loads; fall back to a raw pickle.loads for safety.
        """
        from django_q.signing import SignedPackage

        try:
            data = SignedPackage.loads(payload)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        try:
            data = pickle.loads(payload)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @staticmethod
    def _safe_repr(value: Any) -> str:
        """Readable repr of a task arg/result, '' for empty values."""
        if value is None or value == () or value == [] or value == {}:
            return ""
        try:
            return repr(value)
        except Exception:
            return str(value)

    @classmethod
    def get_task_detail(cls, task_id: str, workspace=None) -> dict[str, Any] | None:
        """
        Build a detail dict for a single task, usable by the task detail page.

        Works for completed/failed tasks (django-q Task), still-queued tasks
        (OrmQ), and system tasks with no linked Run. Returns None if nothing is
        found for the given id.

        When ``workspace`` is given and the task's linked run belongs to another
        workspace, returns None (tenancy Stage 3 IDOR guard → 404, no
        existence disclosure). NULL-workspace runs stay visible (transitional).
        """
        from django_q.models import OrmQ, Task

        from core.models import Run

        linked_run = (
            Run.objects.filter(task_id=task_id)
            .select_related("script", "triggered_by")
            .first()
        )
        if (
            workspace is not None
            and linked_run is not None
            and linked_run.workspace_id is not None
            and linked_run.workspace_id != workspace.id
        ):
            return None  # cross-workspace task — 404, never disclose it exists
        task_type = "script_run" if linked_run else "system"

        # 1) Completed (or failed) task recorded by django-q.
        task = Task.objects.filter(id=task_id).first()
        if task:
            duration = None
            if task.started and task.stopped:
                duration = (task.stopped - task.started).total_seconds()

            traceback_text = ""
            result_display = ""
            if task.success is False:
                # django-q stores the traceback string in `result` on failure.
                traceback_text = (
                    task.result
                    if isinstance(task.result, str)
                    else cls._safe_repr(task.result)
                )
            else:
                result_display = cls._safe_repr(task.result)

            return {
                "id": task.id,
                "name": task.name or task_id,
                "func": task.func or "Unknown",
                "args_display": cls._safe_repr(task.args),
                "kwargs_display": cls._safe_repr(task.kwargs),
                "state": "completed",
                "success": task.success,
                "started": task.started,
                "stopped": task.stopped,
                "duration": duration,
                "duration_display": cls._format_duration(duration),
                "result_display": result_display,
                "traceback": traceback_text,
                "linked_run": linked_run,
                "type": task_type,
            }

        # 2) Still queued (not yet executed).
        q = OrmQ.objects.filter(key=task_id).first()
        if q:
            payload = cls._decode_ormq_payload(q.payload)
            func = payload.get("func", "Unknown")
            if func and not isinstance(func, str):
                func = getattr(func, "__name__", str(func))
            return {
                "id": task_id,
                "name": payload.get("name", task_id),
                "func": func,
                "args_display": cls._safe_repr(payload.get("args")),
                "kwargs_display": cls._safe_repr(payload.get("kwargs")),
                "state": "queued",
                "success": None,
                "queued_at": q.lock,
                "linked_run": linked_run,
                "type": task_type,
            }

        # 3) No Task/OrmQ row, but a Run references this task_id (e.g. running
        #    right now, or the Task row was pruned). Build detail from the Run.
        if linked_run:
            return {
                "id": task_id,
                "name": f"run-{linked_run.id}",
                "func": "core.tasks.execute_run_task",
                "args_display": "",
                "kwargs_display": "",
                "state": linked_run.status,
                "success": None,
                "started": linked_run.started_at,
                "stopped": linked_run.ended_at,
                "linked_run": linked_run,
                "type": task_type,
            }

        return None

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
