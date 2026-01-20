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


To Implement now:
Phase 2: Scheduling
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

