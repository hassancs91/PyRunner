"""
Vectors console (vector seam Stage 1) — collections of the active workspace.

Gated like Databases: a workspace Owner/Admin (or a superuser) manages
collections, because creating one with an embedding provider spends that
provider's budget. Scripts and plugins reach collections through the loopback
helper / SDK in Stage 2; this page is where a human creates, inspects, searches
and deletes them.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import VectorCollectionForm
from core.models import EmbeddingProvider, VectorCollection, WorkspaceMembership
from core.services import VectorError, VectorService
from core.vectors.local_store import SOFT_LIMIT_POINTS
from core.views.ownership import owned_block_message, owned_delete_blocked

BROWSE_PAGE = 20


def _require_manage(request: HttpRequest) -> None:
    """Owner/Admin of the ACTIVE workspace (or superuser) may manage collections."""
    if request.user.is_superuser:
        return
    membership = WorkspaceMembership.objects.filter(
        user=request.user, workspace=request.workspace
    ).first()
    if membership is None:
        raise Http404("Not found")
    if not membership.can_manage:
        raise PermissionDenied(
            "Only a workspace owner or admin can manage vector collections."
        )


def _collection_or_404(request, pk) -> VectorCollection:
    return get_object_or_404(
        VectorCollection.objects.select_related("connection", "embedding_provider"),
        pk=pk,
        workspace=request.workspace,
    )


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"  # pragma: no cover


@login_required
def vector_list_view(request: HttpRequest) -> HttpResponse:
    _require_manage(request)
    collections = list(
        VectorCollection.objects.for_workspace(request.workspace)
        .select_related("connection", "embedding_provider", "created_by")
        .order_by("name")
    )
    for col in collections:
        col.size_display = _human_size(VectorService.size_bytes(col))
        col.over_soft_limit = col.points_count > SOFT_LIMIT_POINTS
    return render(
        request,
        "cpanel/vectors/list.html",
        {
            "collections": collections,
            "available": VectorService.is_available(),
            "providers_count": EmbeddingProvider.objects.filter(enabled=True).count(),
            "soft_limit": SOFT_LIMIT_POINTS,
        },
    )


@login_required
def vector_create_view(request: HttpRequest) -> HttpResponse:
    _require_manage(request)
    if not VectorService.is_available():
        messages.error(
            request,
            "No vector store connection is enabled. Enable one under Services → Vector stores.",
        )
        return redirect("cpanel:vector_list")

    if request.method == "POST":
        form = VectorCollectionForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            try:
                collection = VectorService.create_collection(
                    workspace=request.workspace,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data.get("description") or "",
                    connection=form.cleaned_data["connection"],
                    embedding_provider=form.cleaned_data.get("embedding_provider"),
                    dimension=form.cleaned_data.get("dimension"),
                    metric=form.cleaned_data["metric"],
                    include_in_backup=bool(form.cleaned_data.get("include_in_backup")),
                    created_by=request.user,
                )
            except VectorError as e:
                failed = (
                    VectorCollection.objects.for_workspace(request.workspace)
                    .filter(name=form.cleaned_data["name"])
                    .first()
                )
                if failed is not None:
                    # The row exists in status=error — land on its page.
                    messages.error(request, f"Collection created but its store failed: {e}")
                    return redirect("cpanel:vector_detail", pk=failed.pk)
                form.add_error(None, str(e))
            else:
                messages.success(request, f'Collection "{collection.name}" created.')
                return redirect("cpanel:vector_detail", pk=collection.pk)
    else:
        form = VectorCollectionForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/vectors/create.html",
        {
            "form": form,
            "providers_count": EmbeddingProvider.objects.filter(enabled=True).count(),
        },
    )


def _detail_context(request, collection, **extra) -> dict:
    size = VectorService.size_bytes(collection)
    sample, sample_error = [], ""
    if collection.is_ready:
        try:
            handle = VectorService.handle(collection)
            sample, _ = handle.scroll(limit=BROWSE_PAGE)
        except VectorError as e:
            sample_error = str(e)
    for item in sample:
        item["payload_json"] = json.dumps(item["payload"], ensure_ascii=False)
    context = {
        "collection": collection,
        "size_bytes": size,
        "size_display": _human_size(size),
        "sample": sample,
        "sample_error": sample_error,
        "soft_limit": SOFT_LIMIT_POINTS,
        "over_soft_limit": collection.points_count > SOFT_LIMIT_POINTS,
        "query": "",
        "k": 5,
        "filter_text": "",
        "results": None,
        "search_error": "",
    }
    context.update(extra)
    return context


@login_required
def vector_detail_view(request: HttpRequest, pk) -> HttpResponse:
    _require_manage(request)
    collection = _collection_or_404(request, pk)
    return render(request, "cpanel/vectors/detail.html", _detail_context(request, collection))


@login_required
@require_POST
def vector_search_view(request: HttpRequest, pk) -> HttpResponse:
    """Run a real query from the detail page (the collection's own provider)."""
    _require_manage(request)
    collection = _collection_or_404(request, pk)
    query = (request.POST.get("query") or "").strip()
    filter_text = (request.POST.get("filter") or "").strip()
    try:
        k = max(1, min(int(request.POST.get("k") or 5), 50))
    except ValueError:
        k = 5

    results, error = None, ""
    flt = None
    if filter_text:
        try:
            flt = json.loads(filter_text)
        except ValueError as e:
            error = f"Filter is not valid JSON: {e}"
    if not error:
        if not query:
            error = "Type a query to search."
        else:
            try:
                results = VectorService.handle(collection).search(query, k=k, filter=flt)
                for item in results:
                    item["payload_json"] = json.dumps(item["payload"], ensure_ascii=False)
            except VectorError as e:
                error = str(e)
    collection.refresh_from_db()
    return render(
        request,
        "cpanel/vectors/detail.html",
        _detail_context(
            request,
            collection,
            query=query,
            k=k,
            filter_text=filter_text,
            results=results,
            search_error=error,
        ),
    )


@login_required
@require_POST
def vector_backup_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    _require_manage(request)
    collection = _collection_or_404(request, pk)
    collection.include_in_backup = not collection.include_in_backup
    collection.save(update_fields=["include_in_backup", "updated_at"])
    state = "included in" if collection.include_in_backup else "excluded from"
    messages.success(request, f'Collection "{collection.name}" is now {state} backups.')
    return redirect("cpanel:vector_detail", pk=collection.pk)


@login_required
@require_POST
def vector_retry_view(request: HttpRequest, pk) -> HttpResponse:
    _require_manage(request)
    collection = _collection_or_404(request, pk)
    try:
        VectorService.retry_backend(collection)
    except VectorError as e:
        messages.error(request, f"Store still unavailable: {e}")
    else:
        messages.success(request, f'Collection "{collection.name}" is ready.')
    return redirect("cpanel:vector_detail", pk=collection.pk)


@login_required
@require_POST
def vector_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete the collection and its points (typed-name confirmation)."""
    _require_manage(request)
    collection = _collection_or_404(request, pk)

    if owned_delete_blocked(request, collection):
        messages.error(request, owned_block_message(collection, "collection"))
        return redirect("cpanel:vector_list")

    if request.POST.get("confirm_name", "").strip() != collection.name:
        messages.error(
            request,
            "Confirmation didn't match: type the collection's exact name to delete it.",
        )
        return redirect("cpanel:vector_detail", pk=collection.pk)

    name = collection.name
    VectorService.delete_collection(collection)
    messages.success(request, f'Collection "{name}" deleted.')
    return redirect("cpanel:vector_list")
