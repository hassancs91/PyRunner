"""
Log viewer views for the control panel.
"""
import logging
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.services.log_service import LogService

logger = logging.getLogger(__name__)


@login_required
def logs_view(request: HttpRequest) -> HttpResponse:
    """Display logs with filtering options."""
    # Get filter parameters
    level_filter = request.GET.get("level")
    search_query = request.GET.get("search", "").strip()
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    page = int(request.GET.get("page", 1))
    per_page = 100

    # Parse dates
    start_date = None
    end_date = None
    if date_from:
        try:
            start_date = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            end_date = datetime.fromisoformat(date_to)
        except ValueError:
            pass

    # Calculate offset
    offset = (page - 1) * per_page

    # Get filtered logs
    entries, total_count = LogService.read_logs(
        level_filter=level_filter if level_filter in LogService.LOG_LEVELS else None,
        search_query=search_query if search_query else None,
        start_date=start_date,
        end_date=end_date,
        limit=per_page,
        offset=offset,
    )

    # Calculate pagination
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1

    # Get log file stats
    log_files = LogService.get_log_files()
    total_size = LogService.get_log_file_size()

    return render(
        request,
        "cpanel/logs.html",
        {
            "entries": entries,
            "total_count": total_count,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "level_filter": level_filter,
            "search_query": search_query,
            "date_from": date_from,
            "date_to": date_to,
            "log_levels": LogService.LOG_LEVELS,
            "log_files_count": len(log_files),
            "total_size": total_size,
        },
    )


@login_required
def logs_api_view(request: HttpRequest) -> JsonResponse:
    """API endpoint for real-time log updates."""
    lines = int(request.GET.get("lines", 50))
    lines = min(lines, 200)  # Cap at 200 for performance

    entries = LogService.tail_logs(lines=lines)

    return JsonResponse(
        {
            "success": True,
            "entries": [
                {
                    "timestamp": entry.timestamp.isoformat(),
                    "level": entry.level,
                    "logger": entry.logger,
                    "message": entry.message,
                    "exception": entry.exception,
                }
                for entry in entries
            ],
        }
    )


@login_required
@require_POST
def logs_clear_view(request: HttpRequest) -> JsonResponse:
    """Clear all log files (superuser only)."""
    if not request.user.is_superuser:
        return JsonResponse(
            {
                "success": False,
                "error": "Permission denied. Only administrators can clear logs.",
            },
            status=403,
        )

    try:
        bytes_freed = LogService.clear_logs()
        logger.info(f"Logs cleared by {request.user.email}, freed {bytes_freed} bytes")
        return JsonResponse(
            {
                "success": True,
                "message": f"Logs cleared. Freed {bytes_freed:,} bytes.",
            }
        )
    except Exception as e:
        logger.exception("Failed to clear logs")
        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            }
        )
