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
    schedule_toggle_view,
    schedule_history_view,
)
from .runs import run_list_view, run_detail_view
from .environments import (
    environment_list_view,
    environment_detail_view,
    environment_create_view,
    environment_edit_view,
    environment_delete_view,
    environment_set_default_view,
    environment_packages_view,
    package_install_view,
    package_uninstall_view,
    bulk_install_view,
    export_requirements_view,
    package_operation_status_view,
)
from .settings import settings_view, toggle_global_pause_view

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
    "schedule_toggle_view",
    "schedule_history_view",
    # Runs
    "run_list_view",
    "run_detail_view",
    # Environments
    "environment_list_view",
    "environment_detail_view",
    "environment_create_view",
    "environment_edit_view",
    "environment_delete_view",
    "environment_set_default_view",
    "environment_packages_view",
    "package_install_view",
    "package_uninstall_view",
    "bulk_install_view",
    "export_requirements_view",
    "package_operation_status_view",
    # Settings
    "settings_view",
    "toggle_global_pause_view",
]
