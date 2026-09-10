"""
Authentication views. Password-based login plus email-based password reset;
invite onboarding uses a set-password flow (no passwordless login surface).
"""
import logging

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.http import HttpRequest, HttpResponse

from core.models import User, UserInvite, PasswordResetToken
from core.models.settings import GlobalSettings
from core.email import send_password_reset_email
from core.forms import PasswordLoginForm, SetPasswordForm
from core.services import RecaptchaService, EncryptionService, EncryptionError

logger = logging.getLogger(__name__)


@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    Display the login form and handle password authentication.
    GET: Show the password login form.
    POST: Authenticate the submitted credentials.
    """
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")

    settings = GlobalSettings.get_settings()
    email_enabled = settings.email_backend != GlobalSettings.EmailBackend.DISABLED
    recaptcha_active = settings.recaptcha_active()

    password_form = PasswordLoginForm()
    error_message = None

    if request.method == "POST":
        action = request.POST.get("action", "password")

        if recaptcha_active and not _verify_recaptcha(request, settings):
            # Bot protection: reject before processing any login action.
            error_message = "reCAPTCHA verification failed. Please try again."
        elif action == "password":
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
                                error_message = "This account doesn't have a password set. Use \"Forgot your password?\" to set one."
                            else:
                                error_message = "This account doesn't have a password set. Please contact an administrator."
                        else:
                            error_message = "Invalid email or password."
                    except User.DoesNotExist:
                        error_message = "Invalid email or password."

    return render(request, "auth/login.html", {
        "password_form": password_form,
        "email_enabled": email_enabled,
        "error_message": error_message,
        "recaptcha_active": recaptcha_active,
        "recaptcha_site_key": settings.recaptcha_site_key,
    })


def _verify_recaptcha(request: HttpRequest, settings: GlobalSettings) -> bool:
    """Verify the reCAPTCHA token submitted with the login form."""
    token = request.POST.get("g-recaptcha-response", "")
    try:
        secret = EncryptionService.decrypt(settings.recaptcha_secret_key_encrypted)
    except EncryptionError:
        logger.error("Could not decrypt reCAPTCHA secret key for login verification")
        return False
    return RecaptchaService.verify(secret, token, get_client_ip(request))


@require_POST
@csrf_protect
def logout_view(request: HttpRequest) -> HttpResponse:
    """
    Log user out and redirect to login page.
    """
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("auth:login")


@csrf_protect
@require_http_methods(["GET", "POST"])
def accept_invite_view(request: HttpRequest, token: str) -> HttpResponse:
    """
    Handle an invite link: validate the invite, then let the invited user set a
    password. The account is created (with a usable password) only when the form
    is submitted, so no passwordless login surface is ever created.
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

    if request.method == "POST":
        # A transient User carries the invite email into the similarity
        # validator even when the account doesn't exist yet.
        form = SetPasswordForm(
            request.POST,
            user=User.objects.filter(email=invite.email).first()
            or User(email=invite.email),
        )
        if form.is_valid():
            # Create/activate the invited account WITH a password. get_or_create
            # keeps the post-save membership signal in play (invitee -> member).
            user, _ = User.objects.get_or_create(
                email=invite.email,
                defaults={"is_verified": True},
            )
            user.is_verified = True
            user.set_password(form.cleaned_data["password"])
            user.save()

            invite.mark_used(user)

            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, f"Welcome to PyRun, {user.email}!")
            return redirect("cpanel:dashboard")
    else:
        form = SetPasswordForm()

    return render(request, "auth/accept_invite.html", {
        "form": form,
        "email": invite.email,
        "token": token,
    })


# =============================================================================
# Password Management Views
# =============================================================================

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def change_password_view(request: HttpRequest) -> HttpResponse:
    """Allow users to set or change their password."""
    if request.method == "POST":
        form = SetPasswordForm(request.POST, user=request.user)
        if form.is_valid():
            password = form.cleaned_data["password"]
            request.user.set_password(password)
            request.user.save()

            # Keep the session valid after the hash change. login() without a
            # backend argument would crash here — with multiple auth backends
            # configured (axes + ModelBackend), Django requires an explicit
            # backend for session-loaded users.
            update_session_auth_hash(request, request.user)
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
        form = SetPasswordForm(request.POST, user=reset_token.user)
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
