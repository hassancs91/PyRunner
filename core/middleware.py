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
    - /admin/* (emergency access)
    """

    ALLOWED_PATH_PREFIXES = [
        "/setup/",
        "/static/",
        "/admin/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip for allowed paths
        if any(
            request.path.startswith(prefix)
            for prefix in self.ALLOWED_PATH_PREFIXES
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
