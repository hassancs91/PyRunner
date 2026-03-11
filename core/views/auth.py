"""
Authentication views supporting both password and magic link login.
"""
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone

from core.models import MagicToken, User, UserInvite, PasswordResetToken
from core.models.settings import GlobalSettings
from core.email import send_magic_link_email, send_password_reset_email
from core.forms import PasswordLoginForm, SetPasswordForm


@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    Display login form and handle both password and magic link authentication.
    GET: Show login form with password (primary) and magic link (secondary) options
    POST: Handle password auth or magic link request based on form submission
    """
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")

    settings = GlobalSettings.get_settings()
    email_enabled = settings.email_backend != GlobalSettings.EmailBackend.DISABLED

    password_form = PasswordLoginForm()
    error_message = None

    if request.method == "POST":
        action = request.POST.get("action", "password")

        if action == "password":
            # Password authentication
            password_form = PasswordLoginForm(request.POST)
            if password_form.is_valid():
                email = password_form.cleaned_data["email"].lower()
                password = password_form.cleaned_data["password"]

                user = authenticate(request, username=email, password=password)
                if user is not None:
                    login(request, user)
                    messages.success(request, f"Welcome back, {user.email}!")
                    return redirect("cpanel:dashboard")
                else:
                    # Check if user exists to give appropriate error
                    try:
                        user_obj = User.objects.get(email=email)
                        if not user_obj.has_usable_password():
                            if email_enabled:
                                error_message = "This account doesn't have a password set. Use the magic link option below."
                            else:
                                error_message = "This account doesn't have a password set. Please contact an administrator."
                        else:
                            error_message = "Invalid email or password."
                    except User.DoesNotExist:
                        error_message = "Invalid email or password."

        elif action == "magic_link":
            # Magic link flow
            return _handle_magic_link_request(request)

    return render(request, "auth/login.html", {
        "password_form": password_form,
        "email_enabled": email_enabled,
        "error_message": error_message,
    })


def _handle_magic_link_request(request: HttpRequest) -> HttpResponse:
    """Handle magic link login request."""
    email = request.POST.get("magic_email", "").strip().lower()

    if not email:
        messages.error(request, "Please enter your email address.")
        return redirect("auth:login")

    if "@" not in email or "." not in email:
        messages.error(request, "Please enter a valid email address.")
        return redirect("auth:login")

    # Check if user exists or if this is the first user
    user_exists = User.objects.filter(email=email).exists()
    is_first_user = User.objects.count() == 0

    # If user doesn't exist and it's not the first user, check registration settings
    if not user_exists and not is_first_user:
        settings = GlobalSettings.get_settings()
        if not settings.allow_registration:
            # Check for valid invite
            has_valid_invite = UserInvite.objects.filter(
                email=email,
                used_at__isnull=True,
                expires_at__gt=timezone.now()
            ).exists()

            if not has_valid_invite:
                messages.error(
                    request,
                    "Registration is invite-only. Please contact an administrator."
                )
                return redirect("auth:login")

    ip_address = get_client_ip(request)
    token = MagicToken.create_for_email(email, ip_address)

    # Check if we should show magic link directly (email disabled)
    settings = GlobalSettings.get_settings()
    show_magic_link = settings.email_backend == GlobalSettings.EmailBackend.DISABLED

    if not show_magic_link:
        send_magic_link_email(request, token)

    # Store in session for magic_link_sent_view
    request.session["magic_token_id"] = str(token.id)
    request.session["show_magic_link"] = show_magic_link

    return redirect("auth:magic_link_sent")


def magic_link_sent_view(request: HttpRequest) -> HttpResponse:
    """
    Confirmation page shown after magic link is sent.
    Shows the magic link directly if email is disabled.
    """
    context = {}

    show_magic_link = request.session.pop("show_magic_link", False)
    magic_token_id = request.session.pop("magic_token_id", None)

    if show_magic_link and magic_token_id:
        try:
            token = MagicToken.objects.get(id=magic_token_id)
            if token.is_valid():
                verify_url = reverse("auth:verify", kwargs={"token": token.token})
                context["magic_link_url"] = request.build_absolute_uri(verify_url)
                context["show_magic_link"] = True
        except MagicToken.DoesNotExist:
            pass

    return render(request, "auth/magic_link_sent.html", context)


@require_http_methods(["GET"])
def verify_view(request: HttpRequest, token: str) -> HttpResponse:
    """
    Verify magic link token and log user in.
    """
    try:
        magic_token = MagicToken.objects.get(token=token)
    except MagicToken.DoesNotExist:
        return render(request, "auth/verify.html", {
            "error": "Invalid link",
            "message": "This magic link is invalid. Please request a new one."
        })

    if not magic_token.is_valid():
        if magic_token.used_at:
            error_message = "This magic link has already been used."
        else:
            error_message = "This magic link has expired. Please request a new one."

        return render(request, "auth/verify.html", {
            "error": "Link expired",
            "message": error_message
        })

    try:
        user = magic_token.consume()
    except ValueError as e:
        return render(request, "auth/verify.html", {
            "error": "Verification failed",
            "message": str(e)
        })

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    messages.success(request, f"Welcome back, {user.email}!")

    return redirect("cpanel:dashboard")


@require_POST
@csrf_protect
def logout_view(request: HttpRequest) -> HttpResponse:
    """
    Log user out and redirect to login page.
    """
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("auth:login")


@require_http_methods(["GET"])
def accept_invite_view(request: HttpRequest, token: str) -> HttpResponse:
    """
    Handle invite link - validates invite and creates magic token for the invited email.
    """
    try:
        invite = UserInvite.objects.get(token=token)
    except UserInvite.DoesNotExist:
        return render(request, "auth/invite_invalid.html", {
            "error": "Invalid invite link",
            "message": "This invite link is invalid or has already been used."
        })

    if not invite.is_valid():
        if invite.used_at:
            error_message = "This invite has already been used."
        else:
            error_message = "This invite has expired. Please request a new one from an administrator."

        return render(request, "auth/invite_invalid.html", {
            "error": "Invite expired",
            "message": error_message
        })

    # Create magic token for this email
    ip_address = get_client_ip(request)
    magic_token = MagicToken.create_for_email(invite.email, ip_address)

    # Mark invite as used
    invite.mark_used(magic_token.user)

    # Check if we should show magic link directly (email disabled)
    settings = GlobalSettings.get_settings()
    show_magic_link = settings.email_backend == GlobalSettings.EmailBackend.DISABLED

    if not show_magic_link:
        send_magic_link_email(request, magic_token)

    # Store in session for magic_link_sent_view
    request.session["magic_token_id"] = str(magic_token.id)
    request.session["show_magic_link"] = show_magic_link

    return redirect("auth:magic_link_sent")


# =============================================================================
# Password Management Views
# =============================================================================

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def change_password_view(request: HttpRequest) -> HttpResponse:
    """Allow users to set or change their password."""
    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data["password"]
            request.user.set_password(password)
            request.user.save()

            # Re-authenticate to update session
            login(request, request.user)
            messages.success(request, "Password updated successfully.")
            return redirect("cpanel:settings")
    else:
        form = SetPasswordForm()

    return render(request, "auth/change_password.html", {
        "form": form,
        "has_password": request.user.has_usable_password(),
    })


@csrf_protect
@require_http_methods(["GET", "POST"])
def forgot_password_view(request: HttpRequest) -> HttpResponse:
    """Request a password reset email."""
    settings = GlobalSettings.get_settings()
    email_enabled = settings.email_backend != GlobalSettings.EmailBackend.DISABLED

    if not email_enabled:
        messages.error(request, "Password reset is not available. Please contact an administrator.")
        return redirect("auth:login")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, "auth/forgot_password.html")

        # Always show success message to prevent email enumeration
        messages.success(
            request,
            "If an account exists with that email, a password reset link has been sent."
        )

        # Only send email if user exists and has verified their account
        try:
            user = User.objects.get(email=email)
            if user.is_verified:
                token = PasswordResetToken.create_for_user(user)
                send_password_reset_email(request, user, token)
        except User.DoesNotExist:
            pass

        return redirect("auth:login")

    return render(request, "auth/forgot_password.html")


@csrf_protect
@require_http_methods(["GET", "POST"])
def reset_password_view(request: HttpRequest, token: str) -> HttpResponse:
    """Reset password using a token from email."""
    try:
        reset_token = PasswordResetToken.objects.get(token=token)
    except PasswordResetToken.DoesNotExist:
        return render(request, "auth/reset_password.html", {
            "error": "Invalid link",
            "message": "This password reset link is invalid. Please request a new one."
        })

    if not reset_token.is_valid():
        if reset_token.used_at:
            error_message = "This password reset link has already been used."
        else:
            error_message = "This password reset link has expired. Please request a new one."

        return render(request, "auth/reset_password.html", {
            "error": "Link expired",
            "message": error_message
        })

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            user = reset_token.consume()
            user.set_password(form.cleaned_data["password"])
            user.save()

            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Password has been reset successfully.")
            return redirect("cpanel:dashboard")
    else:
        form = SetPasswordForm()

    return render(request, "auth/reset_password.html", {
        "form": form,
        "token": token,
    })


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP from request headers."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
