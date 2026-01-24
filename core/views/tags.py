"""
Tag management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import TagForm
from core.models import Tag


@login_required
def tag_list_view(request: HttpRequest) -> HttpResponse:
    """List all tags with script counts."""
    tags = Tag.objects.all().order_by("name")
    return render(
        request,
        "cpanel/tags/list.html",
        {"tags": tags},
    )


@login_required
def tag_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new tag."""
    if request.method == "POST":
        form = TagForm(request.POST)
        if form.is_valid():
            tag = form.save(commit=False)
            tag.created_by = request.user
            tag.save()
            messages.success(request, f'Tag "{tag.name}" created successfully.')
            return redirect("cpanel:tag_list")
    else:
        form = TagForm()

    return render(
        request,
        "cpanel/tags/create.html",
        {"form": form},
    )


@login_required
def tag_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing tag."""
    tag = get_object_or_404(Tag, pk=pk)

    if request.method == "POST":
        form = TagForm(request.POST, instance=tag)
        if form.is_valid():
            form.save()
            messages.success(request, f'Tag "{tag.name}" updated successfully.')
            return redirect("cpanel:tag_list")
    else:
        form = TagForm(instance=tag)

    return render(
        request,
        "cpanel/tags/edit.html",
        {"form": form, "tag": tag},
    )


@login_required
@require_POST
def tag_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a tag."""
    tag = get_object_or_404(Tag, pk=pk)
    name = tag.name
    tag.delete()
    messages.success(request, f'Tag "{name}" deleted successfully.')
    return redirect("cpanel:tag_list")
