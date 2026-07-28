"""
Library management views for the control panel.

Libraries are shared Python modules that scripts attach and import. The console
half of the Secrets grammar: create here → attach on the script form → the run
materializes the pinned revision.

Two rules shape these views:

* **Explicit save = one revision.** The editor posts the whole module map and the
  model writes a revision only if the content actually changed. Revisions are
  saves, not keystrokes — an autosaving editor would make history unreadable.
* **History is append-only.** Restore does not rewind ``current_version``; it
  writes the old content forward as a NEW revision, so every Run's stamped
  version keeps resolving to what it actually ran.

Gating matches Scripts (``@login_required`` + workspace-scoped fetches): a
library is script code, so anyone who can write a script can write a library.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import LibraryForm
from core.models import Library
from core.models.library import validate_modules
from core.services.library_service import diff_module_maps
from core.views.ownership import owned_block_message, owned_delete_blocked

# Starter content for a brand-new library, so the first save has something real
# to version and the import shape is obvious from the start. ``{key}`` is filled
# with the library's own key; ``{{name}}`` escapes to a literal f-string brace.
_STARTER_MODULE = '''"""Shared helpers.

Import from a script that has this library attached:

    from {key}.helpers import greet
"""


def greet(name):
    return f"Hello, {{name}}!"
'''


def _modules_for_editor(library) -> dict:
    """The module map the editor should open with (head revision, or a starter)."""
    head = library.head
    if head is not None:
        return head.modules
    return {"helpers.py": _STARTER_MODULE.format(key=library.key)}


def _attached_scripts(library):
    return list(
        library.attached_scripts.select_related("environment").order_by("name")
    )


def _head_revisions(libraries) -> dict:
    """``{library_id: head_revision}`` for ``libraries`` in ONE query, not N."""
    from core.models import LibraryRevision

    wanted = [(lib.id, lib.current_version) for lib in libraries if lib.current_version]
    if not wanted:
        return {}

    q = Q()
    for library_id, version in wanted:
        q |= Q(library_id=library_id, version=version)
    return {rev.library_id: rev for rev in LibraryRevision.objects.filter(q)}


@login_required
def library_list_view(request: HttpRequest) -> HttpResponse:
    """List the active workspace's libraries."""
    libraries = list(
        Library.objects.for_workspace(request.workspace)
        .annotate(attached_count=Count("attachments"))
        .order_by("key")
    )

    # Owner filter (Plugin Platform v2), computed across the whole workspace so
    # the dropdown always offers every owner regardless of the active filter.
    owners = sorted({lib.owner_plugin for lib in libraries if lib.owner_plugin})
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        libraries = [lib for lib in libraries if lib.owner_plugin == owner_filter]

    heads = _head_revisions(libraries)
    for lib in libraries:
        head = heads.get(lib.id)
        lib.module_count = len(head.modules) if head else 0

    return render(
        request,
        "cpanel/libraries/list.html",
        {
            "libraries": libraries,
            "owners": owners,
            "selected_owner": owner_filter or "",
        },
    )


@login_required
def library_create_view(request: HttpRequest) -> HttpResponse:
    """Create a library (metadata only; content is added in the editor)."""
    if request.method == "POST":
        form = LibraryForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            library = form.save(commit=False)
            library.created_by = request.user
            library.workspace = request.workspace
            library.save()
            messages.success(
                request,
                f'Library "{library.key}" created. Add your modules and save to '
                "create version 1.",
            )
            return redirect("cpanel:library_edit", pk=library.pk)
    else:
        form = LibraryForm(workspace=request.workspace)

    return render(request, "cpanel/libraries/create.html", {"form": form})


@login_required
def library_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """A library's modules, attached scripts, and revision history."""
    library = get_object_or_404(Library, pk=pk, workspace=request.workspace)
    head = library.head

    return render(
        request,
        "cpanel/libraries/detail.html",
        {
            "library": library,
            "head": head,
            "modules": sorted((head.modules if head else {}).items()),
            "attached_scripts": _attached_scripts(library),
            "revisions": library.revisions.select_related("created_by").all(),
        },
    )


@login_required
def library_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit a library's metadata AND its modules; an explicit save = one revision."""
    library = get_object_or_404(Library, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = LibraryForm(request.POST, instance=library, workspace=request.workspace)
        modules, modules_error = _parse_posted_modules(request)

        if form.is_valid() and modules_error is None:
            library = form.save()
            _, created = library.save_revision(modules, created_by=request.user)
            if created:
                messages.success(
                    request,
                    f'Library "{library.key}" saved as version {library.current_version}.',
                )
            else:
                # Honest: a save that changed nothing did NOT make a revision.
                messages.info(
                    request,
                    f'Library "{library.key}" updated. The code is unchanged, so no '
                    "new version was created.",
                )
            return redirect("cpanel:library_detail", pk=library.pk)

        if modules_error:
            messages.error(request, modules_error)
        modules_json = request.POST.get("modules_json") or "{}"
    else:
        form = LibraryForm(instance=library, workspace=request.workspace)
        modules_json = json.dumps(_modules_for_editor(library))

    return render(
        request,
        "cpanel/libraries/edit.html",
        {
            "form": form,
            "library": library,
            "modules_json": modules_json,
        },
    )


def _parse_posted_modules(request):
    """Return ``(modules, error)`` from the editor's hidden ``modules_json`` field.

    The editor posts the whole map as JSON. Validation is the SAME shared
    validator the SDK uses, so console and plugin paths cannot drift on what a
    legal module set is.
    """
    raw = request.POST.get("modules_json") or ""
    if not raw.strip():
        return None, "No modules were submitted."
    try:
        modules = json.loads(raw)
    except json.JSONDecodeError:
        return None, "The editor sent malformed module data. Nothing was saved."

    try:
        validate_modules(modules)
    except ValidationError as e:
        return None, " ".join(e.messages)
    return modules, None


@login_required
@require_POST
def library_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a library — refused while any script still imports it."""
    library = get_object_or_404(Library, pk=pk, workspace=request.workspace)

    # Both refusals below leave the library alive, so both return to it rather
    # than to the list — the message is about THIS library and acting on it
    # (detaching, or going to the plugin) starts from its page.
    if owned_delete_blocked(request, library):
        messages.error(request, owned_block_message(library, "library"))
        return redirect("cpanel:library_detail", pk=library.pk)

    # Delete guard: a library vanishing under a script would turn that script's
    # next run into a fail-closed error. Name the scripts so the fix is obvious.
    attached = _attached_scripts(library)
    if attached:
        names = ", ".join(f'"{s.name}"' for s in attached[:5])
        more = f" and {len(attached) - 5} more" if len(attached) > 5 else ""
        messages.error(
            request,
            f'Cannot delete library "{library.key}": it is still attached to '
            f"{names}{more}. Detach it from those scripts first.",
        )
        return redirect("cpanel:library_detail", pk=library.pk)

    key = library.key
    library.delete()
    messages.success(request, f'Library "{key}" deleted.')
    return redirect("cpanel:library_list")


@login_required
def library_revision_view(request: HttpRequest, pk, version: int) -> HttpResponse:
    """Read-only view of one revision, with its diff against the current head."""
    library = get_object_or_404(Library, pk=pk, workspace=request.workspace)
    revision = library.revision(version)
    if revision is None:
        messages.error(request, f"Version {version} does not exist for this library.")
        return redirect("cpanel:library_detail", pk=library.pk)

    head = library.head
    diff = diff_module_maps(revision.modules, head.modules if head else {})

    return render(
        request,
        "cpanel/libraries/revision.html",
        {
            "library": library,
            "revision": revision,
            "head": head,
            "is_head": head is not None and revision.version == head.version,
            "modules": sorted(revision.modules.items()),
            "diff": diff,
            "has_changes": any(d["status"] != "unchanged" for d in diff),
        },
    )


@login_required
@require_POST
def library_revision_restore_view(request: HttpRequest, pk, version: int) -> HttpResponse:
    """Restore an old revision by writing its content forward as a NEW revision.

    Never rewinds ``current_version``: run stamps must stay monotonic, and history
    is append-only.
    """
    library = get_object_or_404(Library, pk=pk, workspace=request.workspace)
    revision = library.revision(version)
    if revision is None:
        messages.error(request, f"Version {version} does not exist for this library.")
        return redirect("cpanel:library_detail", pk=library.pk)

    _, created = library.save_revision(revision.modules, created_by=request.user)
    if created:
        messages.success(
            request,
            f"Restored version {version} as new version {library.current_version}.",
        )
    else:
        messages.info(
            request,
            f"Version {version} is already the current content — nothing to restore.",
        )
    return redirect("cpanel:library_detail", pk=library.pk)
