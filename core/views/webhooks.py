"""
Webhook views for triggering scripts via HTTP.
"""

import json
import logging

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Script, Run
from core.tasks import queue_script_run

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_trigger_view(request: HttpRequest, token: str) -> JsonResponse:
    """
    Public endpoint to trigger script execution via webhook.

    Accepts GET and POST requests. For POST, the request body and query
    parameters are passed to the script as environment variables.

    Args:
        request: The HTTP request
        token: The webhook token (64-char URL-safe string)

    Returns:
        JsonResponse with:
        - 200: Script queued successfully
        - 403: Script is disabled
        - 404: Invalid token
        - 500: Failed to queue
    """
    # Find script by token
    try:
        script = Script.objects.select_related("environment").get(webhook_token=token)
    except Script.DoesNotExist:
        logger.warning(f"Webhook trigger with invalid token: {token[:8]}...")
        return JsonResponse(
            {"error": "Invalid webhook token"},
            status=404,
        )

    # Check if script is enabled
    if not script.is_enabled:
        logger.info(f"Webhook trigger rejected - script disabled: {script.name}")
        return JsonResponse(
            {"error": "Script is disabled"},
            status=403,
        )

    # Extract webhook data
    webhook_data = _extract_webhook_data(request)

    # Create a new Run record
    run = Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=None,  # Webhook-triggered, no user
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
    )

    # Store webhook data in the run for the executor
    # We'll pass this through a custom field or via task args
    run._webhook_data = webhook_data

    # Queue for async execution
    try:
        queue_script_run(run, webhook_data=webhook_data)
        logger.info(f"Webhook triggered run {run.id} for script {script.name}")

        return JsonResponse({
            "status": "queued",
            "run_id": str(run.id),
            "script": script.name,
        })

    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.save()
        logger.error(f"Webhook failed to queue run {run.id}: {e}")

        return JsonResponse(
            {"error": "Failed to queue script execution"},
            status=500,
        )


def _extract_webhook_data(request: HttpRequest) -> dict:
    """
    Extract webhook data from the request.

    Returns a dict with:
    - method: GET or POST
    - body: Request body as string (for POST)
    - query: Query parameters as dict
    - content_type: Request content type
    """
    data = {
        "method": request.method,
        "query": dict(request.GET),
        "content_type": request.content_type or "",
    }

    # Extract body for POST requests
    if request.method == "POST":
        try:
            body = request.body.decode("utf-8")
            data["body"] = body

            # Try to parse as JSON for convenience
            if request.content_type == "application/json":
                try:
                    data["body_json"] = json.loads(body)
                except json.JSONDecodeError:
                    pass
        except Exception:
            data["body"] = ""

    return data
