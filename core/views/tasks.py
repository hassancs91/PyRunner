"""
Views for task management in cpanel.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.services.task_service import TaskService


@login_required
def tasks_view(request: HttpRequest) -> HttpResponse:
    """
    Main tasks management page.
    Display all tasks with filtering by status.
    """
    # Get filter parameters
    status_filter = request.GET.get("status", "")
    page = int(request.GET.get("page", 1))
    per_page = 50

    # Get task statistics
    stats = TaskService.get_task_statistics()

    # Get stuck tasks
    stuck_tasks = TaskService.get_stuck_tasks()

    # Get queued tasks
    queued_tasks = TaskService.get_queued_tasks()

    # Get completed tasks with filtering
    completed_status = None
    if status_filter == "success":
        completed_status = "success"
    elif status_filter == "failed":
        completed_status = "failed"

    offset = (page - 1) * per_page
    completed_tasks, total_completed = TaskService.get_completed_tasks(
        status_filter=completed_status,
        limit=per_page,
        offset=offset,
    )

    # Calculate pagination
    total_pages = (total_completed + per_page - 1) // per_page if total_completed > 0 else 1

    context = {
        "stats": stats,
        "stuck_tasks": stuck_tasks,
        "queued_tasks": queued_tasks,
        "completed_tasks": completed_tasks,
        "status_filter": status_filter,
        "page": page,
        "total_pages": total_pages,
        "total_completed": total_completed,
    }

    return render(request, "cpanel/tasks.html", context)


@login_required
def tasks_api_view(request: HttpRequest) -> JsonResponse:
    """
    API endpoint for real-time task updates (AJAX polling).
    Returns current task state for auto-refresh feature.
    """
    stats = TaskService.get_task_statistics()
    queued_tasks = TaskService.get_queued_tasks()
    stuck_tasks = TaskService.get_stuck_tasks()

    # Serialize queued tasks
    queued_data = []
    for task in queued_tasks:
        queued_data.append({
            "id": task["id"],
            "name": task["name"],
            "type": task["type"],
            "queued_at": task["queued_at"].isoformat() if task["queued_at"] else None,
            "linked_run_id": str(task["linked_run"].id) if task["linked_run"] else None,
            "linked_run_script": task["linked_run"].script.name if task["linked_run"] else None,
        })

    # Serialize stuck tasks
    stuck_data = []
    for task in stuck_tasks:
        stuck_data.append({
            "id": task["id"],
            "type": task["type"],
            "stuck_minutes": task["stuck_minutes"],
            "linked_run_id": str(task["linked_run"].id) if task["linked_run"] else None,
        })

    return JsonResponse({
        "stats": stats,
        "queued_tasks": queued_data,
        "stuck_count": len(stuck_tasks),
        "stuck_tasks": stuck_data,
    })


@login_required
@require_POST
def task_cancel_view(request: HttpRequest, task_id: str) -> JsonResponse:
    """
    Cancel a queued task.
    """
    success, message = TaskService.cancel_queued_task(task_id)

    if success:
        return JsonResponse({"success": True, "message": message})
    else:
        return JsonResponse({"success": False, "error": message}, status=400)


@login_required
@require_POST
def task_force_stop_view(request: HttpRequest, task_id: str) -> JsonResponse:
    """
    Force stop a running task.
    """
    success, message = TaskService.force_stop_task(task_id)

    if success:
        return JsonResponse({"success": True, "message": message})
    else:
        return JsonResponse({"success": False, "error": message}, status=400)
