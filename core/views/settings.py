"""
Settings views for the control panel.
"""
import logging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from core.models import GlobalSettings
from core.services.schedule_service import ScheduleService
from core.services.notification_service import NotificationService
from core.services.retention_service import RetentionService
from core.services.system_info_service import SystemInfoService
from core.forms import (
    NotificationSettingsForm,
    GeneralSettingsForm,
    LogRetentionForm,
    WorkerSettingsForm,
    BackupCreateForm,
    BackupRestoreForm,
)

logger = logging.getLogger(__name__)


@login_required
def settings_view(request: HttpRequest) -> HttpResponse:
    """Display global settings."""
    settings = GlobalSettings.get_settings()
    notification_form = NotificationSettingsForm(instance=settings)
    general_form = GeneralSettingsForm(instance=settings)
    retention_form = LogRetentionForm(instance=settings)
    worker_form = WorkerSettingsForm(instance=settings)
    backup_create_form = BackupCreateForm()
    backup_restore_form = BackupRestoreForm()
    return render(
        request,
        "cpanel/settings.html",
        {
            "settings": settings,
            "notification_form": notification_form,
            "general_form": general_form,
            "retention_form": retention_form,
            "worker_form": worker_form,
            "backup_create_form": backup_create_form,
            "backup_restore_form": backup_restore_form,
        },
    )


@login_required
@require_POST
def toggle_global_pause_view(request: HttpRequest) -> HttpResponse:
    """Toggle global schedule pause."""
    settings = GlobalSettings.get_settings()

    if settings.schedules_paused:
        count = ScheduleService.resume_all_schedules()
        messages.success(request, f"All schedules resumed. {count} schedules reactivated.")
    else:
        count = ScheduleService.pause_all_schedules(user=request.user)
        messages.warning(request, f"All schedules paused. {count} schedules deactivated.")

    return redirect("cpanel:settings")


@login_required
@require_POST
def notification_settings_view(request: HttpRequest) -> HttpResponse:
    """Update notification settings."""
    settings = GlobalSettings.get_settings()
    form = NotificationSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Notification settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:settings")


@login_required
@require_POST
def test_email_view(request: HttpRequest) -> JsonResponse:
    """Send a test email to verify configuration."""
    settings = GlobalSettings.get_settings()

    if settings.email_backend == GlobalSettings.EmailBackend.DISABLED:
        return JsonResponse({
            "success": False,
            "error": "Email backend is disabled. Please configure an email backend first.",
        })

    recipient = settings.default_notification_email
    if not recipient:
        return JsonResponse({
            "success": False,
            "error": "No default notification email configured.",
        })

    try:
        NotificationService.send_test_email(recipient)
        return JsonResponse({
            "success": True,
            "message": f"Test email sent to {recipient}",
        })
    except Exception as e:
        logger.exception("Failed to send test email")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })


@login_required
@require_POST
def general_settings_view(request: HttpRequest) -> HttpResponse:
    """Update general settings."""
    settings = GlobalSettings.get_settings()
    form = GeneralSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "General settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:settings")


@login_required
@require_POST
def retention_settings_view(request: HttpRequest) -> HttpResponse:
    """Update log retention settings."""
    settings = GlobalSettings.get_settings()
    form = LogRetentionForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Log retention settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:settings")


@login_required
@require_POST
def worker_settings_view(request: HttpRequest) -> HttpResponse:
    """Update worker settings."""
    settings = GlobalSettings.get_settings()
    form = WorkerSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.warning(
            request, "Worker settings saved. Restart workers for changes to take effect."
        )
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:settings")


@login_required
@require_POST
def restart_workers_view(request: HttpRequest) -> JsonResponse:
    """Trigger a worker restart via management command."""
    import subprocess
    import sys
    from django.conf import settings as django_settings

    try:
        # Run the restart_workers management command
        result = subprocess.run(
            [sys.executable, "manage.py", "restart_workers", "--timeout", "15"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=django_settings.BASE_DIR,
        )

        if result.returncode == 0:
            return JsonResponse({
                "success": True,
                "message": "Workers restart initiated successfully.",
                "output": result.stdout,
            })
        else:
            return JsonResponse({
                "success": False,
                "error": result.stderr or result.stdout or "Unknown error",
            })

    except subprocess.TimeoutExpired:
        return JsonResponse({
            "success": True,
            "message": "Restart initiated. Workers may still be restarting.",
        })
    except Exception as e:
        logger.exception("Failed to restart workers")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })


@login_required
@require_POST
def manual_cleanup_view(request: HttpRequest) -> HttpResponse:
    """Trigger manual cleanup of old runs."""
    try:
        deleted_count = RetentionService.cleanup_all_runs()

        # Update last_cleanup_at timestamp
        settings = GlobalSettings.get_settings()
        settings.last_cleanup_at = timezone.now()
        settings.save(update_fields=["last_cleanup_at"])

        if deleted_count > 0:
            messages.success(request, f"Cleanup completed. {deleted_count} runs deleted.")
        else:
            messages.info(request, "No runs to clean up based on current retention settings.")
    except Exception as e:
        logger.exception("Manual cleanup failed")
        messages.error(request, f"Cleanup failed: {e}")

    return redirect("cpanel:settings")


@login_required
def cleanup_preview_view(request: HttpRequest) -> JsonResponse:
    """Get preview of what would be cleaned up."""
    try:
        stats = RetentionService.get_cleanup_stats()
        return JsonResponse({
            "success": True,
            "stats": stats,
        })
    except Exception as e:
        logger.exception("Failed to get cleanup preview")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })


@login_required
def system_info_view(request: HttpRequest) -> JsonResponse:
    """Get system information via AJAX."""
    try:
        info = SystemInfoService.get_all_info()
        return JsonResponse({
            "success": True,
            "data": info,
        })
    except Exception as e:
        logger.exception("Failed to get system info")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })
