"""
Plugin management views (control panel, superuser only).

Thin HTTP layer over PluginService. The heavy lifting (zip-slip-safe install,
isolated preflight, controlled restart) lives in the service; these views just
gate on superuser, call the service, set messages, and redirect.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import PluginUploadForm
from core.models import Plugin
from core.services import PluginInstallError, PluginService

logger = logging.getLogger(__name__)

# Content types for bundled plugin icons (kept in sync with the doctor's
# ALLOWED_ICON_EXTS). SVG is served only as an <img> source, never inlined.
_ICON_CONTENT_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def superuser_required(view_func):
    """Require superuser status (mirrors core.views.settings)."""
    return user_passes_test(lambda u: u.is_superuser, login_url="auth:login")(view_func)


@login_required
@superuser_required
def plugin_list_view(request: HttpRequest) -> HttpResponse:
    """List installed plugins with status, errors, and a restart banner."""
    plugins = list(Plugin.objects.all())
    # Attach owned-resource counts for the delete-confirm preview (Plugin Platform v2).
    for p in plugins:
        p.owned_counts = PluginService.owned_resource_counts(p.slug)
    context = {
        "plugins": plugins,
        "pending_restart": PluginService.pending_restart(),
        # Plugins that failed the guarded boot loader in settings (rare; the
        # preflight-on-boot normally quarantines first). Surfaced for visibility.
        "load_errors": getattr(settings, "PLUGIN_LOAD_ERRORS", {}),
    }
    return render(request, "cpanel/plugins/list.html", context)


@login_required
@superuser_required
def plugin_detail_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Plugin detail page — the local analogue of a marketplace listing.

    Surfaces the packaged metadata (icon, author, license, links, categories,
    description, declared provisions) plus lifecycle actions. Reads from disk via
    the manifest accessors, so it works for INSTALLED-but-not-active plugins too.
    """
    plugin = get_object_or_404(Plugin, slug=slug)
    plugin.owned_counts = PluginService.owned_resource_counts(plugin.slug)
    context = {
        "plugin": plugin,
        "pending_restart": PluginService.pending_restart(),
    }
    return render(request, "cpanel/plugins/detail.html", context)


@login_required
@superuser_required
def plugin_icon_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Serve a plugin's bundled icon straight from PLUGINS_DIR.

    Reads the file from disk (not staticfiles), so an INSTALLED-but-not-active
    plugin's icon still renders and there is no network dependency. The icon path
    comes from the manifest and is re-validated here to stay inside the plugin
    folder (defense-in-depth alongside the doctor's static check).
    """
    plugin = get_object_or_404(Plugin, slug=slug)
    icon_rel = plugin.manifest_value("icon")
    if not icon_rel:
        raise Http404("Plugin has no icon.")

    folder = (Path(settings.PLUGINS_DIR) / plugin.slug).resolve()
    target = (folder / str(icon_rel)).resolve()
    # Path-traversal guard: the resolved file must live under the plugin folder.
    if folder != target and folder not in target.parents:
        raise Http404("Icon path escapes the plugin folder.")
    ext = target.suffix.lower()
    if ext not in _ICON_CONTENT_TYPES or not target.is_file():
        raise Http404("Icon not found.")

    response = FileResponse(open(target, "rb"), content_type=_ICON_CONTENT_TYPES[ext])
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@superuser_required
def plugin_upload_view(request: HttpRequest) -> HttpResponse:
    """Upload + install a plugin .zip (does not load/activate it)."""
    if request.method == "POST":
        form = PluginUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                plugin = PluginService.install_from_zip(form.cleaned_data["plugin_file"])
            except PluginInstallError as exc:
                messages.error(request, f"Upload failed: {exc}")
                return render(request, "cpanel/plugins/upload.html", {"form": form})
            except Exception:
                logger.exception("Unexpected plugin install error")
                messages.error(request, "Upload failed unexpectedly. Check the logs.")
                return render(request, "cpanel/plugins/upload.html", {"form": form})

            provisions = plugin.provisions_summary
            provision_note = f" It will create {provisions}." if provisions else ""
            messages.success(
                request,
                f'Plugin "{plugin.name}" installed.{provision_note} '
                "Click Activate to validate and enable it.",
            )
            return redirect("cpanel:plugin_detail", slug=plugin.slug)
    else:
        form = PluginUploadForm()
    return render(request, "cpanel/plugins/upload.html", {"form": form})


@login_required
@superuser_required
@require_POST
def plugin_activate_view(request: HttpRequest, pk) -> HttpResponse:
    """Validate the plugin in isolation; on success mark it ACTIVE."""
    plugin = get_object_or_404(Plugin, pk=pk)
    ok, output = PluginService.activate(plugin)
    if ok:
        messages.success(
            request,
            f'Plugin "{plugin.name}" passed preflight and is now active. '
            "Restart to apply.",
        )
        if output.strip():  # advisory doctor warnings (non-blocking)
            messages.warning(request, f"Doctor warnings for \"{plugin.name}\":\n{output}")
    else:
        first = (output or "").strip().splitlines()
        detail = first[-1] if first else "see plugin error for details"
        messages.error(request, f'Activation failed for "{plugin.name}": {detail}')
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
@require_POST
def plugin_deactivate_view(request: HttpRequest, pk) -> HttpResponse:
    """Deactivate a plugin (data preserved)."""
    plugin = get_object_or_404(Plugin, pk=pk)
    PluginService.deactivate(plugin)
    messages.success(
        request, f'Plugin "{plugin.name}" deactivated. Restart to apply.'
    )
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
@require_POST
def plugin_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a plugin's files + row (optionally its data)."""
    plugin = get_object_or_404(Plugin, pk=pk)
    name = plugin.name
    remove_data = request.POST.get("remove_data") == "on"
    warnings = PluginService.delete(plugin, remove_data=remove_data)
    for w in warnings:
        messages.warning(request, w)
    messages.success(request, f'Plugin "{name}" deleted. Restart to apply.')
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
@require_POST
def plugin_restart_view(request: HttpRequest) -> HttpResponse:
    """Trigger a controlled restart, then redirect to the interstitial.

    Post/Redirect/Get: the 'restarting…' page lives at a GET URL, so a manual
    refresh during the wait can never re-POST and re-trigger another restart.
    """
    PluginService.trigger_restart()
    return redirect("cpanel:plugin_restarting")


@login_required
@superuser_required
def plugin_restarting_view(request: HttpRequest) -> HttpResponse:
    """The 'restarting…' interstitial (GET → safe to refresh)."""
    return render(request, "cpanel/plugins/restarting.html")
