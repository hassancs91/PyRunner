"""
Qdrant Backup plugin — views (web process).

Superuser-only. The page is a config form (top) that provisions an owned Script +
secrets + data store + schedule through the SDK, plus a focused dashboard built
from the run history the worker appends to the ``qdrant_backup:state`` data store.

All persistence/orchestration goes through ``provisioning`` (which uses
``core.plugins.api``), so this module never imports core models/tasks directly.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import provisioning as prov
from .forms import QdrantBackupConfigForm

superuser_required = user_passes_test(lambda u: u.is_superuser)

HISTORY_LIMIT = 50  # rows shown in the dashboard history table

# status -> (label, pill classes, dot classes) for the run-history badges.
_BADGES = {
    "success": ("Success", "bg-ok/10 text-ok", "bg-ok"),
    "partial": ("Partial", "bg-warn/10 text-warn", "bg-warn"),
    "failed": ("Failed", "bg-fail/10 text-fail", "bg-fail"),
}


def _badge(status):
    label, cls, dot = _BADGES.get(status, ("Unknown", "bg-panel-hi text-muted", "bg-muted"))
    return {"label": label, "cls": cls, "dot": dot}


def _record(raw):
    """Normalize one stored run dict into the shape the template expects."""
    status = raw.get("status", "unknown")
    return {
        "ts": raw.get("ts", "—"),
        "date": raw.get("date") or raw.get("ts", "")[:10],
        "status": status,
        "badge": _badge(status),
        "collection_count": raw.get("collection_count", 0),
        "failed_count": raw.get("failed_count", 0),
        "total_size_mb": round(raw.get("total_size_mb", 0) or 0, 2),
        "duration_s": round(raw.get("duration_s", 0) or 0, 1),
        "deleted_old": raw.get("deleted_old", 0),
        "error": raw.get("error", ""),
        "zip_key": raw.get("zip_key", ""),
        "collections": [c for c in raw.get("collections", []) if isinstance(c, dict)],
    }


def _dl_url(date, *, collection=None, want_zip=False):
    """Build the in-app download URL (it 302-redirects to a presigned R2 URL)."""
    params = {"date": date, "zip": "1"} if want_zip else {"date": date, "collection": collection}
    return reverse("qdrant_backup:download") + "?" + urlencode(params)


def _dashboard_context():
    runs = [r for r in prov.get_runs() if isinstance(r, dict)]
    history = [_record(r) for r in reversed(runs[-HISTORY_LIMIT:])]
    for r in history:
        r["zip_url"] = _dl_url(r["date"], want_zip=True) if r.get("zip_key") else ""
    latest = history[0] if history else None

    collections = []
    if latest:
        for c in latest["collections"]:
            s3 = c.get("s3_key") or ""
            linkable = c.get("status") == "ok" and s3 and s3 != "FAILED"
            collections.append({
                "collection": c.get("collection", "—"),
                "size_mb": round(c.get("size_mb", 0) or 0, 2),
                "status": c.get("status", "ok"),
                "error": c.get("error", ""),
                "download_url": _dl_url(latest["date"], collection=c.get("collection")) if linkable else "",
            })
    script = prov.get_script()
    return {
        "is_configured": script is not None,
        "has_data": bool(history),
        "latest": latest,
        "history": history,
        "collections": collections,
        "can_run": bool(script and script.can_run),
    }


def _build_form(request, data=None):
    return QdrantBackupConfigForm(
        data,
        initial=None if data else prov.initial_from_config(),
        environments=prov.list_environments(),
        configured_secrets=prov.configured_secret_keys(),
    )


def _render(request, form):
    ctx = {
        "form": form,
        "has_environments": bool(prov.list_environments()),
        "schedule": prov.schedule_summary(),
        "is_active": prov.live_status()["active"],
    }
    ctx.update(_dashboard_context())
    return render(request, "qdrant_backup/index.html", ctx)


@superuser_required
def index(request):
    return _render(request, _build_form(request))


@superuser_required
@require_POST
def save(request):
    form = _build_form(request, data=request.POST)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return _render(request, form)

    try:
        _, warnings = prov.provision(form.cleaned_data, created_by=request.user)
    except Exception as exc:
        messages.error(request, f"Could not save settings: {exc}")
        return _render(request, form)

    messages.success(request, "Settings saved — the backup script, secrets and schedule are provisioned.")
    for w in warnings:
        messages.warning(request, w)
    return redirect(reverse("qdrant_backup:index") + "#settings")


@superuser_required
@require_POST
def run_backup(request):
    run, error = prov.queue_backup(triggered_by=request.user)
    if error:
        messages.error(request, error)
    else:
        messages.info(request, "Backup queued — watch it run below.")
    return redirect(reverse("qdrant_backup:index") + "#backups")


@superuser_required
def status(request):
    """JSON snapshot for the page's live-status poller (run state + progress)."""
    return JsonResponse(prov.live_status())


@superuser_required
@require_POST
def stop(request):
    """Cancel the running/pending backup (SDK → shared force-stop)."""
    if prov.cancel_running():
        messages.info(request, "Stopping the running backup…")
    else:
        messages.error(request, "There's no running backup to stop.")
    return redirect(reverse("qdrant_backup:index") + "#backups")


@superuser_required
@require_POST
def test_qdrant(request):
    """Probe the Qdrant connection with the submitted (or saved) credentials."""
    return JsonResponse(prov.test_qdrant(request.POST))


@superuser_required
@require_POST
def test_r2(request):
    """Probe the R2 connection with the submitted (or saved) credentials."""
    return JsonResponse(prov.test_r2(request.POST))


@superuser_required
def download(request):
    """Redirect to a short-lived presigned R2 URL for a recorded backup object.

    The key is resolved from the stored run history (never trusted from the
    query string), so only objects we actually backed up can be signed. The file
    then downloads browser→R2 directly — it never streams through PyRunner.
    """
    date = request.GET.get("date", "").strip()
    collection = request.GET.get("collection", "").strip()
    want_zip = request.GET.get("zip") == "1"

    key = prov.resolve_download_key(date, collection=(collection or None), want_zip=want_zip)
    if not key:
        messages.error(request, "That backup file isn't available for download.")
        return redirect("qdrant_backup:index")

    url = prov.presigned_url(key)
    if not url:
        messages.error(request, "Couldn't generate a download link — check the R2 settings.")
        return redirect("qdrant_backup:index")

    return redirect(url)
