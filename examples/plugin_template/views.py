"""
plugin_template — the smallest correct PyRunner plugin. Copy this folder, rename
the slug (see README), and build from here.

A single superuser page that stores and reads one value through the SDK
(``core.plugins.api``) — no models, no migrations, import-light.
"""

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.plugins.api import DataStoreAPI

OWNER = "plugin_template"   # everything this plugin owns is stamped with this slug
STORE_KEY = "state"

superuser_required = user_passes_test(lambda u: u.is_superuser)


@superuser_required
def index(request):
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    note = store.get("note", "") if store is not None else ""
    return render(request, "plugin_template/index.html", {"note": note})


@superuser_required
@require_POST
def save(request):
    # upsert() is idempotent — the owned store is created once, reused after.
    store = DataStoreAPI(OWNER).upsert(STORE_KEY, description="plugin_template demo state")
    store.set("note", request.POST.get("note", "").strip())
    messages.success(request, "Saved to the plugin's data store.")
    return redirect(reverse("plugin_template:index"))
