"""
Services management views for the control panel.
"""

import json
import logging
from datetime import timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from django.db.models import ProtectedError

from core.models import (
    AIProvider,
    GlobalSettings,
    ClaudeUsage,
    PROVIDER_PRESETS,
    STORAGE_PRESETS,
    SecretProvider,
    StorageConnection,
)
from core.forms import (
    AISettingsForm,
    AIProviderForm,
    AssetsStorageForm,
    SecretProviderForm,
    StorageConnectionForm,
)
from core.services.s3_service import S3Service
from core.services.claude_service import ClaudeService
from core.services.encryption_service import EncryptionService
from core.services.secret_backends import (
    SecretResolutionError,
    get_backend,
    list_backends,
)
from core.views.decorators import superuser_required

logger = logging.getLogger(__name__)


@login_required
@superuser_required
def services_view(request: HttpRequest) -> HttpResponse:
    """Display services configuration page."""
    settings = GlobalSettings.get_settings()
    claude_form = AISettingsForm(instance=settings)
    claude_status = ClaudeService.get_status()

    # Object storage — many connections; backups use `is_default`, plugins use the
    # `assets_storage` FK. `s3_status` describes the BACKUP connection, keeping the
    # existing backup card and its template contract unchanged.
    storage_form = StorageConnectionForm()
    assets_form = AssetsStorageForm(instance=settings)
    s3_status = S3Service.get_status()
    storage_connections = list(StorageConnection.objects.all())
    # Plain-dict mirror for the template JS (edit prefill, per-type hints), same
    # shape as providers_data below. Credentials are never mirrored — only whether
    # they exist.
    storage_connections_data = [
        {
            "id": str(c.id),
            "name": c.name,
            "provider_type": c.provider_type,
            "endpoint_url": c.endpoint_url,
            "region": c.region,
            "bucket": c.bucket,
            "public_base_url": c.public_base_url,
            "use_ssl": c.use_ssl,
            "path_style": c.path_style,
            "is_default": c.is_default,
            "enabled": c.enabled,
            "has_credentials": bool(c.access_key_encrypted and c.secret_key_encrypted),
        }
        for c in storage_connections
    ]
    storage_presets_data = {str(key): value for key, value in STORAGE_PRESETS.items()}
    assets_connection = settings.assets_storage

    providers = list(AIProvider.objects.all())
    # Plain-dict mirror for the template JS (edit prefill, per-type hints).
    providers_data = [
        {
            "id": str(p.id),
            "name": p.name,
            "provider_type": p.provider_type,
            "base_url": p.base_url,
            "auth_method": p.auth_method,
            "default_model": p.default_model,
            "has_credential": bool(p.credential_encrypted),
        }
        for p in providers
    ]
    presets_data = {str(key): value for key, value in PROVIDER_PRESETS.items()}

    # External Secret Providers (Stage 2). Instance-global profiles, like AI
    # providers. The backend registry drives the type dropdown + dynamic fields;
    # the plain-dict mirrors feed the template JS (edit prefill, field rendering).
    secret_providers = list(SecretProvider.objects.all())
    secret_counts = _secret_counts_by_provider()
    backend_labels = {b.provider_key: (b.label or b.provider_key) for b in list_backends()}
    for p in secret_providers:
        # Attach display helpers for the table (Django templates can't index a
        # dict by a variable key).
        p.secret_count = secret_counts.get(p.id, 0)
        p.type_label = backend_labels.get(p.provider_type, p.provider_type)
    secret_providers_data = [
        {
            "id": str(p.id),
            "name": p.name,
            "provider_type": p.provider_type,
            "cache_ttl": p.cache_ttl,
            "on_error": p.on_error,
            "config": p.config or {},
            "has_credentials": bool(p.credentials_encrypted),
            "secret_count": p.secret_count,
        }
        for p in secret_providers
    ]
    secret_backends_data = [
        {
            "provider_key": b.provider_key,
            "label": b.label or b.provider_key,
            "docs_url": b.docs_url,
            "fields": b.fields,
            "ref_placeholder": b.ref_placeholder,
            "ref_help": b.ref_help,
        }
        for b in list_backends()
    ]

    return render(
        request,
        "cpanel/services/list.html",
        {
            "settings": settings,
            "s3_status": s3_status,
            "storage_form": storage_form,
            "storage_connections": storage_connections,
            "storage_connections_json": storage_connections_data,
            "storage_presets_json": storage_presets_data,
            "assets_form": assets_form,
            "assets_connection": assets_connection,
            "claude_form": claude_form,
            "claude_status": claude_status,
            "ai_providers": providers,
            "ai_provider_form": AIProviderForm(),
            "ai_providers_json": providers_data,
            "ai_presets_json": presets_data,
            "secret_providers": secret_providers,
            "secret_provider_form": SecretProviderForm(),
            "secret_providers_json": secret_providers_data,
            "secret_backends_json": secret_backends_data,
        },
    )


def _secret_counts_by_provider() -> dict:
    """Map provider id → number of secrets referencing it (the in-use-by-N count).

    Counted across ALL workspaces on purpose: profiles are instance-global, and
    the count exists to protect against deleting a profile that any secret —
    anywhere — still resolves through (mirrors the PROTECT FK).
    """
    from django.db.models import Count

    from core.models import Secret

    rows = (
        Secret.objects.filter(source=Secret.Source.EXTERNAL, provider__isnull=False)
        .values("provider_id")
        .annotate(n=Count("id"))
    )
    return {r["provider_id"]: r["n"] for r in rows}


@login_required
@superuser_required
@require_POST
def storage_connection_save_view(request: HttpRequest) -> HttpResponse:
    """Create or update a StorageConnection (hidden connection_id = edit)."""
    instance = None
    connection_id = request.POST.get("connection_id")
    if connection_id:
        # Hidden POST field, not a URL-validated <uuid:…>; a malformed value would
        # raise on the pk lookup, so guard it into the friendly "not found".
        try:
            instance = StorageConnection.objects.filter(pk=connection_id).first()
        except (ValidationError, ValueError):
            instance = None
        if instance is None:
            messages.error(request, "Storage connection not found.")
            return redirect("cpanel:services")

    form = StorageConnectionForm(request.POST, instance=instance)
    if form.is_valid():
        connection = form.save()
        # Convenience: the first connection ever saved becomes the backup default,
        # matching the pre-multi-connection world where the only config WAS backups.
        if (
            not connection.is_default
            and not StorageConnection.objects.filter(is_default=True).exists()
        ):
            connection.is_default = True
            connection.save(update_fields=["is_default"])
        messages.success(request, f"Storage connection '{connection.name}' saved.")
    else:
        for field, errors in form.errors.items():
            label = "" if field == "__all__" else f"{field}: "
            for error in errors:
                messages.error(request, f"{label}{error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def storage_connection_delete_view(request: HttpRequest, connection_id) -> HttpResponse:
    """Delete a storage connection.

    Nothing cascades: `assets_storage` is SET_NULL (the seam degrades to
    unavailable) and backups resolve `is_default` fresh each run. Both cases get a
    warning rather than a silent behavior change.
    """
    connection = StorageConnection.objects.filter(pk=connection_id).first()
    if connection is None:
        messages.error(request, "Storage connection not found.")
        return redirect("cpanel:services")

    settings = GlobalSettings.get_settings()
    was_assets = settings.assets_storage_id == connection.id
    was_default = connection.is_default
    name = connection.name
    connection.delete()

    if was_default:
        messages.warning(
            request,
            f"Connection '{name}' deleted. No connection is set for backups now — "
            "scheduled backups will not run until you mark another one.",
        )
    elif was_assets:
        messages.warning(
            request,
            f"Connection '{name}' deleted. Plugin storage is unavailable now — "
            "select another assets connection to restore it.",
        )
    else:
        messages.success(request, f"Storage connection '{name}' deleted.")
    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def storage_connection_default_view(request: HttpRequest, connection_id) -> HttpResponse:
    """Mark one connection as the backup default (one-click switch)."""
    connection = StorageConnection.objects.filter(pk=connection_id).first()
    if connection is None:
        messages.error(request, "Storage connection not found.")
        return redirect("cpanel:services")

    connection.is_default = True
    connection.save()  # clears the flag on every other row
    messages.success(request, f"Backups now use '{connection.name}'.")
    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def assets_storage_view(request: HttpRequest) -> HttpResponse:
    """Select the connection the plugin storage seam uses."""
    settings = GlobalSettings.get_settings()
    form = AssetsStorageForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        chosen = form.cleaned_data.get("assets_storage")
        if chosen:
            messages.success(request, f"Plugin storage now uses '{chosen.name}'.")
        else:
            messages.success(
                request, "Plugin storage is now unavailable (no connection selected)."
            )
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def s3_test_connection_view(request: HttpRequest) -> JsonResponse:
    """Test a storage connection and return the result.

    Accepts unsaved form data in the POST body so credentials can be validated
    before saving. A blank key falls back to the named connection's stored one, so
    "Test" works on an existing row without re-typing secrets.
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

        # The row being edited, when there is one — the source of any credential
        # the form left blank.
        saved = None
        connection_id = data.get("connection_id")
        if connection_id:
            try:
                saved = StorageConnection.objects.filter(pk=connection_id).first()
            except (ValidationError, ValueError):
                saved = None

        if data:
            access_key = data.get("access_key", "")
            if not access_key and saved and saved.access_key_encrypted:
                access_key = EncryptionService.decrypt(saved.access_key_encrypted)

            secret_key = data.get("secret_key", "")
            if not secret_key and saved and saved.secret_key_encrypted:
                secret_key = EncryptionService.decrypt(saved.secret_key_encrypted)

            success, message = S3Service.test_connection_with_credentials(
                bucket_name=data.get("bucket", ""),
                access_key=access_key,
                secret_key=secret_key,
                endpoint_url=data.get("endpoint_url", ""),
                region=data.get("region", "us-east-1"),
                use_ssl=data.get("use_ssl", True),
                path_style=data.get("path_style", False),
            )
        else:
            # No body: test the saved backup connection.
            success, message = S3Service.test_connection(S3Service.for_backup())

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
    """Update AI integration settings (master toggle + active provider)."""
    settings = GlobalSettings.get_settings()
    form = AISettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "AI settings saved successfully.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def ai_provider_save_view(request: HttpRequest) -> HttpResponse:
    """Create or update an AIProvider profile (hidden provider_id = edit)."""
    instance = None
    provider_id = request.POST.get("provider_id")
    if provider_id:
        # provider_id is a hidden POST field, not a URL-validated <uuid:…>; a
        # malformed value raises ValidationError on the pk lookup, which would
        # 500 instead of showing the friendly "Provider not found".
        try:
            instance = AIProvider.objects.filter(pk=provider_id).first()
        except (ValidationError, ValueError):
            instance = None
        if instance is None:
            messages.error(request, "Provider not found.")
            return redirect("cpanel:services")

    form = AIProviderForm(request.POST, instance=instance)
    if form.is_valid():
        provider = form.save()
        # Convenience: the first provider ever saved becomes active.
        settings = GlobalSettings.get_settings()
        if settings.active_ai_provider_id is None and AIProvider.objects.count() == 1:
            settings.active_ai_provider = provider
            settings.save(update_fields=["active_ai_provider"])
        messages.success(request, f"Provider '{provider.name}' saved.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def ai_provider_delete_view(request: HttpRequest, provider_id) -> HttpResponse:
    """Delete a provider profile (active FK falls back to none via SET_NULL)."""
    provider = AIProvider.objects.filter(pk=provider_id).first()
    if provider is None:
        messages.error(request, "Provider not found.")
        return redirect("cpanel:services")

    was_active = (
        GlobalSettings.get_settings().active_ai_provider_id == provider.id
    )
    name = provider.name
    provider.delete()
    if was_active:
        messages.warning(
            request,
            f"Provider '{name}' deleted. No provider is active now — AI is "
            "effectively off until you activate another one.",
        )
    else:
        messages.success(request, f"Provider '{name}' deleted.")
    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def ai_provider_activate_view(request: HttpRequest, provider_id) -> HttpResponse:
    """Make one saved provider the active one (one-click switch)."""
    provider = AIProvider.objects.filter(pk=provider_id).first()
    if provider is None:
        messages.error(request, "Provider not found.")
        return redirect("cpanel:services")

    settings = GlobalSettings.get_settings()
    settings.active_ai_provider = provider
    settings.save(update_fields=["active_ai_provider"])
    messages.success(request, f"'{provider.name}' is now the active AI provider.")
    return redirect("cpanel:services")


# --------------------------------------------------------------------------- #
# External Secret Providers (Stage 2)
# --------------------------------------------------------------------------- #
@login_required
@superuser_required
@require_POST
def secret_provider_save_view(request: HttpRequest) -> HttpResponse:
    """Create or update a SecretProvider profile (hidden provider_id = edit)."""
    instance = None
    provider_id = request.POST.get("provider_id")
    if provider_id:
        # Hidden POST field, not a URL-validated <uuid:…>; a malformed value would
        # raise on the pk lookup, so guard it into the friendly "not found".
        try:
            instance = SecretProvider.objects.filter(pk=provider_id).first()
        except (ValidationError, ValueError):
            instance = None
        if instance is None:
            messages.error(request, "Secret provider not found.")
            return redirect("cpanel:services")

    form = SecretProviderForm(request.POST, instance=instance)
    if form.is_valid():
        provider = form.save()
        messages.success(request, f"Secret provider '{provider.name}' saved.")
    else:
        for field, errors in form.errors.items():
            label = "" if field == "__all__" else f"{field}: "
            for error in errors:
                messages.error(request, f"{label}{error}")

    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def secret_provider_delete_view(request: HttpRequest, provider_id) -> HttpResponse:
    """Delete a secret-provider profile, blocked while secrets reference it.

    The FK is ``PROTECT``, so a referenced profile raises ``ProtectedError`` — we
    surface that as a clear, actionable message instead of a 500 (deleting a
    profile out from under secrets would strand them as unresolvable)."""
    provider = SecretProvider.objects.filter(pk=provider_id).first()
    if provider is None:
        messages.error(request, "Secret provider not found.")
        return redirect("cpanel:services")

    name = provider.name
    try:
        provider.delete()
    except ProtectedError:
        n = _secret_counts_by_provider().get(provider.id, 0)
        messages.error(
            request,
            f"Cannot delete '{name}' — {n} secret(s) still reference it. Reassign "
            "or delete those secrets first, then delete the provider.",
        )
        return redirect("cpanel:services")

    messages.success(request, f"Secret provider '{name}' deleted.")
    return redirect("cpanel:services")


@login_required
@superuser_required
@require_POST
def secret_provider_test_view(request: HttpRequest) -> JsonResponse:
    """Test a secret-provider connection (saved row or unsaved form values).

    Body: {"provider_id"?, "provider_type", "config": {...}, "credentials": {...}}.
    Typed credentials override the saved ones (edit-before-save); a saved row gets
    its ``last_tested_at`` stamped on success. The probe provider is transient so
    typed credentials are never persisted by a test.
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

        provider_id = data.get("provider_id")
        saved = SecretProvider.objects.filter(pk=provider_id).first() if provider_id else None
        if provider_id and saved is None:
            return JsonResponse({"success": False, "error": "Provider not found."})

        ptype = data.get("provider_type") or (saved.provider_type if saved else "")
        try:
            backend = get_backend(ptype)
        except SecretResolutionError as e:
            return JsonResponse({"success": False, "error": str(e)})

        config = data.get("config")
        if config is None:
            config = saved.config if saved else {}
        creds = dict(saved.get_credentials()) if saved else {}
        for key, val in (data.get("credentials") or {}).items():
            if val:
                creds[key] = val

        probe = SecretProvider(
            provider_type=ptype,
            name=(saved.name if saved else "test"),
            config=config or {},
        )
        probe.set_credentials(creds)
        ok, message = backend.test_connection(probe)

        if ok and saved is not None:
            saved.last_tested_at = timezone.now()
            saved.save(update_fields=["last_tested_at"])

        return JsonResponse(
            {
                "success": ok,
                "message": message if ok else None,
                "error": message if not ok else None,
            }
        )
    except Exception as e:
        logger.exception("Secret provider connection test failed")
        return JsonResponse({"success": False, "error": str(e)})


@login_required
@superuser_required
@require_POST
def claude_test_connection_view(request: HttpRequest) -> JsonResponse:
    """Test an AI provider connection with a real SDK round-trip.

    Three request shapes:
    - {"provider_id": ...} — test a saved provider row (optional "credential"
      override for edit-before-save);
    - {"provider_type": ..., "credential": ..., ...} — test unsaved form values;
    - {} — test the currently-active provider.
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

        provider_id = data.get("provider_id")
        if provider_id:
            provider = AIProvider.objects.filter(pk=provider_id).first()
            if provider is None:
                return JsonResponse({"success": False, "error": "Provider not found."})
            overrides = {"provider_type", "base_url", "default_model", "auth_method"}
            if overrides & set(data):
                # Edit-form test: unsaved field values, saved credential fallback.
                credential = data.get("credential", "")
                if not credential and provider.credential_encrypted:
                    credential = EncryptionService.decrypt(provider.credential_encrypted)
                ptype = data.get("provider_type") or provider.provider_type
                preset = PROVIDER_PRESETS.get(ptype, {})
                success, message = ClaudeService.test_connection_with_credentials(
                    ptype,
                    credential,
                    auth_method=data.get("auth_method") or provider.auth_method,
                    base_url=data.get("base_url")
                    or provider.base_url
                    or preset.get("base_url", ""),
                    model=data.get("default_model", provider.default_model),
                    extra_env=preset.get("extra_env"),
                )
            else:
                success, message = ClaudeService.test_provider(
                    provider, credential_override=data.get("credential", "")
                )
        elif data.get("provider_type"):
            ptype = data["provider_type"]
            preset = PROVIDER_PRESETS.get(ptype, {})
            success, message = ClaudeService.test_connection_with_credentials(
                ptype,
                data.get("credential", ""),
                auth_method=data.get("auth_method") or AIProvider.AuthMethod.API_KEY,
                base_url=data.get("base_url") or preset.get("base_url", ""),
                model=data.get("default_model", ""),
                extra_env=preset.get("extra_env"),
            )
        else:
            success, message = ClaudeService.test_saved_connection()

        return JsonResponse(
            {
                "success": success,
                "message": message if success else None,
                "error": message if not success else None,
            }
        )
    except Exception as e:
        logger.exception("AI provider connection test failed")
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

    # Per-model breakdown (split by serving provider)
    by_model = []
    for row in (
        base.values("provider", "model")
        .annotate(requests=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        .order_by("-input")
    ):
        by_model.append(
            {
                "provider": row["provider"] or "",
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
