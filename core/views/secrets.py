"""
Secret management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import SecretCreateForm, SecretEditForm
from core.models import Secret
from core.services import EncryptionService


@login_required
def secret_list_view(request: HttpRequest) -> HttpResponse:
    """List all secrets with masked values."""
    secrets = Secret.objects.all().order_by("key")

    # Check if encryption is configured
    encryption_configured = EncryptionService.is_configured()

    return render(
        request,
        "cpanel/secrets/list.html",
        {
            "secrets": secrets,
            "encryption_configured": encryption_configured,
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
        form = SecretCreateForm(request.POST)
        if form.is_valid():
            key = form.cleaned_data["key"]
            value = form.cleaned_data["value"]
            description = form.cleaned_data.get("description", "")

            # Create the secret with encrypted value
            secret = Secret(
                key=key,
                description=description,
                created_by=request.user,
            )
            secret.set_value(value)
            secret.save()

            messages.success(request, f'Secret "{key}" created successfully.')
            return redirect("cpanel:secret_list")
    else:
        form = SecretCreateForm()

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
    secret = get_object_or_404(Secret, pk=pk)

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
    secret = get_object_or_404(Secret, pk=pk)
    key = secret.key
    secret.delete()

    messages.success(request, f'Secret "{key}" deleted successfully.')
    return redirect("cpanel:secret_list")
