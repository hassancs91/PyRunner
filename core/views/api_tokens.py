"""
API Token management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import DataStoreAPITokenForm
from core.models import DataStore, DataStoreAPIToken


def _token_scope(request):
    """Q scoping tokens to the active workspace (tenancy Stage 3).

    Includes NULL-workspace tokens (legacy/un-stamped) so a single-workspace
    instance is byte-for-byte; once a token is created post-Stage-3 it carries a
    workspace and is no longer visible cross-workspace.
    """
    return Q(workspace=request.workspace) | Q(workspace__isnull=True)


@login_required
def api_token_list_view(request: HttpRequest) -> HttpResponse:
    """List the active workspace's API tokens."""
    tokens = (
        DataStoreAPIToken.objects.filter(_token_scope(request))
        .select_related("datastore", "created_by")
    )

    return render(
        request,
        "cpanel/api_tokens/list.html",
        {
            "tokens": tokens,
        },
    )


@login_required
def api_token_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new API token."""
    if request.method == "POST":
        form = DataStoreAPITokenForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            token = form.save(commit=False)
            # Generate the token value
            token.token = DataStoreAPIToken.generate_token()
            token.created_by = request.user
            # Stamp the active workspace (tenancy Stage 3) so the token only
            # resolves this workspace's datastores.
            token.workspace = request.workspace
            token.save()

            # Store the token in session for one-time display
            request.session["new_api_token"] = token.token
            request.session["new_api_token_id"] = str(token.id)

            messages.success(request, f'API token "{token.name}" created successfully.')
            return redirect("cpanel:api_token_created", pk=token.pk)
    else:
        form = DataStoreAPITokenForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/api_tokens/create.html",
        {
            "form": form,
        },
    )


@login_required
def api_token_created_view(request: HttpRequest, pk) -> HttpResponse:
    """Display newly created token (one-time view)."""
    token_obj = get_object_or_404(DataStoreAPIToken, _token_scope(request), pk=pk)

    # Get the token value from session (one-time display)
    new_token = request.session.pop("new_api_token", None)
    new_token_id = request.session.pop("new_api_token_id", None)

    # Verify the token in session matches the requested token
    if new_token_id != str(pk):
        # Token was already shown or doesn't match, redirect to list
        messages.warning(request, "Token can only be viewed once after creation.")
        return redirect("cpanel:api_token_list")

    return render(
        request,
        "cpanel/api_tokens/created.html",
        {
            "token_obj": token_obj,
            "token_value": new_token,
        },
    )


@login_required
@require_POST
def api_token_revoke_view(request: HttpRequest, pk) -> HttpResponse:
    """Revoke (delete) an API token."""
    token = get_object_or_404(DataStoreAPIToken, _token_scope(request), pk=pk)
    name = token.name
    token.delete()

    messages.success(request, f'API token "{name}" has been revoked.')
    return redirect("cpanel:api_token_list")


@login_required
@require_POST
def api_token_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle an API token's active status."""
    token = get_object_or_404(DataStoreAPIToken, _token_scope(request), pk=pk)
    token.is_active = not token.is_active
    token.save(update_fields=["is_active"])

    status = "activated" if token.is_active else "deactivated"
    messages.success(request, f'API token "{token.name}" has been {status}.')
    return redirect("cpanel:api_token_list")
