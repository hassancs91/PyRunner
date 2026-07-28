"""
URL patterns for the PyRunner REST API.
"""

from django.urls import path

from core.views.api import (
    list_datastores,
    get_datastore,
    list_entries,
    get_entry,
)
from core.views.api.plugins import (
    plugin_api_discovery,
    plugin_api_index,
    plugin_api_dispatch,
)

app_name = "api"

urlpatterns = [
    # Datastore endpoints
    path("datastores/", list_datastores, name="list_datastores"),
    path("datastores/<str:name>/", get_datastore, name="get_datastore"),
    path("datastores/<str:name>/entries/", list_entries, name="list_entries"),
    path("datastores/<str:name>/entries/<str:key>/", get_entry, name="get_entry"),
    # Plugin API — ONE core-owned dispatcher; plugins have zero URL surface
    # here (their session-authed UI lives under /plugins/<slug>/ instead).
    path("plugins/", plugin_api_discovery, name="plugin_api_discovery"),
    path("plugins/<str:slug>/", plugin_api_index, name="plugin_api_index"),
    path(
        "plugins/<str:slug>/<str:resource>/",
        plugin_api_dispatch,
        name="plugin_api_dispatch",
    ),
    path(
        "plugins/<str:slug>/<str:resource>/<str:item_id>/",
        plugin_api_dispatch,
        name="plugin_api_dispatch_item",
    ),
]
