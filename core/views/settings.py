"""
Settings views for the control panel.
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse

from core.models import GlobalSettings
from core.services.schedule_service import ScheduleService


@login_required
def settings_view(request: HttpRequest) -> HttpResponse:
    """Display global settings."""
    settings = GlobalSettings.get_settings()
    return render(request, "cpanel/settings.html", {"settings": settings})


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
