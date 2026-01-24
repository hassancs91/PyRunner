"""
Setup wizard views for first-run configuration.
"""

from django.shortcuts import render, redirect
from django.contrib.auth import login
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect
from django.http import HttpRequest, HttpResponse

from core.services.setup_service import SetupService
from core.forms import AdminSetupForm


@require_http_methods(["GET", "POST"])
def setup_view(request: HttpRequest) -> HttpResponse:
    """
    Initial setup wizard view.

    GET: Display setup status and run setup automatically
    POST: Trigger setup if not already complete
    """
    # Check if setup is already complete
    if not SetupService.is_setup_needed():
        # If setup is done but no admin exists, redirect to admin setup
        if SetupService.needs_admin_setup():
            return redirect("setup:admin_setup")
        return redirect("auth:login")

    # Get current status
    status = SetupService.get_status()

    # On POST or if setup is needed, run the setup
    if request.method == "POST" or status.get("migrations_pending") or not status.get("default_env_exists"):
        results = SetupService.run_full_setup()

        # Refresh status after setup
        status = SetupService.get_status()

        # If setup completed and admin needed, redirect to admin setup
        if results.get("completed") and SetupService.needs_admin_setup():
            return render(request, "setup/setup.html", {
                "status": status,
                "results": results,
                "setup_complete": True,
                "redirect_to_admin_setup": True,
            })

        return render(request, "setup/setup.html", {
            "status": status,
            "results": results,
            "setup_complete": results.get("completed", False),
        })

    return render(request, "setup/setup.html", {
        "status": status,
        "results": None,
        "setup_complete": False,
    })


@csrf_protect
@require_http_methods(["GET", "POST"])
def admin_setup_view(request: HttpRequest) -> HttpResponse:
    """
    Admin account creation during setup.

    This view is shown after the initial system setup completes
    to create the first admin user with password authentication.
    """
    # Redirect if admin already exists
    if not SetupService.needs_admin_setup():
        return redirect("auth:login")

    if request.method == "POST":
        form = AdminSetupForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].lower()
            password = form.cleaned_data["password"]

            success, message = SetupService.create_admin_user(email, password)

            if success:
                # Log the user in immediately
                from core.models import User
                user = User.objects.get(email=email)
                login(request, user)
                messages.success(request, "Admin account created! Welcome to PyRunner.")
                return redirect("cpanel:dashboard")
            else:
                messages.error(request, message)
    else:
        form = AdminSetupForm()

    return render(request, "setup/admin_setup.html", {"form": form})
