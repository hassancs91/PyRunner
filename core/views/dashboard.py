"""
Dashboard view for the control panel.
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from core.models import Script, Run, Environment


@login_required
def dashboard_view(request):
    """Main dashboard view with overview statistics."""
    # Statistics
    scripts_count = Script.objects.count()
    runs_count = Run.objects.count()
    environments_count = Environment.objects.filter(is_active=True).count()

    # Success/failure stats
    success_count = Run.objects.filter(status=Run.Status.SUCCESS).count()
    failed_count = Run.objects.filter(status__in=[Run.Status.FAILED, Run.Status.TIMEOUT]).count()

    # Recent activity
    recent_runs = Run.objects.select_related("script", "triggered_by").order_by("-created_at")[:5]
    recent_scripts = Script.objects.select_related("environment").order_by("-updated_at")[:5]

    context = {
        "scripts_count": scripts_count,
        "runs_count": runs_count,
        "environments_count": environments_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "recent_runs": recent_runs,
        "recent_scripts": recent_scripts,
    }
    return render(request, "cpanel/dashboard.html", context)
