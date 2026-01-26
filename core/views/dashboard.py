"""
Dashboard view for the control panel.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from core.models import Environment, Run, Script
from core.services.dashboard_service import DashboardService
from core.services.system_info_service import SystemInfoService


@login_required
def dashboard_view(request):
    """Main dashboard view with overview statistics."""
    # Get statistics from service
    stats = DashboardService.get_statistics()

    # Legacy counts (for backwards compatibility)
    runs_count = Run.objects.count()
    environments_count = Environment.objects.filter(is_active=True).count()
    success_count = Run.objects.filter(status=Run.Status.SUCCESS).count()
    failed_count = Run.objects.filter(
        status__in=[Run.Status.FAILED, Run.Status.TIMEOUT]
    ).count()

    # Recent activity
    recent_runs = Run.objects.select_related("script", "triggered_by").order_by(
        "-created_at"
    )[:5]
    recent_scripts = Script.objects.select_related("environment").order_by(
        "-updated_at"
    )[:5]

    # New widgets
    recent_failures = DashboardService.get_recent_failures()
    upcoming_runs = DashboardService.get_upcoming_scheduled_runs()
    system_health = DashboardService.get_system_health()
    system_resources = SystemInfoService.get_system_resources()

    context = {
        # Statistics cards
        "scripts_count": stats["total_scripts"],
        "active_scripts_count": stats["active_scripts"],
        "runs_count": runs_count,
        "runs_today": stats["runs_today"],
        "runs_this_week": stats["runs_this_week"],
        "success_rate": stats["success_rate"],
        "queue_size": stats["queue_size"],
        "environments_count": environments_count,
        "success_count": success_count,
        "failed_count": failed_count,
        # Recent activity
        "recent_runs": recent_runs,
        "recent_scripts": recent_scripts,
        # New widgets
        "recent_failures": recent_failures,
        "upcoming_runs": upcoming_runs,
        # System health
        "system_health": system_health,
        # System resources (CPU, RAM, Storage)
        "system_resources": system_resources,
    }
    return render(request, "cpanel/dashboard.html", context)


@login_required
def system_resources_api(request):
    """AJAX endpoint for system resource metrics."""
    resources = SystemInfoService.get_system_resources()
    return JsonResponse(resources)
