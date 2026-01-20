"""
Settings views for the control panel.
"""
import logging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import GlobalSettings
from core.services.schedule_service import ScheduleService
from core.services.notification_service import NotificationService
from core.forms import NotificationSettingsForm

logger = logging.getLogger(__name__)


@login_required
def settings_view(request: HttpRequest) -> HttpResponse:
    """Display global settings."""
    settings = GlobalSettings.get_settings()
    notification_form = NotificationSettingsForm(instance=settings)
    return render(
        request,
        "cpanel/settings.html",
        {
            "settings": settings,
            "notification_form": notification_form,
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
