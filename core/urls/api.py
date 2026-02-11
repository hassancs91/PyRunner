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

app_name = "api"

urlpatterns = [
    # Datastore endpoints
    path("datastores/", list_datastores, name="list_datastores"),
    path("datastores/<str:name>/", get_datastore, name="get_datastore"),
    path("datastores/<str:name>/entries/", list_entries, name="list_entries"),
    path("datastores/<str:name>/entries/<str:key>/", get_entry, name="get_entry"),
]
