"""
Services management views for the control panel.
"""

import json
import logging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import GlobalSettings
from core.forms import S3SettingsForm
from core.services.s3_service import S3Service
from core.services.encryption_service import EncryptionService

logger = logging.getLogger(__name__)


def superuser_required(view_func):
    """Decorator to require superuser status for S3 configuration."""
    return user_passes_test(lambda u: u.is_superuser, login_url="auth:login")(view_func)


@login_required
@superuser_required
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
@superuser_required
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
@superuser_required
@require_POST
def s3_test_connection_view(request: HttpRequest) -> JsonResponse:
    """Test S3 connection and return result.

    Accepts form data in POST body to test credentials before saving.
    Falls back to saved settings if no form data provided.
    """
    try:
        # Try to parse form data from request body
        data = {}
        if request.body:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse(
                    {"success": False, "error": "Invalid JSON in request body"},
                    status=400,
                )

        if data:
            # Test with provided form data
            settings = GlobalSettings.get_settings()

            # Get credentials from form or fall back to saved encrypted values
            access_key = data.get("s3_access_key", "")
            if not access_key and settings.s3_access_key_encrypted:
                access_key = EncryptionService.decrypt(settings.s3_access_key_encrypted)

            secret_key = data.get("s3_secret_key", "")
            if not secret_key and settings.s3_secret_key_encrypted:
                secret_key = EncryptionService.decrypt(settings.s3_secret_key_encrypted)

            success, message = S3Service.test_connection_with_credentials(
                bucket_name=data.get("s3_bucket_name", ""),
                access_key=access_key,
                secret_key=secret_key,
                endpoint_url=data.get("s3_endpoint_url", ""),
                region=data.get("s3_region", "us-east-1"),
                use_ssl=data.get("s3_use_ssl", True),
                path_style=data.get("s3_path_style", False),
            )
        else:
            # Fall back to testing saved settings
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
