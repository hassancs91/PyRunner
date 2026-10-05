"""
Run views for the control panel.
"""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse

from core.models import Run, Script

RUNS_PER_PAGE = 25
SEARCH_MAX_LENGTH = 200


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with optional filtering."""
    # Same pattern as the packages page: viewing the list reconciles runs whose
    # worker died without finalizing them, so a stuck 'running' row heals on
    # sight even when the cluster (and its every-minute heartbeat) is down.
    Run.reconcile_stale()

    runs = (
        Run.objects.for_workspace(request.workspace)
        .select_related("script", "triggered_by")
        # The list renders stderr (the inline error expander) but never stdout,
        # which is the far larger column. Deferring it keeps a page of runs small.
        .defer("stdout")
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

    # Text search: script name + stderr by default (the "which run had this
    # error?" question), stdout only when asked — it is the big column, and
    # an icontains over it is an honest sequential scan at self-hosted scale.
    # The filter runs in SQL, so stdout stays deferred from the SELECT either
    # way. Capped so a pasted traceback can't become a multi-KB LIKE pattern.
    search_query = request.GET.get("q", "").strip()[:SEARCH_MAX_LENGTH]
    search_output = request.GET.get("deep") == "1"
    if search_query:
        match = Q(script__name__icontains=search_query) | Q(stderr__icontains=search_query)
        if search_output:
            match |= Q(stdout__icontains=search_query)
        runs = runs.filter(match)

    # Get the active workspace's scripts for the filter dropdown
    scripts = Script.objects.for_workspace(request.workspace).order_by("name")

    # Paginate
    paginator = Paginator(runs, RUNS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1
    )

    # Preserve active filters in pagination links (everything except `page`),
    # and in the status tabs / script picker (everything except the one being
    # changed) so switching one filter never drops the others.
    params = request.GET.copy()
    params.pop("page", None)
    querystring = params.urlencode()
    without_status = params.copy()
    without_status.pop("status", None)
    without_script = params.copy()
    without_script.pop("script", None)

    return render(request, "cpanel/runs/list.html", {
        "runs": page_obj,
        "page_obj": page_obj,
        "page_range": page_range,
        "querystring": querystring,
        "querystring_without_status": without_status.urlencode(),
        "querystring_without_script": without_script.urlencode(),
        "status_choices": Run.Status.choices,
        "status_filter": status_filter,
        "script_filter": script_filter,
        "search_query": search_query,
        "search_output": search_output,
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
