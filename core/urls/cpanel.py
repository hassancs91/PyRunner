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
    schedule_toggle_view,
    schedule_history_view,
    webhook_enable_view,
    webhook_disable_view,
    webhook_regenerate_view,
)
from core.views.runs import run_list_view, run_detail_view
from core.views.environments import (
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
from core.views.settings import (
    settings_view,
    toggle_global_pause_view,
    notification_settings_view,
    test_email_view,
)
from core.views.secrets import (
    secret_list_view,
    secret_create_view,
    secret_edit_view,
    secret_delete_view,
)

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
    path("scripts/<uuid:pk>/schedule/toggle/", schedule_toggle_view, name="schedule_toggle"),
    path("scripts/<uuid:pk>/schedule/history/", schedule_history_view, name="schedule_history"),
    # Webhooks
    path("scripts/<uuid:pk>/webhook/enable/", webhook_enable_view, name="webhook_enable"),
    path("scripts/<uuid:pk>/webhook/disable/", webhook_disable_view, name="webhook_disable"),
    path("scripts/<uuid:pk>/webhook/regenerate/", webhook_regenerate_view, name="webhook_regenerate"),

    # Runs
    path("runs/", run_list_view, name="run_list"),
    path("runs/<uuid:pk>/", run_detail_view, name="run_detail"),

    # Environments
    path("environments/", environment_list_view, name="environment_list"),
    path("environments/create/", environment_create_view, name="environment_create"),
    path("environments/<uuid:pk>/", environment_detail_view, name="environment_detail"),
    path("environments/<uuid:pk>/edit/", environment_edit_view, name="environment_edit"),
    path("environments/<uuid:pk>/delete/", environment_delete_view, name="environment_delete"),
    path("environments/<uuid:pk>/set-default/", environment_set_default_view, name="environment_set_default"),
    # Package Management
    path("environments/<uuid:pk>/packages/", environment_packages_view, name="environment_packages"),
    path("environments/<uuid:pk>/packages/install/", package_install_view, name="package_install"),
    path("environments/<uuid:pk>/packages/uninstall/", package_uninstall_view, name="package_uninstall"),
    path("environments/<uuid:pk>/packages/bulk-install/", bulk_install_view, name="bulk_install"),
    path("environments/<uuid:pk>/packages/export/", export_requirements_view, name="export_requirements"),
    # AJAX endpoint
    path("api/package-operation/<uuid:operation_id>/status/", package_operation_status_view, name="package_operation_status"),

    # Secrets
    path("secrets/", secret_list_view, name="secret_list"),
    path("secrets/create/", secret_create_view, name="secret_create"),
    path("secrets/<uuid:pk>/edit/", secret_edit_view, name="secret_edit"),
    path("secrets/<uuid:pk>/delete/", secret_delete_view, name="secret_delete"),

    # Settings
    path("settings/", settings_view, name="settings"),
    path("settings/toggle-pause/", toggle_global_pause_view, name="toggle_global_pause"),
    path("settings/notifications/", notification_settings_view, name="notification_settings"),
    path("settings/test-email/", test_email_view, name="test_email"),
]
