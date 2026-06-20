"""
Run views for the control panel.
"""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse

from core.models import Run, Script

RUNS_PER_PAGE = 25


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with optional filtering."""
    runs = (
        Run.objects.for_workspace(request.workspace)
        .select_related("script", "triggered_by")
        .order_by("-created_at")
    )

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter and status_filter in dict(Run.Status.choices):
        runs = runs.filter(status=status_filter)

    # Optional filtering by script
    script_filter = request.GET.get("script")
    if script_filter:
        runs = runs.filter(script_id=script_filter)

    # Get the active workspace's scripts for the filter dropdown
    scripts = Script.objects.for_workspace(request.workspace).order_by("name")

    # Paginate
    paginator = Paginator(runs, RUNS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1
    )

    # Preserve active filters in pagination links (everything except `page`)
    params = request.GET.copy()
    params.pop("page", None)
    querystring = params.urlencode()

    return render(request, "cpanel/runs/list.html", {
        "runs": page_obj,
        "page_obj": page_obj,
        "page_range": page_range,
        "querystring": querystring,
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
        pk=pk,
        workspace=request.workspace,
    )

    return render(request, "cpanel/runs/detail.html", {"run": run})
