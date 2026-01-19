"""
URL patterns for control panel.
"""
from django.urls import path
from core.views.dashboard import dashboard_view
from core.views.scripts import (
    script_list_view,
    script_create_view,
    script_detail_view,
    script_edit_view,
    script_run_view,
    script_toggle_view,
)
from core.views.runs import run_list_view, run_detail_view
from core.views.environments import environment_list_view, environment_detail_view

app_name = "cpanel"

urlpatterns = [
    # Dashboard
    path("", dashboard_view, name="dashboard"),

    # Scripts
    path("scripts/", script_list_view, name="script_list"),
    path("scripts/create/", script_create_view, name="script_create"),
    path("scripts/<uuid:pk>/", script_detail_view, name="script_detail"),
    path("scripts/<uuid:pk>/edit/", script_edit_view, name="script_edit"),
    path("scripts/<uuid:pk>/run/", script_run_view, name="script_run"),
    path("scripts/<uuid:pk>/toggle/", script_toggle_view, name="script_toggle"),

    # Runs
    path("runs/", run_list_view, name="run_list"),
    path("runs/<uuid:pk>/", run_detail_view, name="run_detail"),

    # Environments
    path("environments/", environment_list_view, name="environment_list"),
    path("environments/<uuid:pk>/", environment_detail_view, name="environment_detail"),
]
