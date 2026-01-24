"""
User management views for admin users.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.urls import reverse
from django.http import HttpRequest, HttpResponse

from core.models import User, UserInvite
from core.models.settings import GlobalSettings


def is_admin(user):
    """Check if user is an admin (superuser)."""
    return user.is_superuser


@login_required
@user_passes_test(is_admin)
def user_list_view(request: HttpRequest) -> HttpResponse:
    """List all users and pending invites."""
    users = User.objects.all().order_by("-date_joined")
    pending_invites = UserInvite.objects.filter(used_at__isnull=True).order_by("-created_at")
    settings = GlobalSettings.get_settings()

    # Check for recently created invite URL to display
    last_invite_url = request.session.pop("last_invite_url", None)
    last_invite_email = request.session.pop("last_invite_email", None)

    return render(request, "cpanel/users.html", {
        "users": users,
        "pending_invites": pending_invites,
        "allow_registration": settings.allow_registration,
        "last_invite_url": last_invite_url,
        "last_invite_email": last_invite_email,
    })


@login_required
@user_passes_test(is_admin)
@require_http_methods(["GET", "POST"])
def invite_user_view(request: HttpRequest) -> HttpResponse:
    """Create a new user invite."""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if not email or "@" not in email or "." not in email:
            messages.error(request, "Please enter a valid email address.")
            return redirect("cpanel:user_list")

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            messages.error(request, f"User {email} already exists.")
            return redirect("cpanel:user_list")

        # Check if there's already a pending invite
        existing_invite = UserInvite.objects.filter(
            email=email, used_at__isnull=True
        ).first()
        if existing_invite:
            messages.warning(
                request,
                f"An invite for {email} already exists. Creating a new one will invalidate the old link."
            )

        invite = UserInvite.create_invite(email, created_by=request.user)

        # Generate invite URL
        invite_url = request.build_absolute_uri(
            reverse("auth:accept_invite", kwargs={"token": invite.token})
        )

        # Store invite URL for display (since email may not be configured)
        request.session["last_invite_url"] = invite_url
        request.session["last_invite_email"] = email

        messages.success(request, f"Invitation created for {email}.")
        return redirect("cpanel:user_list")

    return redirect("cpanel:user_list")


@login_required
@user_passes_test(is_admin)
@require_POST
def revoke_invite_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Revoke/delete an unused invite."""
    invite = get_object_or_404(UserInvite, pk=pk, used_at__isnull=True)
    email = invite.email
    invite.delete()
    messages.success(request, f"Invite for {email} revoked.")
    return redirect("cpanel:user_list")


@login_required
@user_passes_test(is_admin)
@require_POST
def toggle_registration_view(request: HttpRequest) -> HttpResponse:
    """Toggle open registration on/off."""
    settings = GlobalSettings.get_settings()
    settings.allow_registration = not settings.allow_registration
    settings.save(update_fields=["allow_registration"])

    status = "enabled" if settings.allow_registration else "disabled"
    messages.success(request, f"Open registration {status}.")
    return redirect("cpanel:user_list")


@login_required
@user_passes_test(is_admin)
@require_POST
def delete_user_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a user (cannot delete self)."""
    user = get_object_or_404(User, pk=pk)

    if user.pk == request.user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("cpanel:user_list")

    email = user.email
    user.delete()
    messages.success(request, f"User {email} deleted.")
    return redirect("cpanel:user_list")
