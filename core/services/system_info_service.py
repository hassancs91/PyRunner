"""
System information service for gathering platform and application stats.
"""

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

import psutil
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Cgroup filesystem root. Inside a Docker container these files hold the
# kernel's per-container accounting (the same numbers `docker stats` shows),
# whereas psutil's /proc reads describe the whole host machine. Module-level
# so tests can point it at a fake tree.
CGROUP_ROOT = Path("/sys/fs/cgroup")

# cgroup v1 reports "no memory limit" as PAGE_COUNTER_MAX (~2^63).
_CGROUP_V1_UNLIMITED = 1 << 60


class SystemInfoService:
    """Service for gathering system and application information."""

    @classmethod
    def get_version(cls) -> str:
        """Get PyRun version."""
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
        """Get database file size in bytes (SQLite only; 0 on other engines, where
        ``NAME`` is a database name, not a file path)."""
        from django.db import connection

        if connection.vendor != "sqlite":
            return 0
        db_path = settings.DATABASES["default"]["NAME"]
        try:
            return os.path.getsize(db_path)
        except (OSError, TypeError):
            return 0

    @classmethod
    def get_database_size_display(cls) -> str:
        """Get human-readable database size ("N/A" on non-SQLite engines, where the
        on-disk size isn't a local file we can stat)."""
        from django.db import connection

        if connection.vendor != "sqlite":
            return "N/A"
        from core.services.environment_service import EnvironmentService

        return EnvironmentService.format_disk_usage(cls.get_database_size())

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
        # One shared staleness window with GlobalSettings.worker_is_alive() so the
        # dashboard card and the inbound-webhook fast-fail never disagree.
        heartbeat_threshold = now - timedelta(
            seconds=GlobalSettings.WORKER_HEARTBEAT_TIMEOUT_SECONDS
        )

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

    @classmethod
    def _format_bytes(cls, bytes_value: int) -> str:
        """Format bytes into human-readable string."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if bytes_value < 1024.0:
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.1f} PB"

    @classmethod
    def get_cpu_usage(cls) -> float:
        """Get current CPU usage percentage (0-100)."""
        try:
            return psutil.cpu_percent(interval=0.1)
        except Exception as e:
            logger.error(f"Failed to get CPU usage: {e}")
            return 0.0

    @classmethod
    def get_memory_info(cls) -> dict:
        """
        Get RAM usage information.

        Returns dict with:
        - total: Total RAM in bytes
        - used: Used RAM in bytes
        - available: Available RAM in bytes
        - percent: Usage percentage (0-100)
        - total_display: Human-readable total
        - used_display: Human-readable used
        """
        try:
            mem = psutil.virtual_memory()
            return {
                "total": mem.total,
                "used": mem.used,
                "available": mem.available,
                "percent": mem.percent,
                "total_display": cls._format_bytes(mem.total),
                "used_display": cls._format_bytes(mem.used),
            }
        except Exception as e:
            logger.error(f"Failed to get memory info: {e}")
            return {
                "total": 0,
                "used": 0,
                "available": 0,
                "percent": 0,
                "total_display": "Unknown",
                "used_display": "Unknown",
            }

    @classmethod
    def get_disk_info(cls) -> dict:
        """
        Get storage/disk usage information.

        Returns dict with:
        - total: Total disk space in bytes
        - used: Used disk space in bytes
        - free: Free disk space in bytes
        - percent: Usage percentage (0-100)
        - total_display: Human-readable total
        - used_display: Human-readable used
        """
        try:
            disk = psutil.disk_usage(str(settings.BASE_DIR))
            return {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent,
                "total_display": cls._format_bytes(disk.total),
                "used_display": cls._format_bytes(disk.used),
            }
        except Exception as e:
            logger.error(f"Failed to get disk info: {e}")
            return {
                "total": 0,
                "used": 0,
                "free": 0,
                "percent": 0,
                "total_display": "Unknown",
                "used_display": "Unknown",
            }

    # ------------------------------------------------------------------
    # Container (cgroup) stats
    # ------------------------------------------------------------------

    @classmethod
    def _read_cgroup_file(cls, relative_path: str) -> Optional[str]:
        """Read a cgroup file, returning stripped text or None."""
        try:
            return (Path(CGROUP_ROOT) / relative_path).read_text().strip()
        except OSError:
            return None

    @classmethod
    def _detect_cgroup_version(cls) -> Optional[str]:
        """
        Detect whether we're inside a container with readable cgroup stats.

        Returns "v2" (unified hierarchy), "v1" (legacy controllers), or None
        when not containerized / files unavailable (e.g. bare metal, Windows).
        """
        root = Path(CGROUP_ROOT)
        try:
            if (root / "memory.current").is_file():
                return "v2"
            if (root / "memory" / "memory.usage_in_bytes").is_file():
                return "v1"
        except OSError:
            pass
        return None

    @classmethod
    def _parse_cgroup_stat(cls, content: str) -> dict:
        """Parse "key value" lines (memory.stat / cpu.stat) into an int dict."""
        stats = {}
        for line in content.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    stats[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return stats

    @classmethod
    def _read_container_cpu_usage_usec(cls, version: str) -> Optional[int]:
        """Cumulative CPU time consumed by the container, in microseconds."""
        if version == "v2":
            raw = cls._read_cgroup_file("cpu.stat")
            if raw is None:
                return None
            return cls._parse_cgroup_stat(raw).get("usage_usec")
        raw = cls._read_cgroup_file("cpuacct/cpuacct.usage")  # nanoseconds
        try:
            return int(raw) // 1000 if raw is not None else None
        except ValueError:
            return None

    @classmethod
    def _get_container_cpu_limit(cls, version: str) -> float:
        """
        Effective CPUs the container may use: its quota when one is set
        (e.g. `cpus: 2` in compose), otherwise all host cores — so 100%
        always means "maxed out what this container can actually use".
        """
        quota = period = None
        if version == "v2":
            raw = cls._read_cgroup_file("cpu.max")  # "max 100000" | "<quota> <period>"
            parts = raw.split() if raw else []
            if len(parts) == 2 and parts[0] != "max":
                try:
                    quota, period = int(parts[0]), int(parts[1])
                except ValueError:
                    quota = period = None
        else:
            quota_raw = cls._read_cgroup_file("cpu/cpu.cfs_quota_us")  # -1 = no limit
            period_raw = cls._read_cgroup_file("cpu/cpu.cfs_period_us")
            try:
                if quota_raw is not None and period_raw is not None and int(quota_raw) > 0:
                    quota, period = int(quota_raw), int(period_raw)
            except ValueError:
                quota = period = None
        if quota and period:
            return quota / period
        return float(psutil.cpu_count() or 1)

    @classmethod
    def _get_container_memory(cls, version: str, host_memory: dict) -> Optional[dict]:
        """
        Container memory usage from cgroups, shaped like get_memory_info().

        `is_limit` tells the UI whether `total` is a configured container
        limit or (when uncapped) the host's total RAM.
        """
        if version == "v2":
            current_raw = cls._read_cgroup_file("memory.current")
            stat_raw = cls._read_cgroup_file("memory.stat")
            limit_raw = cls._read_cgroup_file("memory.max")  # bytes | "max"
            inactive_key = "inactive_file"
        else:
            current_raw = cls._read_cgroup_file("memory/memory.usage_in_bytes")
            stat_raw = cls._read_cgroup_file("memory/memory.stat")
            limit_raw = cls._read_cgroup_file("memory/memory.limit_in_bytes")
            inactive_key = "total_inactive_file"

        try:
            current = int(current_raw)
        except (TypeError, ValueError):
            return None

        # Subtract reclaimable page cache so the number matches `docker stats`.
        stat = cls._parse_cgroup_stat(stat_raw) if stat_raw else {}
        used = max(current - stat.get(inactive_key, 0), 0)

        limit = None
        if limit_raw and limit_raw != "max":
            try:
                limit = int(limit_raw)
            except ValueError:
                limit = None
        host_total = host_memory.get("total") or 0
        if limit is not None and (
            limit >= _CGROUP_V1_UNLIMITED or (host_total and limit > host_total)
        ):
            limit = None  # uncapped: the container can use all host RAM

        total = limit if limit is not None else host_total
        if total <= 0:
            return None
        return {
            "total": total,
            "used": used,
            "available": max(total - used, 0),
            "percent": min(round(used / total * 100, 1), 100.0),
            "total_display": cls._format_bytes(total),
            "used_display": cls._format_bytes(used),
            "is_limit": limit is not None,
        }

    @classmethod
    def get_system_resources(cls) -> dict:
        """
        Get all system resource metrics in one call.

        Inside a container, `cpu`/`memory` are the container's own numbers
        (from cgroups, matching `docker stats`) and `host` carries the
        machine-wide psutil numbers. Outside a container, `cpu`/`memory` are
        the host numbers — same shape as before — and `host` is None. `disk`
        is always the filesystem backing the app directory (in Docker that is
        the host disk behind the volume, so a split would show it twice).
        """
        cgroup_version = cls._detect_cgroup_version()

        # Sample container CPU around the blocking host read so both
        # measurements share the same ~100ms window.
        sample_start = None
        if cgroup_version:
            usage = cls._read_container_cpu_usage_usec(cgroup_version)
            if usage is not None:
                sample_start = (usage, time.monotonic())

        host_cpu = cls.get_cpu_usage()  # blocks ~100ms (interval=0.1)
        host_memory = cls.get_memory_info()
        disk = cls.get_disk_info()

        container_cpu = None
        container_memory = None
        if cgroup_version:
            if sample_start is not None:
                usage_end = cls._read_container_cpu_usage_usec(cgroup_version)
                elapsed = time.monotonic() - sample_start[1]
                if usage_end is not None and elapsed > 0:
                    cores = cls._get_container_cpu_limit(cgroup_version)
                    percent = (
                        (usage_end - sample_start[0]) / (elapsed * 1_000_000) / cores * 100
                    )
                    container_cpu = round(min(max(percent, 0.0), 100.0), 1)
            container_memory = cls._get_container_memory(cgroup_version, host_memory)

        if container_cpu is None or container_memory is None:
            # Not containerized (or cgroup files unreadable): the host numbers
            # ARE the app's numbers — render exactly as before.
            return {
                "in_container": False,
                "cpu": {"percent": host_cpu},
                "memory": host_memory,
                "disk": disk,
                "host": None,
            }

        return {
            "in_container": True,
            "cpu": {"percent": container_cpu},
            "memory": container_memory,
            "disk": disk,
            "host": {
                "cpu": {"percent": host_cpu},
                "memory": host_memory,
            },
        }
