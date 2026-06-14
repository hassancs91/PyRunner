"""
Services management views for the control panel.
"""

import json
import logging
from datetime import timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import GlobalSettings, ClaudeUsage
from core.forms import S3SettingsForm, ClaudeSettingsForm
from core.services.s3_service import S3Service
from core.services.claude_service import ClaudeService
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
    claude_form = ClaudeSettingsForm(instance=settings)
    claude_status = ClaudeService.get_status()

    return render(
        request,
        "cpanel/services/list.html",
        {
            "settings": settings,
            "s3_form": s3_form,
            "s3_status": s3_status,
            "claude_form": claude_form,
            "claude_status": claude_status,
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


@login_required
@superuser_required
@require_POST
def claude_settings_view(request: HttpRequest) -> HttpResponse:
    """Update Claude AI integration settings."""
    settings = GlobalSettings.get_settings()
    form = ClaudeSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Claude AI settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def claude_test_connection_view(request: HttpRequest) -> JsonResponse:
    """Test the Claude connection (runs a canned web-search query).

    Accepts credentials in the POST body to test before saving; falls back to
    saved settings when no new credential is provided.
    """
    try:
        data = {}
        if request.body:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse(
                    {"success": False, "error": "Invalid JSON in request body"},
                    status=400,
                )

        settings = GlobalSettings.get_settings()
        auth_method = data.get("claude_auth_method") or settings.claude_auth_method
        model = data.get("claude_default_model", settings.claude_default_model) or ""

        # Pick the credential for the selected method: prefer the just-entered
        # value, otherwise decrypt the saved one.
        if auth_method == GlobalSettings.ClaudeAuthMethod.API_KEY:
            credential = data.get("claude_api_key", "")
            if not credential and settings.claude_api_key_encrypted:
                credential = EncryptionService.decrypt(settings.claude_api_key_encrypted)
        else:
            credential = data.get("claude_oauth_token", "")
            if not credential and settings.claude_oauth_token_encrypted:
                credential = EncryptionService.decrypt(
                    settings.claude_oauth_token_encrypted
                )

        if not credential:
            return JsonResponse(
                {
                    "success": False,
                    "error": "No credential to test. Enter a token/key first.",
                }
            )

        success, message = ClaudeService.test_connection_with_credentials(
            auth_method=auth_method,
            credential=credential,
            model=model,
        )

        # Record a successful test against saved settings.
        if success:
            from django.utils import timezone

            settings.claude_last_tested_at = timezone.now()
            settings.save(update_fields=["claude_last_tested_at"])

        return JsonResponse(
            {
                "success": success,
                "message": message if success else None,
                "error": message if not success else None,
            }
        )
    except Exception as e:
        logger.exception("Claude connection test failed")
        return JsonResponse({"success": False, "error": str(e)})


@login_required
@superuser_required
def claude_usage_view(request: HttpRequest) -> HttpResponse:
    """Claude usage analytics: token totals, daily chart, and per-call rows."""
    period = request.GET.get("period", "30")
    day_map = {"7": 7, "30": 30, "90": 90}

    base = ClaudeUsage.objects.all()
    if period in day_map:
        since = timezone.now() - timedelta(days=day_map[period])
        base = base.filter(created_at__gte=since)
        period_label = f"Last {day_map[period]} days"
    else:
        period = "all"
        period_label = "All time"

    # Summary totals
    agg = base.aggregate(
        requests=Count("id"),
        input=Sum("input_tokens"),
        output=Sum("output_tokens"),
        cache_creation=Sum("cache_creation_tokens"),
        cache_read=Sum("cache_read_tokens"),
    )
    inp = agg["input"] or 0
    out = agg["output"] or 0
    cache_write = agg["cache_creation"] or 0
    cache_read = agg["cache_read"] or 0
    cache = cache_write + cache_read
    summary = {
        "requests": agg["requests"] or 0,
        "input": inp,
        "output": out,
        "cache": cache,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "total": inp + out + cache,
    }

    # Daily series for the chart
    daily = list(
        base.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            input=Sum("input_tokens"),
            output=Sum("output_tokens"),
            requests=Count("id"),
        )
        .order_by("day")
    )
    chart = {
        "labels": [d["day"].strftime("%b %d") if d["day"] else "" for d in daily],
        "input": [d["input"] or 0 for d in daily],
        "output": [d["output"] or 0 for d in daily],
        "requests": [d["requests"] or 0 for d in daily],
    }

    # Per-model breakdown
    by_model = []
    for row in (
        base.values("model")
        .annotate(requests=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        .order_by("-input")
    ):
        by_model.append(
            {
                "model": row["model"] or "(unknown)",
                "requests": row["requests"],
                "input": row["input"] or 0,
                "output": row["output"] or 0,
                "total": (row["input"] or 0) + (row["output"] or 0),
            }
        )

    # Top scripts by tokens
    by_script = []
    for row in (
        base.filter(script_id__isnull=False)
        .values("script_id", "script_name")
        .annotate(requests=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        .order_by("-input")[:10]
    ):
        by_script.append(
            {
                "script_id": row["script_id"],
                "script_name": row["script_name"] or "(unnamed)",
                "requests": row["requests"],
                "total": (row["input"] or 0) + (row["output"] or 0),
            }
        )

    # Recent rows (paginated)
    paginator = Paginator(base.order_by("-created_at"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "cpanel/services/usage.html",
        {
            "period": period,
            "period_label": period_label,
            "period_options": [("7", "7d"), ("30", "30d"), ("90", "90d"), ("all", "All")],
            "summary": summary,
            "chart_json": json.dumps(chart),
            "has_data": summary["requests"] > 0,
            "by_model": by_model,
            "by_script": by_script,
            "page_obj": page_obj,
            "claude_status": ClaudeService.get_status(),
        },
    )
