"""Root URL configuration.

Routes the admin (behind a configurable slug), the setup wizard, auth, the
cpanel console (mounted both unprefixed and under an optional
``/cpanel/w/<workspace_id>/`` tenancy prefix), the public REST API, the internal
loopback datastore API, the public webhook + inbound-channel endpoints, and one
guarded mount per loaded plugin at ``/plugins/<slug>/``.
"""

import importlib
import logging

from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

from core.views.webhooks import webhook_trigger_view
from core.views.channel_webhooks import channel_webhook_view
from core.views.public_pages import plugin_public_page_view

logger = logging.getLogger(__name__)


def get_admin_url_slug():
    """
    Get admin URL slug from settings.
    Called at startup - changes require app restart.
    """
    try:
        from core.models import GlobalSettings
        return GlobalSettings.get_settings().admin_url_slug or "django-admin"
    except Exception:
        return "django-admin"


urlpatterns = [
    path(f"{get_admin_url_slug()}/", admin.site.urls),
    path("setup/", include("core.urls.setup")),
    path("auth/", include("core.urls.auth")),
    # Canonical, unprefixed cpanel routes. A single-workspace instance only ever
    # uses these — byte-for-byte identical to before tenancy. Keep FIRST.
    path("cpanel/", include("core.urls.cpanel")),
    # Additive workspace-scoped mount of the SAME cpanel routes (tenancy
    # Decision 1: optional URL prefix). ActiveWorkspaceMiddleware reads
    # workspace_id, validates membership (404 if not a member), and strips the
    # kwarg so the views are unchanged. Distinct instance namespace 'cpanel_ws'
    # lets {% ws_url %} reverse to the prefixed form when a workspace is active.
    path(
        "cpanel/w/<uuid:workspace_id>/",
        include(("core.urls.cpanel", "cpanel"), namespace="cpanel_ws"),
    ),
    # REST API endpoints (token auth required)
    path("api/v1/", include("core.urls.api")),
    # Internal loopback-only datastore API. Signed per-run token auth; exempt
    # from SSL redirect + setup gate (see settings). Live path: the datastore
    # helper calls it over loopback on no-DB-file/Postgres deploys (idle on
    # SQLite, where the helper talks to the file directly).
    path("internal/", include("core.urls.internal")),
    # Public webhook endpoint (no auth required)
    path("webhook/<str:token>/", webhook_trigger_view, name="webhook_trigger"),
    # Public inbound chat webhook for Channels (signature-verified, no auth).
    path("channels/<str:token>/", channel_webhook_view, name="channel_webhook"),
    # Plugin public pages (capability URLs — no session, per-IP rate-limited).
    path("p/<str:token>/", plugin_public_page_view, name="plugin_public_page"),
    path("", lambda request: redirect("auth:login")),
]


# Auto-mount each loaded plugin at /plugins/<slug>/. Each mount is guarded: a
# plugin whose urls.py (or a view it imports) is broken simply doesn't mount —
# core routes are unaffected. The set of plugins here is already constrained to
# those that passed the guarded loader in settings.py.
for _app in getattr(settings, "INSTALLED_PLUGINS", []):
    _slug = _app.rsplit(".", 1)[-1]
    try:
        importlib.import_module(f"{_app}.urls")
        urlpatterns.append(path(f"plugins/{_slug}/", include(f"{_app}.urls")))
    except ModuleNotFoundError as _exc:
        # No urls.py at all is fine (a plugin may have no routes); anything else
        # is a real failure to skip.
        if _exc.name not in (f"{_app}.urls",):
            logger.exception("Plugin %r URL mount failed; skipping", _slug)
    except Exception:
        logger.exception("Plugin %r URL mount failed; skipping", _slug)
