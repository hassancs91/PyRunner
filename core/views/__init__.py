"""
Views for the core app.
"""
from .auth import login_view, logout_view, verify_view, magic_link_sent_view
from .dashboard import dashboard_view
from .scripts import (
    script_list_view,
    script_create_view,
    script_detail_view,
    script_edit_view,
    script_run_view,
    script_toggle_view,
)
from .runs import run_list_view, run_detail_view
from .environments import environment_list_view, environment_detail_view

__all__ = [
    # Auth
    "login_view",
    "logout_view",
    "verify_view",
    "magic_link_sent_view",
    # Dashboard
    "dashboard_view",
    # Scripts
    "script_list_view",
    "script_create_view",
    "script_detail_view",
    "script_edit_view",
    "script_run_view",
    "script_toggle_view",
    # Runs
    "run_list_view",
    "run_detail_view",
    # Environments
    "environment_list_view",
    "environment_detail_view",
]
