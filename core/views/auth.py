"""
Authentication views for magic link login.
"""
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.http import HttpRequest, HttpResponse

from core.models import MagicToken
from core.email import send_magic_link_email


@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    Display login form and handle magic link requests.
    GET: Show login form
    POST: Create token and send magic link email
    """
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, "auth/login.html")

        if "@" not in email or "." not in email:
            messages.error(request, "Please enter a valid email address.")
            return render(request, "auth/login.html", {"email": email})

        ip_address = get_client_ip(request)
        token = MagicToken.create_for_email(email, ip_address)
        send_magic_link_email(request, token)

        return redirect("auth:magic_link_sent")

    return render(request, "auth/login.html")


def magic_link_sent_view(request: HttpRequest) -> HttpResponse:
    """
    Confirmation page shown after magic link is sent.
    """
    return render(request, "auth/magic_link_sent.html")


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

    login(request, user)
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


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP from request headers."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
