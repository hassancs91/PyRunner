"""
URL patterns for control panel.
"""
from django.urls import path
from core.views.dashboard import dashboard_view, system_resources_api
from core.views.scripts import (
    script_list_view,
    script_create_view,
    script_detail_view,
    script_edit_view,
    script_run_view,
    script_toggle_view,
    script_archive_view,
    script_restore_view,
    script_delete_view,
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
    general_settings_view,
    retention_settings_view,
    worker_settings_view,
    restart_workers_view,
    manual_cleanup_view,
    cleanup_preview_view,
    system_info_view,
)
from core.views.secrets import (
    secret_list_view,
    secret_create_view,
    secret_edit_view,
    secret_delete_view,
)
from core.views.tags import (
    tag_list_view,
    tag_create_view,
    tag_edit_view,
    tag_delete_view,
)
from core.views.datastores import (
    datastore_list_view,
    datastore_create_view,
    datastore_detail_view,
    datastore_edit_view,
    datastore_delete_view,
    datastore_clear_view,
    datastore_entry_create_view,
    datastore_entry_edit_view,
    datastore_entry_delete_view,
)
from core.views.backup import (
    backup_create_view,
    backup_upload_view,
    backup_preview_view,
    backup_restore_view,
)
from core.views.users import (
    user_list_view,
    invite_user_view,
    revoke_invite_view,
    toggle_registration_view,
    delete_user_view,
)
from core.views.logs import (
    logs_view,
    logs_api_view,
    logs_clear_view,
)
from core.views.api_tokens import (
    api_token_list_view,
    api_token_create_view,
    api_token_created_view,
    api_token_revoke_view,
    api_token_toggle_view,
)
from core.views.tasks import (
    tasks_view,
    tasks_api_view,
    task_cancel_view,
    task_force_stop_view,
)
from core.views.services import (
    services_view,
    s3_settings_view,
    s3_test_connection_view,
)

app_name = "cpanel"

urlpatterns = [
    # Dashboard
    path("", dashboard_view, name="dashboard"),
    path("api/system-resources/", system_resources_api, name="system_resources_api"),

    # Scripts
    path("scripts/", script_list_view, name="script_list"),
    path("scripts/create/", script_create_view, name="script_create"),
    path("scripts/<uuid:pk>/", script_detail_view, name="script_detail"),
    path("scripts/<uuid:pk>/edit/", script_edit_view, name="script_edit"),
    path("scripts/<uuid:pk>/run/", script_run_view, name="script_run"),
    path("scripts/<uuid:pk>/toggle/", script_toggle_view, name="script_toggle"),
    path("scripts/<uuid:pk>/schedule/toggle/", schedule_toggle_view, name="schedule_toggle"),
    path("scripts/<uuid:pk>/schedule/history/", schedule_history_view, name="schedule_history"),
    # Script archive/restore/delete
    path("scripts/<uuid:pk>/archive/", script_archive_view, name="script_archive"),
    path("scripts/<uuid:pk>/restore/", script_restore_view, name="script_restore"),
    path("scripts/<uuid:pk>/delete/", script_delete_view, name="script_delete"),
    # Webhooks
    path("scripts/<uuid:pk>/webhook/enable/", webhook_enable_view, name="webhook_enable"),
    path("scripts/<uuid:pk>/webhook/disable/", webhook_disable_view, name="webhook_disable"),
    path("scripts/<uuid:pk>/webhook/regenerate/", webhook_regenerate_view, name="webhook_regenerate"),

    # Runs
    path("runs/", run_list_view, name="run_list"),
    path("runs/<uuid:pk>/", run_detail_view, name="run_detail"),

    # Tasks
    path("tasks/", tasks_view, name="tasks"),
    path("api/tasks/", tasks_api_view, name="tasks_api"),
    path("tasks/<str:task_id>/cancel/", task_cancel_view, name="task_cancel"),
    path("tasks/<str:task_id>/force-stop/", task_force_stop_view, name="task_force_stop"),

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

    # Tags
    path("tags/", tag_list_view, name="tag_list"),
    path("tags/create/", tag_create_view, name="tag_create"),
    path("tags/<uuid:pk>/edit/", tag_edit_view, name="tag_edit"),
    path("tags/<uuid:pk>/delete/", tag_delete_view, name="tag_delete"),

    # Data Stores
    path("datastores/", datastore_list_view, name="datastore_list"),
    path("datastores/create/", datastore_create_view, name="datastore_create"),
    path("datastores/<uuid:pk>/", datastore_detail_view, name="datastore_detail"),
    path("datastores/<uuid:pk>/edit/", datastore_edit_view, name="datastore_edit"),
    path("datastores/<uuid:pk>/delete/", datastore_delete_view, name="datastore_delete"),
    path("datastores/<uuid:pk>/clear/", datastore_clear_view, name="datastore_clear"),
    path("datastores/<uuid:pk>/entries/create/", datastore_entry_create_view, name="datastore_entry_create"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/edit/", datastore_entry_edit_view, name="datastore_entry_edit"),
    path("datastores/<uuid:pk>/entries/<uuid:entry_pk>/delete/", datastore_entry_delete_view, name="datastore_entry_delete"),

    # Settings
    path("settings/", settings_view, name="settings"),
    path("settings/toggle-pause/", toggle_global_pause_view, name="toggle_global_pause"),
    path("settings/notifications/", notification_settings_view, name="notification_settings"),
    path("settings/test-email/", test_email_view, name="test_email"),
    path("settings/general/", general_settings_view, name="general_settings"),
    path("settings/retention/", retention_settings_view, name="retention_settings"),
    path("settings/workers/", worker_settings_view, name="worker_settings"),
    path("settings/restart-workers/", restart_workers_view, name="restart_workers"),
    path("settings/cleanup/", manual_cleanup_view, name="manual_cleanup"),
    path("settings/cleanup-preview/", cleanup_preview_view, name="cleanup_preview"),
    path("settings/system-info/", system_info_view, name="system_info"),

    # Backup & Restore
    path("settings/backup/create/", backup_create_view, name="backup_create"),
    path("settings/backup/upload/", backup_upload_view, name="backup_upload"),
    path("settings/backup/preview/", backup_preview_view, name="backup_preview"),
    path("settings/backup/restore/", backup_restore_view, name="backup_restore"),

    # User Management
    path("users/", user_list_view, name="user_list"),
    path("users/invite/", invite_user_view, name="invite_user"),
    path("users/invite/<int:pk>/revoke/", revoke_invite_view, name="revoke_invite"),
    path("users/<int:pk>/delete/", delete_user_view, name="delete_user"),
    path("users/toggle-registration/", toggle_registration_view, name="toggle_registration"),

    # Logs
    path("logs/", logs_view, name="logs"),
    path("api/logs/", logs_api_view, name="logs_api"),
    path("api/logs/clear/", logs_clear_view, name="logs_clear"),

    # API Tokens
    path("settings/api-tokens/", api_token_list_view, name="api_token_list"),
    path("settings/api-tokens/create/", api_token_create_view, name="api_token_create"),
    path("settings/api-tokens/<uuid:pk>/created/", api_token_created_view, name="api_token_created"),
    path("settings/api-tokens/<uuid:pk>/revoke/", api_token_revoke_view, name="api_token_revoke"),
    path("settings/api-tokens/<uuid:pk>/toggle/", api_token_toggle_view, name="api_token_toggle"),

    # Services
    path("services/", services_view, name="services"),
    path("services/s3/", s3_settings_view, name="s3_settings"),
    path("services/s3/test/", s3_test_connection_view, name="s3_test_connection"),
]
