"""
URL patterns for initial setup wizard.
"""

from django.urls import path

from core.views.setup import setup_view, admin_setup_view

app_name = "setup"

urlpatterns = [
    path("", setup_view, name="setup"),
    path("admin/", admin_setup_view, name="admin_setup"),
]
