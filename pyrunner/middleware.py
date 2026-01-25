"""Custom middleware for PyRunner."""


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
