"""
SDK Showcase plugin — views (web process), superuser-only.

A single page of capability cards, each demonstrating one ``core.plugins.api``
surface. All persistence/orchestration goes through ``provisioning`` (which uses
the SDK), so this module never imports core models/tasks directly.
"""

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.plugins.api import VectorError

from . import provisioning as prov
from .forms import SetupForm

superuser_required = user_passes_test(lambda u: u.is_superuser)


def _build_form(data=None):
    return SetupForm(
        data,
        initial=None if data else prov.initial_from_state(),
        environments=prov.list_environments(),
        has_secret=prov.has_secret(),
    )


def _render(request, form, **extra):
    is_setup = prov.is_setup()
    ctx = {
        "form": form,
        "has_environments": bool(prov.list_environments()),
        "is_setup": is_setup,
        "counter": prov.get_counter(),
        "store_keys": prov.store_keys(),
        "config": prov.get_config(),
        "secret_redacted": prov.secret_redacted(),
        "schedule": prov.schedule_summary(),
        "inventory": prov.owned_inventory() if is_setup else None,
        "worker_runs": list(reversed(prov.get_runs()))[:10],
        "sdk_runs": prov.recent_runs(10),
        "is_active": prov.live_status()["active"],
        "vectors": prov.vector_summary() if is_setup else None,
        "vector_topics": sorted({topic for topic, _ in prov.SAMPLE_DOCS}),
        "vector_query": "",
        "vector_topic": "",
        "vector_results": None,
    }
    ctx.update(extra)
    return render(request, "sdk_showcase/index.html", ctx)


@superuser_required
def index(request):
    return _render(request, _build_form())


@superuser_required
@require_POST
def setup(request):
    form = _build_form(data=request.POST)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return _render(request, form)
    try:
        _script, warnings = prov.provision(form.cleaned_data, created_by=request.user)
    except Exception as exc:
        messages.error(request, f"Could not set up the demo: {exc}")
        return _render(request, form)
    for warning in warnings:
        messages.warning(request, warning)
    messages.success(
        request,
        "Demo provisioned — data store, secret, script, schedule and vector collection are ready.",
    )
    return redirect(reverse("sdk_showcase:index"))


@superuser_required
@require_POST
def increment(request):
    value = prov.increment_counter()
    messages.info(request, f"Counter is now {value} (written to the data store).")
    return redirect(reverse("sdk_showcase:index") + "#datastore")


@superuser_required
@require_POST
def run_demo(request):
    run, error = prov.queue_demo_run(triggered_by=request.user)
    if error:
        messages.error(request, error)
    else:
        messages.info(request, "Demo run queued — watch it below.")
    return redirect(reverse("sdk_showcase:index") + "#runs")


@superuser_required
@require_POST
def stop(request):
    if prov.cancel_running():
        messages.info(request, "Stopping the running demo…")
    else:
        messages.error(request, "There's no running demo to stop.")
    return redirect(reverse("sdk_showcase:index") + "#runs")


@superuser_required
@require_POST
def set_schedule(request):
    mode = request.POST.get("mode", "manual")
    if prov.sync_schedule(mode):
        messages.success(request, f"Schedule set to: {mode}.")
    else:
        messages.error(request, "Set up the demo first.")
    return redirect(reverse("sdk_showcase:index") + "#schedule")


@superuser_required
@require_POST
def reset_demo(request):
    prov.reset_demo_data()
    messages.info(request, "Demo data reset (counter + history cleared).")
    return redirect(reverse("sdk_showcase:index") + "#ownership")


@superuser_required
@require_POST
def index_docs(request):
    try:
        count = prov.index_sample_docs()
    except (ValueError, VectorError) as exc:
        messages.error(request, f"Could not index the sample docs: {exc}")
    else:
        messages.success(request, f"Sample docs indexed — the collection holds {count} point(s).")
    return redirect(reverse("sdk_showcase:index") + "#vectors")


@superuser_required
@require_POST
def search_docs(request):
    query = (request.POST.get("query") or "").strip()
    topic = request.POST.get("topic") or ""
    results = []
    if not query:
        messages.error(request, "Type something to search for.")
    else:
        try:
            results = prov.search_docs(query, topic=topic or None)
        except (ValueError, VectorError) as exc:
            messages.error(request, f"Search failed: {exc}")
    return _render(
        request,
        _build_form(),
        vector_query=query,
        vector_topic=topic,
        vector_results=results,
    )


@superuser_required
def status(request):
    """JSON snapshot for the page's live-status poller (run state + progress)."""
    return JsonResponse(prov.live_status())
