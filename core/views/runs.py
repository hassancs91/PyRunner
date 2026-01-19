"""
Run views for the control panel.
"""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse

from core.models import Run, Script


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with optional filtering."""
    runs = Run.objects.select_related("script", "triggered_by").order_by("-created_at")

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter and status_filter in dict(Run.Status.choices):
        runs = runs.filter(status=status_filter)

    # Optional filtering by script
    script_filter = request.GET.get("script")
    if script_filter:
        runs = runs.filter(script_id=script_filter)

    # Get all scripts for filter dropdown
    scripts = Script.objects.all().order_by("name")

    return render(request, "cpanel/runs/list.html", {
        "runs": runs,
        "status_choices": Run.Status.choices,
        "status_filter": status_filter,
        "script_filter": script_filter,
        "scripts": scripts,
    })


@login_required
def run_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View run details including output."""
    run = get_object_or_404(
        Run.objects.select_related("script", "triggered_by"),
        pk=pk
    )

    return render(request, "cpanel/runs/detail.html", {"run": run})
