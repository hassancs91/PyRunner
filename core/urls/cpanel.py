"""
URL patterns for control panel.
"""
from django.urls import path
from core.views.dashboard import dashboard_view

app_name = "cpanel"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
]
