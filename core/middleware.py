"""
Custom middleware for PyRunner.
"""

import logging

from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)


class SetupWizardMiddleware:
    """
    Middleware that redirects to setup wizard if initial setup is not completed.

    Allows access to:
    - /setup/* (the setup wizard itself)
    - /static/* (static assets)
    - /<admin_url>/* (emergency access, configurable)
    """

    # Static allowed paths (always allowed)
    STATIC_ALLOWED_PREFIXES = [
        "/setup/",
        "/static/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response
        self._admin_prefix = None

    def _get_admin_prefix(self):
        """Get the admin URL prefix (cached after first call)."""
        if self._admin_prefix is None:
            try:
                from core.models import GlobalSettings
                slug = GlobalSettings.get_settings().admin_url_slug or "django-admin"
                self._admin_prefix = f"/{slug}/"
            except Exception:
                self._admin_prefix = "/django-admin/"
        return self._admin_prefix

    def _get_allowed_prefixes(self):
        """Get all allowed path prefixes including dynamic admin URL."""
        return self.STATIC_ALLOWED_PREFIXES + [self._get_admin_prefix()]

    def __call__(self, request):
        # Skip for allowed paths
        if any(
            request.path.startswith(prefix)
            for prefix in self._get_allowed_prefixes()
        ):
            return self.get_response(request)

        # Check if setup is needed
        if self._is_setup_needed():
            setup_url = reverse("setup:setup")
            if request.path != setup_url:
                return redirect(setup_url)

        # Check if admin setup is needed (setup complete but no admin user)
        elif self._is_admin_setup_needed():
            admin_setup_url = reverse("setup:admin_setup")
            if request.path != admin_setup_url:
                return redirect(admin_setup_url)

        return self.get_response(request)

    def _is_setup_needed(self) -> bool:
        """Check if initial setup has been completed."""
        try:
            from core.services.setup_service import SetupService
            return SetupService.is_setup_needed()
        except Exception as e:
            # If we can't check, assume setup is needed
            logger.debug(f"Setup check failed in middleware: {e}")
            return True

    def _is_admin_setup_needed(self) -> bool:
        """Check if admin user needs to be created."""
        try:
            from core.services.setup_service import SetupService
            return SetupService.needs_admin_setup()
        except Exception as e:
            logger.debug(f"Admin setup check failed in middleware: {e}")
            return False
