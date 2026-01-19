"""
Environment views for the control panel.
"""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse

from core.models import Environment


@login_required
def environment_list_view(request: HttpRequest) -> HttpResponse:
    """List all environments."""
    environments = Environment.objects.all().order_by("-is_default", "name")

    return render(request, "cpanel/environments/list.html", {
        "environments": environments,
    })


@login_required
def environment_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View environment details and associated scripts."""
    environment = get_object_or_404(Environment, pk=pk)
    scripts = environment.scripts.select_related("created_by").order_by("-updated_at")

    return render(request, "cpanel/environments/detail.html", {
        "environment": environment,
        "scripts": scripts,
    })
