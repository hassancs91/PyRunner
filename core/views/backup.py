"""
Views for backup and restore functionality.
"""
import json
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from core.forms import BackupCreateForm, BackupRestoreForm
from core.services.backup_service import BackupService


@login_required
def backup_create_view(request):
    """
    Create and download a backup file.
    POST request with options.
    """
    if request.method == "POST":
        form = BackupCreateForm(request.POST)
        if form.is_valid():
            try:
                # Create backup
                backup_data = BackupService.create_backup(
                    include_runs=form.cleaned_data.get("include_runs", True),
                    max_runs=form.cleaned_data.get("max_runs", 1000),
                    include_package_operations=form.cleaned_data.get("include_package_operations", False),
                    created_by_user=request.user,
                )

                # Generate filename with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"pyrunner_backup_{timestamp}.json"

                # Create JSON response
                response = HttpResponse(
                    json.dumps(backup_data, indent=2),
                    content_type="application/json",
                )
                response["Content-Disposition"] = f'attachment; filename="{filename}"'

                return response

            except Exception as e:
                messages.error(request, f"Failed to create backup: {str(e)}")
                return redirect("cpanel:settings")

    # GET request - redirect to settings
    return redirect("cpanel:settings")


@login_required
@require_POST
def backup_upload_view(request):
    """
    Upload backup file and return validation results.
    AJAX endpoint for file upload.
    """
    try:
        backup_file = request.FILES.get("backup_file")
        if not backup_file:
            return JsonResponse({
                "success": False,
                "error": "No file uploaded",
            })

        # Check file size (100MB limit)
        max_size = BackupService.MAX_BACKUP_SIZE_MB * 1024 * 1024
        if backup_file.size > max_size:
            return JsonResponse({
                "success": False,
                "error": f"File too large (max {BackupService.MAX_BACKUP_SIZE_MB}MB)",
            })

        # Parse JSON
        try:
            backup_data = json.load(backup_file)
        except json.JSONDecodeError as e:
            return JsonResponse({
                "success": False,
                "error": f"Invalid JSON file: {str(e)}",
            })

        # Validate backup
        validation_result = BackupService.validate_backup(backup_data)

        if not validation_result["valid"]:
            return JsonResponse({
                "success": False,
                "error": "Backup validation failed",
                "errors": validation_result["errors"],
                "warnings": validation_result["warnings"],
            })

        # Store backup data in session for restore
        request.session["pending_backup"] = backup_data

        # Get preview
        preview = BackupService.get_backup_preview(backup_data)

        return JsonResponse({
            "success": True,
            "preview": preview,
            "warnings": validation_result["warnings"],
        })

    except Exception as e:
        return JsonResponse({
            "success": False,
            "error": f"Upload failed: {str(e)}",
        })


@login_required
def backup_preview_view(request):
    """
    Get preview of uploaded backup for confirmation.
    AJAX endpoint.
    """
    backup_data = request.session.get("pending_backup")
    if not backup_data:
        return JsonResponse({
            "success": False,
            "error": "No backup data found. Please upload a file first.",
        })

    try:
        preview = BackupService.get_backup_preview(backup_data)
        return JsonResponse({
            "success": True,
            "preview": preview,
        })
    except Exception as e:
        return JsonResponse({
            "success": False,
            "error": f"Preview failed: {str(e)}",
        })


@login_required
@require_POST
def backup_restore_view(request):
    """
    Execute restore operation.
    Requires confirmation from preview step.
    """
    # Get backup data from session
    backup_data = request.session.get("pending_backup")
    if not backup_data:
        messages.error(request, "No backup data found. Please upload a file first.")
        return redirect("cpanel:settings")

    # Validate form (confirmation checkbox)
    restore_runs = request.POST.get("restore_runs") == "on"
    confirm_delete = request.POST.get("confirm_delete") == "on"

    if not confirm_delete:
        messages.error(request, "You must confirm data deletion to proceed with restore.")
        return redirect("cpanel:settings")

    try:
        # Create automatic backup before restore
        try:
            auto_backup_data = BackupService.create_backup(
                include_runs=True,
                max_runs=1000,
                include_package_operations=False,
                created_by_user=request.user,
            )

            # Save automatic backup to file
            import os
            from django.conf import settings

            backup_dir = os.path.join(settings.BASE_DIR, "data", "backups", "auto")
            os.makedirs(backup_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            auto_backup_path = os.path.join(backup_dir, f"auto_backup_{timestamp}.json")

            with open(auto_backup_path, "w") as f:
                json.dump(auto_backup_data, f, indent=2)

            messages.info(request, f"Automatic backup created: auto_backup_{timestamp}.json")

        except Exception as e:
            messages.warning(request, f"Failed to create automatic backup: {str(e)}")

        # Perform restore
        result = BackupService.restore_backup(
            backup_data=backup_data,
            restore_runs=restore_runs,
            current_user=request.user,
        )

        if result["success"]:
            # Clear session data
            if "pending_backup" in request.session:
                del request.session["pending_backup"]

            counts = result["counts"]
            messages.success(
                request,
                f"Backup restored successfully! Imported: {counts.get('scripts', 0)} scripts, "
                f"{counts.get('environments', 0)} environments, {counts.get('secrets', 0)} secrets, "
                f"{counts.get('runs', 0)} runs. "
                f"Next: Recreate virtual environments from the Environments page."
            )
        else:
            error_msg = "; ".join(result["errors"])
            messages.error(request, f"Restore failed: {error_msg}")

    except Exception as e:
        messages.error(request, f"Restore failed: {str(e)}")

    return redirect("cpanel:dashboard")
