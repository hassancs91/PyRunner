"""
Service for checking whether a newer PyRunner release is available on GitHub.

The actual network call runs in a django-q2 task (see core.tasks.check_for_updates_task)
on a daily schedule, and the result is persisted on GlobalSettings. The request path
only ever does a cheap read (see get_update_context), so page rendering never blocks
on GitHub.
"""

import logging

import requests
from django.utils import timezone

from pyrunner.version import __version__

logger = logging.getLogger(__name__)


class UpdateService:
    """Fetches and compares the latest PyRunner version published on GitHub."""

    GITHUB_TAGS_URL = "https://api.github.com/repos/hassancs91/PyRunner/tags"
    REQUEST_TIMEOUT = 10  # seconds

    @staticmethod
    def _parse(version: str) -> tuple:
        """Turn 'v1.8.2' / '1.8.2-rc1' into a comparable tuple like (1, 8, 2)."""
        cleaned = str(version).strip().lstrip("vV").split("-")[0].split("+")[0]
        parts = []
        for chunk in cleaned.split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                parts.append(0)
        return tuple(parts) or (0,)

    @classmethod
    def is_newer(cls, candidate: str, current: str) -> bool:
        """True if `candidate` is a strictly higher version than `current`."""
        return cls._parse(candidate) > cls._parse(current)

    @classmethod
    def fetch_latest_version(cls) -> str | None:
        """
        Return the highest semver tag from the GitHub repo, or None on failure.

        Tags are not returned in version order, so we compute the max ourselves.
        """
        resp = requests.get(
            cls.GITHUB_TAGS_URL,
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github+json"},
            timeout=cls.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        names = [t.get("name", "").strip() for t in resp.json() if t.get("name")]
        names = [n for n in names if n]
        if not names:
            return None

        latest = max(names, key=cls._parse)
        return latest.lstrip("vV")

    @classmethod
    def refresh(cls) -> str | None:
        """
        Fetch the latest version from GitHub and persist it on GlobalSettings.

        Returns the latest version string, or None if the fetch yielded nothing.
        """
        from core.models import GlobalSettings

        latest = cls.fetch_latest_version()

        settings = GlobalSettings.get_settings()
        if latest:
            settings.update_latest_version = latest
        settings.update_checked_at = timezone.now()
        settings.save(update_fields=["update_latest_version", "update_checked_at"])

        return latest

    @classmethod
    def get_update_context(cls) -> dict:
        """
        Cheap read for the context processor — is an update available?

        Reads only the stored version (single indexed query, no row creation) and
        compares it to the running version. Never touches the network.
        """
        from core.models import GlobalSettings

        try:
            latest = (
                GlobalSettings.objects.filter(pk=1)
                .values_list("update_latest_version", flat=True)
                .first()
            ) or ""
        except Exception:
            latest = ""

        latest = latest.strip()
        available = bool(latest) and cls.is_newer(latest, __version__)
        return {
            "update_available": available,
            "update_latest_version": latest if available else "",
        }
