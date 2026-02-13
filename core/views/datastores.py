"""
Data Store management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import DataStoreForm, DataStoreEntryForm
from core.models import DataStore, DataStoreEntry
from core.services import DatastoreService


@login_required
def datastore_list_view(request: HttpRequest) -> HttpResponse:
    """List all data stores."""
    datastores = DatastoreService.get_datastores_with_stats()

    # Format size for each datastore
    for ds in datastores:
        ds.size_display = DatastoreService.format_size(ds.size_bytes)

    total_size = DatastoreService.get_total_size()
    total_size_display = DatastoreService.format_size(total_size)

    return render(
        request,
        "cpanel/datastores/list.html",
        {
            "datastores": datastores,
            "total_size_display": total_size_display,
            "datastore_count": len(datastores),
        },
    )


@login_required
def datastore_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new data store."""
    if request.method == "POST":
        form = DataStoreForm(request.POST)
        if form.is_valid():
            datastore = form.save(commit=False)
            datastore.created_by = request.user
            datastore.save()

            messages.success(request, f'Data store "{datastore.name}" created successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreForm()

    return render(
        request,
        "cpanel/datastores/create.html",
        {
            "form": form,
        },
    )


@login_required
def datastore_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View a data store with all its entries."""
    datastore = get_object_or_404(DataStore, pk=pk)
    entries = datastore.entries.all().order_by("key")

    return render(
        request,
        "cpanel/datastores/detail.html",
        {
            "datastore": datastore,
            "entries": entries,
        },
    )


@login_required
def datastore_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit a data store's metadata."""
    datastore = get_object_or_404(DataStore, pk=pk)

    if request.method == "POST":
        form = DataStoreForm(request.POST, instance=datastore)
        if form.is_valid():
            form.save()
            messages.success(request, f'Data store "{datastore.name}" updated successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreForm(instance=datastore)

    return render(
        request,
        "cpanel/datastores/edit.html",
        {
            "form": form,
            "datastore": datastore,
        },
    )


@login_required
@require_POST
def datastore_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a data store and all its entries."""
    datastore = get_object_or_404(DataStore, pk=pk)
    name = datastore.name
    datastore.delete()

    messages.success(request, f'Data store "{name}" deleted successfully.')
    return redirect("cpanel:datastore_list")


@login_required
@require_POST
def datastore_clear_view(request: HttpRequest, pk) -> HttpResponse:
    """Clear all entries from a data store."""
    datastore = get_object_or_404(DataStore, pk=pk)
    count = datastore.entries.count()
    datastore.entries.all().delete()

    messages.success(request, f'Cleared {count} entries from "{datastore.name}".')
    return redirect("cpanel:datastore_detail", pk=datastore.pk)


# =============================================================================
# Entry Views
# =============================================================================


@login_required
def datastore_entry_create_view(request: HttpRequest, pk) -> HttpResponse:
    """Add a new entry to a data store."""
    datastore = get_object_or_404(DataStore, pk=pk)

    if request.method == "POST":
        form = DataStoreEntryForm(request.POST, datastore=datastore)
        if form.is_valid():
            entry = DataStoreEntry(
                datastore=datastore,
                key=form.cleaned_data["key"],
                value_json=form.cleaned_data["value"],
            )
            entry.save()

            messages.success(request, f'Entry "{entry.key}" added successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreEntryForm(datastore=datastore)

    return render(
        request,
        "cpanel/datastores/entry_create.html",
        {
            "form": form,
            "datastore": datastore,
        },
    )


@login_required
def datastore_entry_edit_view(request: HttpRequest, pk, entry_pk) -> HttpResponse:
    """Edit an existing entry."""
    datastore = get_object_or_404(DataStore, pk=pk)
    entry = get_object_or_404(DataStoreEntry, pk=entry_pk, datastore=datastore)

    if request.method == "POST":
        form = DataStoreEntryForm(request.POST, datastore=datastore, instance=entry)
        if form.is_valid():
            entry.key = form.cleaned_data["key"]
            entry.value_json = form.cleaned_data["value"]
            entry.save()

            messages.success(request, f'Entry "{entry.key}" updated successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreEntryForm(datastore=datastore, instance=entry)

    return render(
        request,
        "cpanel/datastores/entry_edit.html",
        {
            "form": form,
            "datastore": datastore,
            "entry": entry,
        },
    )


@login_required
@require_POST
def datastore_entry_delete_view(request: HttpRequest, pk, entry_pk) -> HttpResponse:
    """Delete an entry from a data store."""
    datastore = get_object_or_404(DataStore, pk=pk)
    entry = get_object_or_404(DataStoreEntry, pk=entry_pk, datastore=datastore)
    key = entry.key
    entry.delete()

    messages.success(request, f'Entry "{key}" deleted successfully.')
    return redirect("cpanel:datastore_detail", pk=datastore.pk)
