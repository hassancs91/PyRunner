"""
Databases management views for the control panel (Owner/Admin only).

Managed databases create real server-side objects (a Postgres schema + role)
on the attached data server, so the whole section is gated to workspace
Owners/Admins (or a superuser) — unlike data stores, which any member may
manage. Scripts get access through explicit ``DatabaseGrant`` rows managed on
the detail page, then connect from code via the ``pyrunner_db`` helper.
"""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import DatabaseForm
from core.models import Database, DatabaseGrant, Script, WorkspaceMembership
from core.services import (
    DatabaseExplorerError,
    DatabaseExplorerService,
    DatabaseProvisionError,
    DatabaseService,
)
from core.views.ownership import owned_block_message, owned_delete_blocked


def _require_manage(request: HttpRequest) -> None:
    """Owner/Admin of the ACTIVE workspace (or superuser) may manage databases.

    Same disclosure discipline as workspace management: a non-member gets 404,
    a member without a manage role gets 403.
    """
    if request.user.is_superuser:
        return
    membership = WorkspaceMembership.objects.filter(
        user=request.user, workspace=request.workspace
    ).first()
    if membership is None:
        raise Http404("Not found")
    if not membership.can_manage:
        raise PermissionDenied("Only a workspace owner or admin can manage databases.")


def _reconcile_grants(database, script_ids, workspace) -> None:
    """Make the database's grant set exactly match ``script_ids``.

    Only scripts in the active workspace are grantable. Adds missing grants,
    removes deselected ones; never touches grants for scripts outside the
    workspace's reach. (Mirrors the secrets grant reconcile in scripts.py.)
    """
    valid = {
        str(pk)
        for pk in Script.objects.for_workspace(workspace)
        .filter(pk__in=[s for s in script_ids if s])
        .values_list("pk", flat=True)
    }
    existing = {
        str(g.script_id): g for g in DatabaseGrant.objects.filter(database=database)
    }
    for sid in valid - existing.keys():
        DatabaseGrant.objects.create(database=database, script_id=sid, active=True)
    for sid in existing.keys() - valid:
        existing[sid].delete()


def _detail_context(request: HttpRequest, database: Database, **extra) -> dict:
    granted_ids = {
        str(g.script_id)
        for g in DatabaseGrant.objects.filter(database=database, active=True)
    }
    scripts = list(Script.objects.for_workspace(request.workspace).order_by("name"))

    # The View half of the explorer: the database's tables, read through its
    # own scoped role. Degrades to a banner — a down data server must not take
    # the management page with it.
    tables, tables_error = [], ""
    if database.is_ready and DatabaseService.is_configured():
        try:
            tables = DatabaseExplorerService.tables(database)
        except DatabaseExplorerError as e:
            tables_error = str(e)

    context = {
        "database": database,
        "server": DatabaseService.server_info(),
        "scripts": scripts,
        "granted_ids": granted_ids,
        "granted_count": len(granted_ids),
        "tables": tables,
        "tables_error": tables_error,
    }
    context.update(extra)
    return context


@login_required
def database_list_view(request: HttpRequest) -> HttpResponse:
    """List the active workspace's databases + the data-server status card."""
    _require_manage(request)
    databases = list(
        Database.objects.for_workspace(request.workspace)
        .select_related("created_by")
        .order_by("name")
    )
    # Live per-database metrics (size / tables / connections) — one provisioner
    # query for the whole page; {} when the server is unreachable.
    stats = (
        DatabaseExplorerService.stats_for_workspace(request.workspace)
        if databases and DatabaseService.is_configured()
        else {}
    )
    for db in databases:
        db.granted_count = DatabaseGrant.objects.filter(
            database=db, active=True
        ).count()
        db.stats = stats.get(db.id)

    return render(
        request,
        "cpanel/databases/list.html",
        {
            "databases": databases,
            "database_count": len(databases),
            "server": DatabaseService.server_info(),
        },
    )


@login_required
def database_monitor_view(request: HttpRequest) -> HttpResponse:
    """The Monitor half of the explorer: live activity for THIS workspace's
    databases — connections, long-running/blocked queries, idle-in-transaction,
    sizes, and slow-query history when pg_stat_statements is available."""
    _require_manage(request)
    if not DatabaseService.is_configured():
        messages.error(request, "No data server is configured.")
        return redirect("cpanel:database_list")

    activity = DatabaseExplorerService.activity_for_workspace(request.workspace)
    slow = DatabaseExplorerService.slow_queries_for_workspace(request.workspace)
    stats = DatabaseExplorerService.stats_for_workspace(request.workspace)
    databases = list(
        Database.objects.for_workspace(request.workspace).order_by("name")
    )
    for db in databases:
        db.stats = stats.get(db.id)

    return render(
        request,
        "cpanel/databases/monitor.html",
        {
            "server": DatabaseService.server_info(),
            "activity": activity,
            "slow": slow,
            "databases": databases,
        },
    )


@login_required
@require_POST
def database_server_test_view(request: HttpRequest) -> HttpResponse:
    """Probe the attached data server and report the result."""
    _require_manage(request)
    if not DatabaseService.is_configured():
        messages.error(
            request,
            "No data server is configured. Set PYRUNNER_DATA_DB_URL and restart.",
        )
        return redirect("cpanel:database_list")

    ok, detail = DatabaseService.test_connection()
    if ok:
        messages.success(request, detail)
    else:
        messages.error(request, detail)
    return redirect("cpanel:database_list")


@login_required
def database_create_view(request: HttpRequest) -> HttpResponse:
    """Create a database: row first, then live provisioning on the data server."""
    _require_manage(request)
    if not DatabaseService.is_configured():
        messages.error(
            request,
            "No data server is configured. Set PYRUNNER_DATA_DB_URL to enable "
            "the Databases feature.",
        )
        return redirect("cpanel:database_list")

    if request.method == "POST":
        form = DatabaseForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            try:
                database = DatabaseService.create_database(
                    name=form.cleaned_data["name"],
                    workspace=request.workspace,
                    description=form.cleaned_data["description"],
                    created_by=request.user,
                )
            except DatabaseProvisionError as e:
                # The row exists in status=error (see the service) — land on
                # its page where the cause and a Retry button are shown.
                failed = (
                    Database.objects.for_workspace(request.workspace)
                    .filter(name=form.cleaned_data["name"])
                    .first()
                )
                messages.error(request, f"Provisioning failed: {e}")
                if failed is not None:
                    return redirect("cpanel:database_detail", pk=failed.pk)
                return redirect("cpanel:database_list")

            messages.success(
                request, f'Database "{database.name}" created and provisioned.'
            )
            return redirect("cpanel:database_detail", pk=database.pk)
    else:
        form = DatabaseForm(workspace=request.workspace)

    return render(request, "cpanel/databases/create.html", {"form": form})


@login_required
def database_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """A database's status, connection facts, and script grants."""
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)
    return render(
        request, "cpanel/databases/detail.html", _detail_context(request, database)
    )


@login_required
def database_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit a database's metadata (name/description).

    Renaming only changes the PyRun handle scripts use; the provisioned
    schema/role names are stored verbatim and stay as they are.
    """
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = DatabaseForm(request.POST, instance=database, workspace=request.workspace)
        if form.is_valid():
            form.save()
            messages.success(request, f'Database "{database.name}" updated.')
            return redirect("cpanel:database_detail", pk=database.pk)
    else:
        form = DatabaseForm(instance=database, workspace=request.workspace)

    return render(
        request,
        "cpanel/databases/edit.html",
        {"form": form, "database": database},
    )


@login_required
@require_POST
def database_grants_view(request: HttpRequest, pk) -> HttpResponse:
    """Reconcile which scripts may connect to this database."""
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)
    _reconcile_grants(
        database, request.POST.getlist("granted_script_ids"), request.workspace
    )
    messages.success(request, f'Script access for "{database.name}" updated.')
    return redirect("cpanel:database_detail", pk=database.pk)


@login_required
@require_POST
def database_retry_view(request: HttpRequest, pk) -> HttpResponse:
    """Re-run provisioning after a failure (the service is idempotent)."""
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)
    try:
        DatabaseService.provision(database)
    except DatabaseProvisionError as e:
        messages.error(request, f"Provisioning failed: {e}")
    else:
        messages.success(request, f'Database "{database.name}" provisioned.')
    return redirect("cpanel:database_detail", pk=database.pk)


@login_required
@require_POST
def database_reveal_view(request: HttpRequest, pk) -> HttpResponse:
    """Show the scoped DSN (with password) once, for external SQL clients.

    POST-only and never persisted in the page URL; the DSN is exactly what a
    granted script receives, so revealing it to the Owner/Admin who provisioned
    the database adds no new capability.
    """
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)
    if not database.is_ready:
        messages.error(request, "This database is not ready — no credentials to show.")
        return redirect("cpanel:database_detail", pk=database.pk)
    try:
        revealed_dsn = DatabaseService.scoped_dsn(database)
    except Exception as e:
        messages.error(request, f"Could not build the connection string: {e}")
        return redirect("cpanel:database_detail", pk=database.pk)
    return render(
        request,
        "cpanel/databases/detail.html",
        _detail_context(request, database, revealed_dsn=revealed_dsn),
    )


def _resolve_table(request: HttpRequest, pk, table: str):
    """Shared lookup for table-addressed pages: the database (workspace-scoped)
    plus the validated table entry. 404s on an unknown table BEFORE the name is
    ever used in a query (it is identifier-quoted afterwards regardless)."""
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)
    if not database.is_ready:
        raise Http404("Database is not ready")
    info = DatabaseExplorerService.table_or_none(database, table)
    if info is None:
        raise Http404("No such table")
    return database, info


@login_required
def database_table_view(request: HttpRequest, pk, table: str) -> HttpResponse:
    """Read-only table browser: structure + one page of rows."""
    _require_manage(request)
    try:
        database, info = _resolve_table(request, pk, table)
        try:
            page = max(1, int(request.GET.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        columns = DatabaseExplorerService.columns(database, table)
        indexes = DatabaseExplorerService.indexes(database, table)
        grid = DatabaseExplorerService.rows(database, table, page=page)
    except DatabaseExplorerError as e:
        messages.error(request, f"Could not read the table: {e}")
        return redirect("cpanel:database_detail", pk=pk)

    return render(
        request,
        "cpanel/databases/table.html",
        {
            "database": database,
            "table": info,
            "columns": columns,
            "indexes": indexes,
            "grid": grid,
            "page": page,
            "has_prev": page > 1,
            "has_next": grid.truncated,
        },
    )


@login_required
def database_table_csv_view(request: HttpRequest, pk, table: str) -> HttpResponse:
    """CSV export of a table (read-only, capped — the cap is stated in the UI)."""
    _require_manage(request)
    try:
        database, _info = _resolve_table(request, pk, table)
        result = DatabaseExplorerService.csv_rows(database, table)
    except DatabaseExplorerError as e:
        messages.error(request, f"Could not export the table: {e}")
        return redirect("cpanel:database_detail", pk=pk)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="{database.name}_{table}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(row)
    return response


@login_required
@require_POST
def database_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Drop the schema (all data!) + role, then delete the row.

    Requires the database's name typed back as confirmation. If the server
    cleanup fails the row is kept by default so the objects aren't orphaned
    silently; 'force' skips that safety for a decommissioned/unreachable
    server.
    """
    _require_manage(request)
    database = get_object_or_404(Database, pk=pk, workspace=request.workspace)

    if owned_delete_blocked(request, database):
        messages.error(request, owned_block_message(database, "database"))
        return redirect("cpanel:database_list")

    if request.POST.get("confirm_name", "").strip() != database.name:
        messages.error(
            request,
            "Confirmation didn't match: type the database's exact name to delete it.",
        )
        return redirect("cpanel:database_detail", pk=database.pk)

    force = request.POST.get("force") == "on"
    try:
        DatabaseService.deprovision(database)
    except DatabaseProvisionError as e:
        if not force:
            messages.error(
                request,
                f"Server cleanup failed, database kept: {e} — fix the data "
                'server and retry, or tick "delete anyway" to remove only '
                "PyRun's record.",
            )
            return redirect("cpanel:database_detail", pk=database.pk)
        messages.warning(
            request,
            f"Server cleanup failed ({e}); the schema/role may still exist "
            "on the data server.",
        )

    name = database.name
    database.delete()
    messages.success(request, f'Database "{name}" deleted.')
    return redirect("cpanel:database_list")
