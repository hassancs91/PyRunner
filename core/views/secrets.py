"""
Secret management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import SecretCreateForm, SecretEditForm
from core.models import Secret
from core.services import EncryptionService
from core.views.ownership import owned_block_message, owned_delete_blocked


@login_required
def secret_picker_view(request: HttpRequest) -> HttpResponse:
    """Autocomplete for the script secret-attach UI: workspace secrets matching ``q``,
    each tagged with its owner (System for unowned) so results group cleanly."""
    q = (request.GET.get("q") or "").strip()
    qs = Secret.objects.for_workspace(request.workspace)
    if q:
        qs = qs.filter(key__icontains=q)
    qs = qs.order_by("owner_plugin", "key")[:50]
    secrets = [
        {
            "id": str(s.id),
            "key": s.key,
            "owner_plugin": s.owner_plugin or "",
            "description": s.description,
        }
        for s in qs
    ]
    return JsonResponse({"secrets": secrets})


@login_required
def secret_list_view(request: HttpRequest) -> HttpResponse:
    """List all secrets with masked values."""
    secrets = Secret.objects.for_workspace(request.workspace).order_by("key")

    # Owner filter (Plugin Platform v2): owners computed before filtering so the
    # dropdown always lists every owner present in the workspace.
    owners = list(
        Secret.objects.for_workspace(request.workspace)
        .exclude(owner_plugin__isnull=True)
        .exclude(owner_plugin="")
        .order_by("owner_plugin")
        .values_list("owner_plugin", flat=True)
        .distinct()
    )
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        secrets = secrets.filter(owner_plugin=owner_filter)

    # Check if encryption is configured
    encryption_configured = EncryptionService.is_configured()

    return render(
        request,
        "cpanel/secrets/list.html",
        {
            "secrets": secrets,
            "encryption_configured": encryption_configured,
            "owners": owners,
            "selected_owner": owner_filter or "",
        },
    )


@login_required
def secret_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new secret."""
    # Check encryption configuration first
    if not EncryptionService.is_configured():
        messages.error(
            request,
            "Encryption is not configured. Set ENCRYPTION_KEY in your environment.",
        )
        return redirect("cpanel:secret_list")

    if request.method == "POST":
        form = SecretCreateForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            key = form.cleaned_data["key"]
            value = form.cleaned_data["value"]
            description = form.cleaned_data.get("description", "")

            # Create the secret with encrypted value, stamped with the active
            # workspace (tenancy Stage 3) so its key is unique per workspace.
            secret = Secret(
                key=key,
                description=description,
                created_by=request.user,
                workspace=request.workspace,
            )
            secret.set_value(value)
            secret.save()

            messages.success(request, f'Secret "{key}" created successfully.')
            return redirect("cpanel:secret_list")
    else:
        form = SecretCreateForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/secrets/create.html",
        {
            "form": form,
        },
    )


@login_required
def secret_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing secret."""
    secret = get_object_or_404(Secret, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = SecretEditForm(request.POST)
        if form.is_valid():
            # Update value if provided
            new_value = form.cleaned_data.get("value")
            if new_value:
                secret.set_value(new_value)

            # Always update description
            secret.description = form.cleaned_data.get("description", "")
            secret.save()

            messages.success(request, f'Secret "{secret.key}" updated successfully.')
            return redirect("cpanel:secret_list")
    else:
        form = SecretEditForm(
            initial={
                "description": secret.description,
            }
        )

    return render(
        request,
        "cpanel/secrets/edit.html",
        {
            "form": form,
            "secret": secret,
        },
    )


@login_required
@require_POST
def secret_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a secret."""
    secret = get_object_or_404(Secret, pk=pk, workspace=request.workspace)

    # Plugin Platform v2: refuse to delete a plugin-owned secret out from under
    # its plugin (superuser force=1 is the escape hatch; cascade drops grants).
    if owned_delete_blocked(request, secret):
        messages.error(request, owned_block_message(secret, "secret"))
        return redirect("cpanel:secret_list")

    key = secret.key
    secret.delete()

    messages.success(request, f'Secret "{key}" deleted successfully.')
    return redirect("cpanel:secret_list")
