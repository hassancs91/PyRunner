"""
Script views for the control panel.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse

from core.models import Script, Run
from core.forms import ScriptForm


@login_required
def script_list_view(request: HttpRequest) -> HttpResponse:
    """List all scripts with optional filtering."""
    scripts = Script.objects.select_related("environment", "created_by").order_by("-updated_at")

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter == "enabled":
        scripts = scripts.filter(is_enabled=True)
    elif status_filter == "disabled":
        scripts = scripts.filter(is_enabled=False)

    return render(request, "cpanel/scripts/list.html", {
        "scripts": scripts,
        "status_filter": status_filter,
    })


@login_required
def script_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new script."""
    if request.method == "POST":
        form = ScriptForm(request.POST)
        if form.is_valid():
            script = form.save(commit=False)
            script.created_by = request.user
            script.save()
            messages.success(request, f'Script "{script.name}" created successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm()

    return render(request, "cpanel/scripts/create.html", {"form": form})


@login_required
def script_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View script details and recent runs."""
    script = get_object_or_404(
        Script.objects.select_related("environment", "created_by"),
        pk=pk
    )
    recent_runs = script.runs.select_related("triggered_by").order_by("-created_at")[:10]

    return render(request, "cpanel/scripts/detail.html", {
        "script": script,
        "recent_runs": recent_runs,
    })


@login_required
def script_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing script."""
    script = get_object_or_404(Script, pk=pk)

    if request.method == "POST":
        form = ScriptForm(request.POST, instance=script)
        if form.is_valid():
            form.save()
            messages.success(request, f'Script "{script.name}" updated successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm(instance=script)

    return render(request, "cpanel/scripts/edit.html", {
        "form": form,
        "script": script,
    })


@login_required
@require_POST
def script_run_view(request: HttpRequest, pk) -> HttpResponse:
    """Trigger a manual script run."""
    script = get_object_or_404(Script, pk=pk)

    if not script.is_enabled:
        messages.error(request, "Cannot run a disabled script.")
        return redirect("cpanel:script_detail", pk=pk)

    # Create a new Run record (pending state)
    run = Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=request.user,
        code_snapshot=script.code,
    )

    # TODO: Queue the actual execution with django-q2 (Step 5)
    messages.success(request, f'Script "{script.name}" queued for execution.')
    return redirect("cpanel:run_detail", pk=run.pk)


@login_required
@require_POST
def script_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle script enabled/disabled state."""
    script = get_object_or_404(Script, pk=pk)
    script.is_enabled = not script.is_enabled
    script.save(update_fields=["is_enabled", "updated_at"])

    status = "enabled" if script.is_enabled else "disabled"
    messages.success(request, f'Script "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)
