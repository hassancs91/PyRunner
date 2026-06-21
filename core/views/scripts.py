"""
Script views for the control panel.
"""
import re

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import Script, Run, ScriptSchedule, ScheduleHistory, Secret, SecretGrant, Tag
from core.forms import ScriptForm, ScheduleForm
from core.tasks import queue_script_run
from core.services.schedule_service import ScheduleService
from core.views.ownership import owned_block_message, owned_delete_blocked

# Matches os.environ['X'] / os.environ.get('X') / os.getenv('X') (single/double quotes).
_ENV_REF_RE = re.compile(
    r"""os\.(?:environ\s*\[|environ\.get\s*\(|getenv\s*\()\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)


def _reconcile_grants(script, secret_ids, workspace) -> None:
    """Make the script's SecretGrant set exactly match ``secret_ids`` (Selected mode).

    Only secrets in the active workspace are grantable. Adds missing grants,
    removes deselected ones; never touches grants for secrets outside the
    workspace's reach.
    """
    valid = {
        str(pk)
        for pk in Secret.objects.for_workspace(workspace)
        .filter(pk__in=[s for s in secret_ids if s])
        .values_list("pk", flat=True)
    }
    existing = {str(g.secret_id): g for g in SecretGrant.objects.filter(script=script)}
    for sid in valid - existing.keys():
        SecretGrant.objects.create(script=script, secret_id=sid, active=True)
    for sid in existing.keys() - valid:
        existing[sid].delete()


@login_required
@require_POST
def scan_env_refs_view(request: HttpRequest) -> HttpResponse:
    """Scan a posted script body for os.environ references; return the keys it uses
    and which already exist as secrets in the active workspace (for one-click attach)."""
    code = request.POST.get("code", "")
    keys = sorted(set(_ENV_REF_RE.findall(code)))
    existing = {
        s.key: str(s.id)
        for s in Secret.objects.for_workspace(request.workspace).filter(key__in=keys)
    }
    refs = [{"key": k, "secret_id": existing.get(k)} for k in keys]
    return JsonResponse({"refs": refs})


# Starter code templates available via ?template=<key> on the create page.
SCRIPT_TEMPLATES = {
    "ai": {
        "name": "AI Web Research",
        "description": "Uses Claude (web search + fetch) to research a topic.",
        "code": '''"""
AI Web Research example.

Requires:
  - Claude enabled in PyRunner (Services -> Claude AI)
  - 'claude-agent-sdk' installed in this script's Environment (Environments -> Packages)
"""
from pyrunner_ai import ask_claude

TOPIC = "the latest stable Python release and its headline features"

# Web search + web fetch are enabled by default.
answer = ask_claude(
    f"Search the web and give me a short, up-to-date briefing on {TOPIC}. "
    "Include the version number and 3 bullet points."
)

print(answer)

# Want details (tools used, cost, turns)? Use raw=True:
# result = ask_claude("...", raw=True)
# print(result.text, result.tools_used, result.cost_usd)
''',
    },
}


@login_required
def script_list_view(request: HttpRequest) -> HttpResponse:
    """List all scripts with optional filtering."""
    scripts = (
        Script.objects.for_workspace(request.workspace)
        .select_related("environment", "created_by")
        .prefetch_related("tags")
        .order_by("-updated_at")
    )

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter == "enabled":
        scripts = scripts.filter(is_enabled=True, archived_at__isnull=True)
    elif status_filter == "disabled":
        scripts = scripts.filter(is_enabled=False, archived_at__isnull=True)
    elif status_filter == "archived":
        scripts = scripts.filter(archived_at__isnull=False)
    else:
        # Default "All" excludes archived scripts
        scripts = scripts.filter(archived_at__isnull=True)

    # Filter by tag
    tag_filter = request.GET.get("tag")
    selected_tag = None
    if tag_filter:
        try:
            selected_tag = Tag.objects.get(pk=tag_filter)
            scripts = scripts.filter(tags=selected_tag)
        except (Tag.DoesNotExist, ValueError, ValidationError):
            pass

    # Filter by owning plugin (Plugin Platform v2). The owner list is computed
    # from the whole workspace so the dropdown always offers every owner.
    owners = list(
        Script.objects.for_workspace(request.workspace)
        .exclude(owner_plugin__isnull=True)
        .exclude(owner_plugin="")
        .order_by("owner_plugin")
        .values_list("owner_plugin", flat=True)
        .distinct()
    )
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        scripts = scripts.filter(owner_plugin=owner_filter)

    # Get all tags for filter dropdown
    all_tags = Tag.objects.all().order_by("name")

    return render(request, "cpanel/scripts/list.html", {
        "scripts": scripts,
        "status_filter": status_filter,
        "all_tags": all_tags,
        "selected_tag": selected_tag,
        "owners": owners,
        "selected_owner": owner_filter or "",
    })


@login_required
def script_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new script."""
    if request.method == "POST":
        form = ScriptForm(request.POST)
        if form.is_valid():
            script = form.save(commit=False)
            script.created_by = request.user
            # Stamp the active workspace (tenancy Stage 3) so the new script is
            # owned by — and visible in — the workspace it was created in.
            script.workspace = request.workspace
            script.save()
            form.save_m2m()  # Save M2M relationships (tags)
            # Reconcile per-script secret grants when in Selected injection mode.
            if script.injection_mode == Script.InjectionMode.SELECTED:
                _reconcile_grants(script, request.POST.getlist("granted_secret_ids"), request.workspace)
            messages.success(request, f'Script "{script.name}" created successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        # Optionally pre-fill from a starter template (e.g. ?template=ai).
        template = SCRIPT_TEMPLATES.get(request.GET.get("template", ""))
        form = ScriptForm(initial=template) if template else ScriptForm()

    available_tags = Tag.objects.all().order_by("name")
    return render(request, "cpanel/scripts/create.html", {
        "form": form,
        "available_tags": available_tags,
        "selected_tag_ids": [],
        "granted_secrets": [],
    })


@login_required
def script_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View script details and recent runs."""
    script = get_object_or_404(
        Script.objects.select_related("environment", "created_by").prefetch_related("tags"),
        pk=pk,
        workspace=request.workspace,
    )
    recent_runs = script.runs.select_related("triggered_by").order_by("-created_at")[:10]

    # Ensure schedule exists for this script
    schedule, _ = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user, "workspace": script.workspace},
    )

    return render(request, "cpanel/scripts/detail.html", {
        "script": script,
        "recent_runs": recent_runs,
        "schedule": schedule,
    })


@login_required
def script_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing script and its schedule."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    # Get or create schedule for this script
    schedule, created = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user, "workspace": script.workspace},
    )

    if request.method == "POST":
        form = ScriptForm(request.POST, instance=script)
        schedule_form = ScheduleForm(request.POST, instance=schedule)

        if form.is_valid() and schedule_form.is_valid():
            # Capture previous config for history
            previous_config = {
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
            }

            script = form.save(commit=False)
            script.save()
            form.save_m2m()
            if script.injection_mode == Script.InjectionMode.SELECTED:
                _reconcile_grants(script, request.POST.getlist("granted_secret_ids"), request.workspace)
            schedule = schedule_form.save()

            # Capture new config
            new_config = {
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
            }

            # Create history entry if changed
            if previous_config != new_config:
                change_type = (
                    ScheduleHistory.ChangeType.CREATED
                    if created
                    else ScheduleHistory.ChangeType.UPDATED
                )
                ScheduleHistory.objects.create(
                    schedule=schedule,
                    change_type=change_type,
                    previous_config=previous_config if not created else None,
                    new_config=new_config,
                    changed_by=request.user,
                )

            # Sync with django-q2
            ScheduleService.sync_schedule(schedule)

            messages.success(request, f'Script "{script.name}" updated successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm(instance=script)
        schedule_form = ScheduleForm(instance=schedule)

    available_tags = Tag.objects.all().order_by("name")
    selected_tag_ids = list(script.tags.values_list("pk", flat=True))
    granted_secrets = [
        {"id": str(g.secret_id), "key": g.secret.key, "owner_plugin": g.secret.owner_plugin or ""}
        for g in script.secret_grants.select_related("secret").filter(active=True)
    ]
    return render(request, "cpanel/scripts/edit.html", {
        "form": form,
        "schedule_form": schedule_form,
        "script": script,
        "available_tags": available_tags,
        "selected_tag_ids": selected_tag_ids,
        "granted_secrets": granted_secrets,
    })


@login_required
@require_POST
def script_run_view(request: HttpRequest, pk) -> HttpResponse:
    """Trigger a manual script run."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.can_run:
        if script.is_archived:
            messages.error(request, "Cannot run an archived script.")
        else:
            messages.error(request, "Cannot run a disabled script.")
        return redirect("cpanel:script_detail", pk=pk)

    # Create a new Run record (pending state). Stamp the run's workspace from its
    # script (tenancy Stage 1) so the executor scopes secrets to it.
    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
        status=Run.Status.PENDING,
        triggered_by=request.user,
        code_snapshot=script.code,
    )

    # Queue for async execution via django-q2
    try:
        queue_script_run(run)
        messages.info(request, f'Script "{script.name}" has been queued for execution.')
    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.save()
        messages.error(request, f"Failed to queue script: {str(e)}")

    return redirect("cpanel:run_detail", pk=run.pk)


@login_required
@require_POST
def script_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle script enabled/disabled state."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)
    script.is_enabled = not script.is_enabled
    script.save(update_fields=["is_enabled", "updated_at"])

    status = "enabled" if script.is_enabled else "disabled"
    messages.success(request, f'Script "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def schedule_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle schedule active/inactive state."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    try:
        schedule = script.schedule
    except ScriptSchedule.DoesNotExist:
        messages.error(request, "No schedule configured for this script.")
        return redirect("cpanel:script_detail", pk=pk)

    previous_active = schedule.is_active
    schedule.is_active = not schedule.is_active
    schedule.save(update_fields=["is_active", "updated_at"])

    # Record history
    ScheduleHistory.objects.create(
        schedule=schedule,
        change_type=(
            ScheduleHistory.ChangeType.ENABLED
            if schedule.is_active
            else ScheduleHistory.ChangeType.DISABLED
        ),
        previous_config={"is_active": previous_active},
        new_config={"is_active": schedule.is_active},
        changed_by=request.user,
    )

    # Sync with django-q2
    ScheduleService.sync_schedule(schedule)

    status = "enabled" if schedule.is_active else "paused"
    messages.success(request, f'Schedule for "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
def schedule_history_view(request: HttpRequest, pk) -> HttpResponse:
    """View schedule change history."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    try:
        schedule = script.schedule
        history = schedule.history.select_related("changed_by").order_by("-created_at")
    except ScriptSchedule.DoesNotExist:
        history = []
        schedule = None

    return render(request, "cpanel/scripts/schedule_history.html", {
        "script": script,
        "schedule": schedule,
        "history": history,
    })


@login_required
@require_POST
def webhook_enable_view(request: HttpRequest, pk) -> HttpResponse:
    """Enable webhook for a script (creates token if not exists)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.webhook_token:
        script.create_webhook_token()
        messages.success(request, f'Webhook enabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already enabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_disable_view(request: HttpRequest, pk) -> HttpResponse:
    """Disable webhook for a script (removes token)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if script.webhook_token:
        script.clear_webhook_token()
        messages.success(request, f'Webhook disabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already disabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_regenerate_view(request: HttpRequest, pk) -> HttpResponse:
    """Regenerate webhook token (invalidates old URL)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    script.regenerate_webhook_token()
    messages.success(request, f'Webhook URL regenerated for "{script.name}". The old URL is now invalid.')

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def script_archive_view(request: HttpRequest, pk) -> HttpResponse:
    """Archive a script (soft delete)."""
    from django.utils import timezone

    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if script.is_archived:
        messages.info(request, f'Script "{script.name}" is already archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Archive the script
    script.archived_at = timezone.now()
    script.archived_by = request.user
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    # Pause the schedule if it exists and is active
    try:
        schedule = script.schedule
        if schedule.is_active:
            schedule.is_active = False
            schedule.save(update_fields=["is_active", "updated_at"])
            ScheduleService.sync_schedule(schedule)
    except ScriptSchedule.DoesNotExist:
        pass

    messages.success(request, f'Script "{script.name}" has been archived.')
    return redirect("cpanel:script_list")


@login_required
@require_POST
def script_restore_view(request: HttpRequest, pk) -> HttpResponse:
    """Restore an archived script."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.is_archived:
        messages.info(request, f'Script "{script.name}" is not archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Restore the script
    script.archived_at = None
    script.archived_by = None
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    messages.success(request, f'Script "{script.name}" has been restored.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def script_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Permanently delete an archived script."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.is_archived:
        messages.error(request, "Only archived scripts can be permanently deleted.")
        return redirect("cpanel:script_detail", pk=pk)

    if owned_delete_blocked(request, script):
        messages.error(request, owned_block_message(script, "script"))
        return redirect("cpanel:script_detail", pk=pk)

    name = script.name
    script.delete()  # CASCADE will handle runs and schedule

    messages.success(request, f'Script "{name}" has been permanently deleted.')
    return redirect("cpanel:script_list")
