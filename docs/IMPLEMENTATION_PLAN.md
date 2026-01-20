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


TO Implement:
