"""
Changelog / "What's new" view for the control panel.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from core.changelog import CHANGELOG


@login_required
def changelog_view(request: HttpRequest) -> HttpResponse:
    """Render the in-app release notes."""
    return render(request, "cpanel/changelog.html", {"releases": CHANGELOG})
