"""
Services management views for the control panel.
"""

import logging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import GlobalSettings
from core.forms import S3SettingsForm
from core.services.s3_service import S3Service

logger = logging.getLogger(__name__)


@login_required
def services_view(request: HttpRequest) -> HttpResponse:
    """Display services configuration page."""
    settings = GlobalSettings.get_settings()
    s3_form = S3SettingsForm(instance=settings)
    s3_status = S3Service.get_status()

    return render(
        request,
        "cpanel/services/list.html",
        {
            "settings": settings,
            "s3_form": s3_form,
            "s3_status": s3_status,
        },
    )


@login_required
@require_POST
def s3_settings_view(request: HttpRequest) -> HttpResponse:
    """Update S3 storage settings."""
    settings = GlobalSettings.get_settings()
    form = S3SettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "S3 storage settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:services")


@login_required
@require_POST
def s3_test_connection_view(request: HttpRequest) -> JsonResponse:
    """Test S3 connection and return result."""
    try:
        success, message = S3Service.test_connection()
        return JsonResponse(
            {
                "success": success,
                "message": message if success else None,
                "error": message if not success else None,
            }
        )
    except Exception as e:
        logger.exception("S3 connection test failed")
        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            }
        )
