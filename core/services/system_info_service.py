"""
System information service for gathering platform and application stats.
"""

import logging
import os
import sys
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class SystemInfoService:
    """Service for gathering system and application information."""

    @classmethod
    def get_version(cls) -> str:
        """Get PyRunner version."""
        try:
            from pyrunner.version import __version__

            return __version__
        except ImportError:
            return "Unknown"

    @classmethod
    def get_python_version(cls) -> str:
        """Get Python interpreter version."""
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    @classmethod
    def get_python_version_full(cls) -> str:
        """Get full Python version string."""
        return sys.version

    @classmethod
    def get_uptime(cls) -> Optional[timedelta]:
        """
        Get application uptime as timedelta.
        Returns None if start time not captured.
        """
        from core.apps import APP_START_TIME

        if APP_START_TIME is None:
            return None
        return timezone.now() - APP_START_TIME

    @classmethod
    def get_uptime_display(cls) -> str:
        """Get human-readable uptime string."""
        uptime = cls.get_uptime()
        if uptime is None:
            return "Unknown"

        total_seconds = int(uptime.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if not parts or seconds > 0:
            parts.append(f"{seconds}s")

        return " ".join(parts)

    @classmethod
    def get_database_size(cls) -> int:
        """Get database file size in bytes."""
        db_path = settings.DATABASES["default"]["NAME"]
        try:
            return os.path.getsize(db_path)
        except (OSError, FileNotFoundError):
            return 0

    @classmethod
    def get_database_size_display(cls) -> str:
        """Get human-readable database size."""
        from core.services.environment_service import EnvironmentService

        size = cls.get_database_size()
        return EnvironmentService.format_disk_usage(size)

    @classmethod
    def get_environments_disk_usage(cls) -> int:
        """Get total disk usage of all environments in bytes."""
        env_root = settings.ENVIRONMENTS_ROOT

        if not os.path.isdir(env_root):
            return 0

        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(env_root):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except (OSError, FileNotFoundError):
                        pass
        except Exception as e:
            logger.error(f"Failed to calculate environments disk usage: {e}")

        return total

    @classmethod
    def get_environments_disk_usage_display(cls) -> str:
        """Get human-readable environments disk usage."""
        from core.services.environment_service import EnvironmentService

        size = cls.get_environments_disk_usage()
        return EnvironmentService.format_disk_usage(size)

    @classmethod
    def get_worker_status(cls) -> dict:
        """
        Get django-q worker status using heartbeat mechanism.

        Returns dict with:
        - status: str ("running", "stopped", "unknown")
        - status_text: str (human-readable status)
        - configured_workers: int (number of configured workers)
        - queued_tasks: int (pending tasks in queue)
        - recent_tasks: int (tasks completed in last hour)
        - last_task_at: datetime or None
        - heartbeat_at: datetime or None
        """
        from datetime import timedelta

        from django_q.models import OrmQ, Task

        from core.models import GlobalSettings

        now = timezone.now()
        one_hour_ago = now - timedelta(hours=1)
        stale_threshold = now - timedelta(seconds=30)
        heartbeat_threshold = now - timedelta(seconds=90)  # 1.5 minutes stale

        # Get heartbeat timestamp from settings
        global_settings = GlobalSettings.get_settings()
        heartbeat_at = global_settings.worker_heartbeat_at

        # Count queued tasks (pending in OrmQ)
        try:
            queued_count = OrmQ.objects.count()
        except Exception:
            queued_count = 0

        # Check for stale queued tasks (tasks stuck in queue > 30 seconds)
        try:
            stale_tasks = OrmQ.objects.filter(lock__lt=stale_threshold).count()
        except Exception:
            stale_tasks = 0

        # Count recent completed tasks
        try:
            recent_tasks = Task.objects.filter(started__gte=one_hour_ago).count()
        except Exception:
            recent_tasks = 0

        # Get last task timestamp
        try:
            last_task = Task.objects.order_by("-started").first()
            last_task_at = last_task.started if last_task else None
        except Exception:
            last_task_at = None

        # Get configured worker count
        worker_count = settings.Q_CLUSTER.get("workers", 2)

        # Determine status based on heartbeat and queue state
        if stale_tasks > 0:
            # Tasks stuck in queue = workers definitely not running
            status = "stopped"
            status_text = "Stopped"
        elif heartbeat_at and heartbeat_at >= heartbeat_threshold:
            # Recent heartbeat = workers running
            status = "running"
            status_text = "Running"
        elif heartbeat_at and heartbeat_at < heartbeat_threshold:
            # Stale heartbeat = workers likely stopped
            status = "stopped"
            status_text = "Stopped"
        else:
            # No heartbeat yet = unknown (first run or never started)
            status = "unknown"
            status_text = "Unknown"

        return {
            "status": status,
            "status_text": status_text,
            "configured_workers": worker_count,
            "queued_tasks": queued_count,
            "recent_tasks": recent_tasks,
            "last_task_at": last_task_at,
            "heartbeat_at": heartbeat_at,
        }

    @classmethod
    def get_all_info(cls) -> dict:
        """
        Get all system information in one call.

        Returns dict with all system info.
        """
        worker_status = cls.get_worker_status()
        uptime = cls.get_uptime()

        return {
            "version": cls.get_version(),
            "python_version": cls.get_python_version(),
            "python_version_full": cls.get_python_version_full(),
            "uptime": cls.get_uptime_display(),
            "uptime_seconds": uptime.total_seconds() if uptime else None,
            "database_size": cls.get_database_size(),
            "database_size_display": cls.get_database_size_display(),
            "environments_size": cls.get_environments_disk_usage(),
            "environments_size_display": cls.get_environments_disk_usage_display(),
            "worker_status": worker_status,
        }
