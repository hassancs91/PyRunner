"""Custom middleware for PyRun."""

from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Attach a scoped Content-Security-Policy to HTML responses.

    The policy (``settings.CONTENT_SECURITY_POLICY``) is deliberately narrow: it
    hardens clickjacking (``frame-ancestors``), ``<base>``/``<object>`` injection,
    and cross-origin form submission, but does NOT restrict ``script-src`` /
    ``style-src``. The templates rely on inline ``<script>`` blocks and inline
    event handlers, so dropping ``'unsafe-inline'`` from ``script-src`` needs a
    nonce-based template refactor first (see the note in settings.py). Scoped to
    ``text/html`` since CSP only governs document contexts.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")

    def __call__(self, request):
        response = self.get_response(request)

        if self.policy and "text/html" in response.get("Content-Type", ""):
            # setdefault: never clobber a stricter policy set by a specific view.
            response.setdefault("Content-Security-Policy", self.policy)

        return response


class NoCacheMiddleware:
    """Prevent browser caching of HTML responses.

    This ensures users always see fresh data when navigating the dashboard,
    without needing to hard-refresh the browser.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only add no-cache headers to HTML responses
        content_type = response.get("Content-Type", "")
        if "text/html" in content_type:
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response
