"""
Log service for reading and parsing application logs.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from django.conf import settings


class LogEntry(NamedTuple):
    """Represents a parsed log entry."""

    timestamp: datetime
    level: str
    logger: str
    message: str
    module: str | None = None
    function: str | None = None
    line: int | None = None
    exception: str | None = None
    raw: str = ""


class LogService:
    """
    Service for reading and filtering application logs.
    """

    LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @classmethod
    def get_log_file_path(cls) -> Path:
        """Get the path to the main log file."""
        return settings.LOGS_DIR / "pyrunner.log"

    @classmethod
    def get_log_files(cls) -> list[Path]:
        """Get all log files including rotated ones."""
        log_dir = settings.LOGS_DIR
        if not log_dir.exists():
            return []

        files = list(log_dir.glob("pyrunner.log*"))
        # Sort by modification time, newest first
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    @classmethod
    def get_log_file_size(cls) -> int:
        """Get total size of all log files in bytes."""
        return sum(f.stat().st_size for f in cls.get_log_files())

    @classmethod
    def parse_log_line(cls, line: str) -> LogEntry | None:
        """Parse a single log line (JSON format)."""
        line = line.strip()
        if not line:
            return None

        try:
            data = json.loads(line)
            return LogEntry(
                timestamp=datetime.fromisoformat(data.get("timestamp", "")),
                level=data.get("level", "UNKNOWN"),
                logger=data.get("logger", ""),
                message=data.get("message", ""),
                module=data.get("module"),
                function=data.get("function"),
                line=data.get("line"),
                exception=data.get("exception"),
                raw=line,
            )
        except (json.JSONDecodeError, ValueError):
            # Handle non-JSON lines (legacy format)
            return None

    @classmethod
    def read_logs(
        cls,
        level_filter: str | None = None,
        search_query: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[LogEntry], int]:
        """
        Read and filter log entries.

        Returns:
            Tuple of (log_entries, total_count)
        """
        log_file = cls.get_log_file_path()
        if not log_file.exists():
            return [], 0

        entries = []
        total_matched = 0

        # Read file in reverse (newest first)
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in reversed(lines):
            entry = cls.parse_log_line(line)
            if entry is None:
                continue

            # Apply filters
            if level_filter and entry.level != level_filter:
                continue

            if search_query:
                search_lower = search_query.lower()
                if (
                    search_lower not in entry.message.lower()
                    and search_lower not in entry.logger.lower()
                ):
                    continue

            if start_date and entry.timestamp < start_date:
                continue

            if end_date and entry.timestamp > end_date:
                continue

            total_matched += 1

            # Apply pagination
            if total_matched > offset and len(entries) < limit:
                entries.append(entry)

        return entries, total_matched

    @classmethod
    def tail_logs(cls, lines: int = 100) -> list[LogEntry]:
        """Get the last N log entries (for real-time view)."""
        log_file = cls.get_log_file_path()
        if not log_file.exists():
            return []

        entries = []
        with open(log_file, "r", encoding="utf-8") as f:
            # Read last N lines efficiently
            all_lines = f.readlines()[-lines:]

        for line in reversed(all_lines):
            entry = cls.parse_log_line(line)
            if entry:
                entries.append(entry)

        return entries

    @classmethod
    def clear_logs(cls) -> int:
        """Clear all log files. Returns number of bytes freed."""
        total_size = 0
        for log_file in cls.get_log_files():
            total_size += log_file.stat().st_size
            log_file.unlink()
        return total_size
