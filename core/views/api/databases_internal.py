"""
Internal Databases API (loopback-only) — credential resolution for runs.

The control plane of the Databases feature's hybrid access model: a run's
``pyrunner_db`` helper calls here (signed per-run token, same auth as the
internal datastore API) to resolve the scoped DSN of a database its script has
been granted, then connects to the data server DIRECTLY with that DSN. The
provisioner credential and the Fernet-decrypted role password never enter the
script's process environment — the DSN is handed out at connect time only.

Grants are EXPLICIT-ONLY (unlike secrets' injection_mode='all'): no active
``DatabaseGrant`` row for the run's script ⇒ 404, indistinguishable from a
database that doesn't exist (no cross-workspace or ungranted disclosure).

Contract (mirrored by the helper's exceptions):
- missing / not granted        -> 404 DATABASE_NOT_FOUND  (client: ValueError)
- provisioning failed / stuck  -> 409 DATABASE_NOT_READY  (client: PyRunnerDbError)
- no data server attached      -> 503 NOT_CONFIGURED      (client: PyRunnerDbError)
"""

import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Database, DatabaseGrant, Run
from core.views.api.decorators import internal_datastore_token_required

logger = logging.getLogger(__name__)


def _database_not_found(name: str) -> JsonResponse:
    return JsonResponse(
        {
            "error": {
                "code": "DATABASE_NOT_FOUND",
                "message": (
                    f"Database '{name}' does not exist or is not granted to "
                    "this script. Grant it on the database's page in PyRun."
                ),
            }
        },
        status=404,
    )


def _script_id_for_run(request: HttpRequest):
    """The run's script id, derived server-side from the signed token."""
    return (
        Run.objects.filter(id=request.datastore_run.get("run_id"))
        .values_list("script_id", flat=True)
        .first()
    )


@csrf_exempt
@require_http_methods(["GET"])
@internal_datastore_token_required
def list_databases(request: HttpRequest) -> JsonResponse:
    """GET /internal/databases — the databases granted to the run's script."""
    script_id = _script_id_for_run(request)
    grants = (
        DatabaseGrant.objects.filter(script_id=script_id, active=True)
        .select_related("database")
        .order_by("database__name")
        if script_id
        else []
    )
    return JsonResponse(
        {
            "databases": [
                {"name": g.database.name, "status": g.database.status}
                for g in grants
            ]
        }
    )


@csrf_exempt
@require_http_methods(["GET"])
@internal_datastore_token_required
def resolve_dsn(request: HttpRequest, name: str) -> JsonResponse:
    """GET /internal/databases/<name>/dsn — scoped DSN for a granted database."""
    from core.services import DatabaseService

    if not DatabaseService.is_configured():
        return JsonResponse(
            {
                "error": {
                    "code": "NOT_CONFIGURED",
                    "message": (
                        "No data server is attached to this PyRun instance "
                        "(PYRUNNER_DATA_DB_URL is not set)."
                    ),
                }
            },
            status=503,
        )

    try:
        database = Database.resolve_for_workspace(name, request.datastore_workspace)
    except Database.DoesNotExist:
        return _database_not_found(name)

    script_id = _script_id_for_run(request)
    granted = (
        script_id is not None
        and DatabaseGrant.objects.filter(
            script_id=script_id, database=database, active=True
        ).exists()
    )
    if not granted:
        # Same response as "missing": a script must not learn whether an
        # ungranted database exists.
        return _database_not_found(name)

    if not database.is_ready:
        return JsonResponse(
            {
                "error": {
                    "code": "DATABASE_NOT_READY",
                    "message": (
                        f"Database '{name}' is not ready (status: "
                        f"{database.status}). Check its page in PyRun."
                    ),
                }
            },
            status=409,
        )

    return JsonResponse(
        {
            "name": database.name,
            "dsn": DatabaseService.scoped_dsn(database),
            "schema": database.schema_name,
        }
    )
