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
        # Seam 1 internal datastore endpoint: loopback-only, token-authed, must
        # never be 302'd to /setup/ during a transient is_setup_needed() window.
        "/internal/",
        # Plugin public pages: sessionless capability URLs — a 302 to /setup/
        # would be nonsense for an anonymous reader; the view 404s cleanly on
        # an instance with no shares.
        "/p/",
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


class ActiveWorkspaceMiddleware:
    """Resolve the active workspace (tenancy Decision 1: URL-scoped, optional prefix).

    Reads the optional ``workspace_id`` captured by the additive ``/w/<id>/`` URL
    prefix, validates it against the user's membership (404 if the user is not a
    member — the URL is never trusted; this is the web tier's primary new IDOR
    surface), and attaches ``request.workspace``. A bare (unprefixed) URL resolves
    to the user's default workspace *in place* — no redirect — so a
    single-workspace instance is byte-for-byte unchanged.

    It attaches ``request.workspace``; the tenant-scoped views and services then
    filter their queries by it (``Model.objects.for_workspace(request.workspace)``
    for lists, ``get_object_or_404(Model, …, workspace=request.workspace)`` for
    detail). It runs as a ``process_view`` hook (last in MIDDLEWARE), so
    ``request.user`` is already populated and the captured URL kwarg is available.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # The prefixed include passes ``workspace_id`` to every view it wraps;
        # strip it here so the existing view signatures are untouched.
        requested_id = view_kwargs.pop("workspace_id", None)

        # Pin namespace resolution to the canonical (unprefixed) cpanel instance.
        # The cpanel routes are mounted twice (canonical + the /w/<id>/ prefixed
        # 'cpanel_ws' instance); without this, a request served under the prefix
        # would make every existing {% url 'cpanel:…' %} try to reverse into the
        # prefixed instance (which needs a workspace_id) and raise NoReverseMatch.
        # {% ws_url %} and the switcher target 'cpanel_ws' explicitly, so they are
        # unaffected.
        request.current_app = "cpanel"

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # Anonymous: no workspace context. A prefixed URL under an
            # @login_required view will redirect to login as usual — we do not
            # 404 anonymous callers (that would leak that the path exists).
            request.workspace = None
            return None

        from core.models import Workspace

        workspace, ok = Workspace.resolve_for(user, requested_id=requested_id)
        if requested_id is not None and not ok:
            # The URL named a workspace this user may not access (or one that
            # doesn't exist). 404 — no existence disclosure, never trust the URL.
            from django.http import Http404

            raise Http404("Workspace not found")

        request.workspace = workspace
        return None


class TimezoneMiddleware:
    """Activate the instance display timezone for every request.

    ``GlobalSettings.timezone`` is the admin-chosen display timezone; activating
    it makes Django render every template datetime in that zone (storage stays
    UTC via USE_TZ). Defensive: before setup, or on a DB hiccup, we deactivate
    — i.e. fall back to UTC — rather than break the page.

    The settings lookup is memoized per-process for a few seconds so the
    middleware adds ~zero queries on hot paths; a changed timezone shows up
    within TTL_SECONDS (display-only, so brief staleness is harmless).
    """

    TTL_SECONDS = 5.0

    def __init__(self, get_response):
        self.get_response = get_response
        self._tz = None  # ZoneInfo, or None = deactivate (UTC)
        self._expires = 0.0

    def _current_tz(self):
        import time

        now = time.monotonic()
        if now >= self._expires:
            try:
                from core.models import GlobalSettings
                from core.tz import safe_zoneinfo

                self._tz = safe_zoneinfo(
                    GlobalSettings.get_settings().timezone,
                    context="instance display timezone",
                )
            except Exception:
                self._tz = None  # DB not ready (setup) — render in UTC
            self._expires = now + self.TTL_SECONDS
        return self._tz

    def __call__(self, request):
        from django.utils import timezone

        tz = self._current_tz()
        if tz is not None:
            timezone.activate(tz)
        else:
            timezone.deactivate()
        return self.get_response(request)
