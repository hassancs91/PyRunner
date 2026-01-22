PyRunner — Full Implementation Plan

Name: PyRunner
Description: Self-hosted Python script automation platform
Target Users: Solo developers, indie hackers, small teams
Core Value: Upload a script, schedule it, monitor it — nothing else to configure
License: Open source (MIT)
Deployment: Single Docker container

Phase 1: Core MVP ✅ (Completed)
Scope

Project setup (Django, django-q2, Tailwind)
User auth (magic link only)
Environment model + default env creation
Script model (paste code only)
Manual run mode only
Basic executor (subprocess, capture output)
Run model + history
Cpanel dashboard (list scripts, run, view logs)

Deliverables

 Django project structure
 Custom User model with email auth
 MagicToken model for passwordless login
 Environment model with venv support
 Script model (name, description, code)
 Run model (status, stdout, stderr, timestamps)
 Script executor with subprocess
 django-q2 async task execution
 Basic dashboard UI
 Script CRUD views
 Run history views
 Tailwind styling


Phase 2: Scheduling ✅ (Completed)
Goal
Enable automated script execution on time-based schedules.
Features
Run Modes

Manual — Run on-demand from dashboard (existing)
Interval — Run every X minutes

Minimum interval: 5 minutes
Configurable: 5, 10, 15, 30, 60 minutes, etc.


Daily — Run at specific time(s)

Support multiple times per day
Timezone selection
Examples: "09:00 UTC", "09:00, 18:00 America/New_York"



Schedule Management

Enable/disable schedule without deleting
View next scheduled run time
View last run time
Pause all schedules (global toggle)
Schedule history (when schedule was changed)

django-q2 Integration

Create Schedule objects for each scheduled script
Link Schedule ID to Script model
Handle schedule updates (delete old, create new)
Reschedule daily tasks after each run

Deliverables

✅ ScriptSchedule model (run_mode, interval_minutes, daily_times, timezone, is_active)
✅ ScheduleHistory model (change tracking for audit)
✅ GlobalSettings model (global pause functionality)
✅ Run.trigger_type field (manual/scheduled/api)
✅ ScheduleService for django-q2 integration
✅ ScheduleForm with validation
✅ Script edit view with schedule configuration
✅ Schedule toggle view
✅ Schedule history view
✅ Global settings view with pause/resume
✅ Updated templates (detail page schedule card, edit form, sidebar)


Phase 3: Environments & Packages ✅ (Completed)
Goal
Allow users to manage Python environments and install packages via UI.
Features
Environment Management

Create new environments

Name and description
Python version selection (from available on system)
Auto-create venv in designated folder


Edit environment details
Delete environment

Only if no scripts assigned
Confirmation required
Removes venv folder


Set default environment
View environment details

Creation date
Python version
Assigned scripts count
Disk usage



Package Management

View installed packages list

Package name and version
Sortable, searchable


Install package

Text input for package name
Support version pinning (e.g., "requests==2.31.0")
Show installation progress/output
Success/error feedback


Uninstall package

Confirmation required
Show uninstall output


Bulk install from requirements.txt

Paste or upload requirements
Install all at once


Export requirements.txt

Download current packages as file



Script-Environment Assignment

Select environment when creating script
Change environment on existing script
Show environment name in script list
Warning when changing environment (may affect script)

Deliverables

✅ PackageOperation model (operation tracking for async pip operations)
✅ EnvironmentService (venv creation, pip operations, Python discovery)
✅ EnvironmentCreateForm, EnvironmentEditForm, PackageInstallForm, BulkInstallForm
✅ Environment CRUD views (create, edit, delete, set default)
✅ Package management views (list, install, uninstall, bulk install, export)
✅ execute_package_operation async task for django-q2
✅ Environment templates (create, edit, packages)
✅ Updated environment list/detail templates with action buttons
✅ Disk usage display
✅ Python version auto-discovery (py launcher, PATH)


Phase 4: Secrets Management ✅ (Completed)
Goal
Secure storage for API keys and credentials, injected into scripts at runtime.
Features
Secret Storage

Add secret (key-value pair)

Key: uppercase, underscores allowed (e.g., API_KEY)
Value: any string, encrypted at rest
Optional description


Edit secret

Can update value
Can update description
Key is immutable after creation


Delete secret

Confirmation required
Warning about scripts that may use it


View secrets list

Key name
Masked value preview (e.g., "sk-...abc")
Description
Created/updated dates



Secret Injection

All secrets injected as environment variables
Available to all scripts automatically
Scripts access via os.environ['KEY_NAME']
No configuration needed per-script

Security

Encryption at rest using Fernet (symmetric)
Encryption key stored in environment variable
Never log or display full secret values
Mask secrets in script output

Deliverables

✅ Secret model (key, encrypted_value, description, timestamps)
✅ EncryptionService (Fernet encryption/decryption utilities)
✅ Run migrations (0004_secret.py)
✅ ENCRYPTION_KEY in settings.py
✅ .env.example file
✅ SecretCreateForm, SecretEditForm
✅ Secret CRUD views (list, create, edit, delete)
✅ Secrets templates (list.html, create.html, edit.html)
✅ Sidebar navigation with Secrets link
✅ Executor updated to inject secrets as environment variables
✅ Output masking to prevent accidental secret exposure


Phase 5: Webhooks ✅ (Completed)
Goal
Enable external triggers via webhooks so external services can run scripts.
Features
Webhook Triggers

Webhook as a run mode option
Auto-generate unique webhook URL per script
Accept GET and POST requests
Return JSON response with run ID and status
Pass request body to script as environment variable (JSON string)
Pass query params to script as environment variables
Regenerate webhook token (invalidates old URL)
Copy webhook URL button

Webhook Response

Immediate response (don't wait for script to finish)
Response payload:

status: "queued"
run_id: UUID of created run
script: script name


Optional: synchronous mode (wait for result, with timeout) - Future

Webhook Security

Unique token per script (64 chars, URL-safe)
Token in URL path (not query param)
Only enabled scripts can be triggered
Rate limiting per token (optional, future)
IP whitelist (optional, future)

Use Cases

GitHub webhooks (on push, run deploy script)
Stripe webhooks (on payment, run fulfillment)
Zapier/Make integration
Cron services (external cron triggers PyRunner)
Manual curl/Postman triggers for testing

Deliverables

✅ webhook_token field added to Script model (64 chars, unique, nullable)
✅ Migration 0005_add_webhook_token.py
✅ Script.generate_webhook_token(), create_webhook_token(), regenerate_webhook_token(), clear_webhook_token() methods
✅ Script.has_webhook property
✅ webhook_trigger_view (public endpoint, GET/POST handler)
✅ webhook_enable_view, webhook_disable_view, webhook_regenerate_view (authenticated)
✅ Public webhook URL route: /webhook/<token>/
✅ Executor updated to inject WEBHOOK_METHOD, WEBHOOK_QUERY, WEBHOOK_BODY, WEBHOOK_BODY_JSON, WEBHOOK_CONTENT_TYPE
✅ tasks.py updated to pass webhook_data through queue
✅ Script detail page webhook card with:
  - Enable/disable webhook toggle
  - Webhook URL display with copy button
  - Example curl command
  - Regenerate URL button


Phase 6: Notifications ✅ (Completed)
Goal
Alert users when scripts succeed or fail via email and webhooks.
Features
Notification Settings (Global)

Email backend selection

Disabled
SMTP
Resend API


SMTP configuration

Host, port
Username, password
TLS/SSL toggle
From email address


Resend configuration

API key
From email address


Default notification email
Test email button

Per-Script Notifications

Notify on: never, failure, success, both
Override email (optional, uses global default)
Webhook notification URL
Webhook notification toggle

Email Notifications

Send on script completion based on settings
Email content:

Script name
Run status (success/failed/timeout)
Duration
Error excerpt (if failed)
Link to run details


HTML and plain text versions

Webhook Notifications

POST JSON payload to configured URL
Payload contents:

Event type: "run_completed"
Script: id, name
Run: id, status, exit_code, duration
Timestamps: started_at, ended_at
Error: stderr excerpt (if failed)


Timeout: 10 seconds
Fail silently (don't break script execution)

UI Changes

Notification settings page

Backend selection tabs
SMTP form fields
Resend form fields
Default email input
Test email button


Script form — Notification section

Notify on dropdown
Override email input
Webhook URL input


Script detail — Notification settings display

Deliverables

✅ Email backend fields added to GlobalSettings model (email_backend, smtp_*, resend_*, default_notification_email)
✅ Notification fields added to Script model (notify_on, notify_email, notify_webhook_url, notify_webhook_enabled)
✅ Migration 0006_notification_settings.py
✅ NotificationService (core/services/notification_service.py) with email and webhook sending
✅ NotificationSettingsForm for global settings
✅ ScriptForm updated with notification fields
✅ Settings view with notification settings and test email endpoint
✅ Settings template with tabbed interface (Schedule Control, Email Notifications)
✅ Email templates (run_completed.html, run_completed.txt, test_email.html)
✅ Script form template with notification section
✅ Script detail template with notifications card
✅ Task integration in execute_run_task() to send notifications after run completion


Phase 7: General Settings & Log Retention ✅ (Completed)
Goal
Enable instance customization and automatic cleanup of old run logs.
Features
General Settings

Instance name (shown in header/emails)
Timezone selection
Date/time format preferences (YYYY-MM-DD, MM/DD/YYYY, DD/MM/YYYY, DD.MM.YYYY)
Time format preferences (12h, 24h)

Log Retention

Global retention policy

Keep runs for X days (0 = forever)
Keep last X runs per script (0 = unlimited)

Per-script retention override
Auto-cleanup scheduled task (daily at 2 AM)
Manual cleanup button with preview

Deliverables

✅ GlobalSettings model fields (instance_name, timezone, date_format, time_format, retention_days, retention_count, auto_cleanup_enabled, last_cleanup_at)
✅ Script model fields (retention_days_override, retention_count_override)
✅ Migration 0007_phase7_settings.py
✅ RetentionService (core/services/retention_service.py) with cleanup logic and schedule management
✅ GeneralSettingsForm, LogRetentionForm in forms.py
✅ Settings views (general_settings_view, retention_settings_view, manual_cleanup_view, cleanup_preview_view)
✅ URL routes for new settings endpoints
✅ Settings template with 4 tabs (General, Schedule Control, Email Notifications, Log Retention)
✅ cleanup_old_runs_task in tasks.py
✅ Preview functionality showing cleanup statistics before deletion


Phase 8: System Information ✅ (Completed)
Goal
Display system metrics and application health information in the Settings page.
Features
System Information

Version display (PyRunner version from version.py)
Uptime (time since app startup)
Database size (SQLite file size)
Environments disk usage (total size of virtual environments)
Python version (interpreter version)
django-q worker status (queue health and task metrics)

Deliverables

✅ pyrunner/version.py with __version__ constant
✅ APP_START_TIME capture in core/apps.py for uptime tracking
✅ SystemInfoService (core/services/system_info_service.py) with all metrics gathering
✅ system_info_view AJAX endpoint in core/views/settings.py
✅ URL route settings/system-info/ in core/urls/cpanel.py
✅ Settings template with 5 tabs (General, Schedule Control, Email Notifications, Log Retention, System Info)
✅ JavaScript loadSystemInfo() function with auto-refresh on tab selection
✅ Worker heartbeat mechanism for reliable status detection:
  - worker_heartbeat_at field in GlobalSettings
  - post_execute signal handler updates heartbeat on task completion
  - Scheduled heartbeat task runs every minute
  - Migration 0008_worker_heartbeat.py


Phase 9: Dashboard Improvements ✅ (Completed)
Goal
Enhance the dashboard with statistics, widgets, and system health indicators.
Features
Statistics Cards

Total scripts
Active scripts (enabled)
Runs today
Runs this week
Success rate (%)
Queue size


Recent failures widget (last 5)
Upcoming scheduled runs widget
Quick actions (run, enable/disable) on recent scripts
System health indicator (worker status)
System alerts (workers stopped, schedules paused)

Deliverables

✅ DashboardService (core/services/dashboard_service.py) with statistics and widget data methods
✅ Updated dashboard view with new context data
✅ Updated dashboard template with:
  - System health alerts (workers stopped, schedules paused banners)
  - System health indicator in header (worker status dot)
  - 6 statistics cards in 2 rows (Total Scripts, Active Scripts, Success Rate, Runs Today, Runs This Week, Queue Size)
  - Recent Failures widget with empty state
  - Upcoming Scheduled Runs widget with empty state
  - Quick actions (Run, Enable/Disable) on recent scripts
  - Enhanced Quick Actions section with Environments and Settings links


Phase 10: Backup & Restore ✅ (Completed)
Goal
Enable users to easily backup and restore their PyRunner instance data.
Features
Backup Creation

Export complete instance data to JSON file
Configurable options:

Include run history (default: 1000 most recent)
Include package operations
Run count limit (0 = all)


JSON format with versioning (v1.0.0)
Encrypted data stays encrypted (requires matching ENCRYPTION_KEY)
SHA256 hash of ENCRYPTION_KEY for validation
One-click download with timestamp in filename

Backup Restore

Upload JSON backup file
Validation before restore:

Structure validation
ENCRYPTION_KEY hash verification
Foreign key reference validation


Preview modal with confirmation:

Shows backup metadata (source, date, created by)
Displays counts (X scripts, Y runs, etc.)
Warnings about data deletion
Explicit confirmation checkbox


Full replace mode:

Delete all existing data
Import from backup
Regenerate django-q2 schedules
Wrapped in database transaction (atomic)


Automatic backup before restore (safety net)
Clear success/error messages

Safety Features

Multiple confirmation steps (upload → preview → confirm)
Transaction safety (rollback on any error)
Automatic backup of current state before restore
ENCRYPTION_KEY validation (fail early if mismatch)
File size limits (100MB max)
Foreign key integrity checks
Clear error messages with actionable guidance

Backup Contents

Always included:

GlobalSettings (singleton configuration)
Environments (metadata only, not venv files)
Users (basic info, no passwords)
Scripts (with webhook tokens preserved)
ScriptSchedules (q_schedule_ids regenerated on restore)
ScheduleHistory (audit trail)
Secrets (encrypted values preserved)


Optional (configurable):

Runs (execution history, default 1000 most recent)
PackageOperations (pip operation history)



Deliverables

✅ BackupService (core/services/backup_service.py)

create_backup() - Export all data to JSON structure
validate_backup() - Validate structure and ENCRYPTION_KEY
validate_encryption_key() - Verify hash match
get_backup_preview() - Preview summary for confirmation
restore_backup() - Import data with transaction safety
Export/import methods for each model
Schedule regeneration using ScheduleService


✅ Backup views (core/views/backup.py)

backup_create_view() - Create and download backup file
backup_upload_view() - AJAX endpoint for file upload
backup_preview_view() - AJAX endpoint for preview
backup_restore_view() - Execute restore with validation


✅ Backup forms (core/forms.py)

BackupCreateForm - Options for backup creation
BackupRestoreForm - Upload and confirmation


✅ URL routes (core/urls/cpanel.py)

/cpanel/settings/backup/create/
/cpanel/settings/backup/upload/
/cpanel/settings/backup/preview/
/cpanel/settings/backup/restore/


✅ UI Implementation (templates/cpanel/settings.html)

"Backup & Restore" tab in Settings page
Create Backup section with options form
Restore Backup section with file upload
Preview modal with detailed summary and confirmation
JavaScript for AJAX upload and preview handling
Success/error feedback with clear messaging




