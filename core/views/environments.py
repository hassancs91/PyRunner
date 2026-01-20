"""
Environment views for the control panel.
"""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from core.models import Environment, PackageOperation
from core.forms import (
    EnvironmentCreateForm,
    EnvironmentEditForm,
    PackageInstallForm,
    BulkInstallForm,
)
from core.services import EnvironmentService


@login_required
def environment_list_view(request: HttpRequest) -> HttpResponse:
    """List all environments."""
    environments = Environment.objects.all().order_by("-is_default", "name")

    return render(
        request,
        "cpanel/environments/list.html",
        {
            "environments": environments,
        },
    )


@login_required
def environment_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View environment details and associated scripts."""
    environment = get_object_or_404(Environment, pk=pk)
    scripts = environment.scripts.select_related("created_by").order_by("-updated_at")

    # Calculate disk usage
    disk_usage_bytes = EnvironmentService.get_disk_usage(environment)
    disk_usage = EnvironmentService.format_disk_usage(disk_usage_bytes)

    return render(
        request,
        "cpanel/environments/detail.html",
        {
            "environment": environment,
            "scripts": scripts,
            "disk_usage": disk_usage,
        },
    )


@login_required
def environment_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new environment."""
    if request.method == "POST":
        form = EnvironmentCreateForm(request.POST)
        if form.is_valid():
            python_path = form.cleaned_data["python_path"]
            name = form.cleaned_data["name"]
            env_path = form.get_generated_path()

            # Create the virtual environment
            success, message = EnvironmentService.create_environment(
                python_path=python_path,
                env_path=env_path,
            )

            if success:
                # Get Python version from the new environment
                env = form.save(commit=False)
                env.path = env_path
                env.created_by = request.user

                # Get version from the created environment
                version = EnvironmentService._get_python_version(
                    env.get_python_executable()
                )
                env.python_version = version or ""

                # Get initial requirements
                env.save()  # Save first to get the ID
                env.requirements = EnvironmentService.pip_freeze(env)
                env.save(update_fields=["requirements"])

                messages.success(request, f'Environment "{name}" created successfully.')
                return redirect("cpanel:environment_detail", pk=env.pk)
            else:
                messages.error(request, f"Failed to create environment: {message}")
    else:
        form = EnvironmentCreateForm()

    return render(
        request,
        "cpanel/environments/create.html",
        {
            "form": form,
        },
    )


@login_required
def environment_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit environment details (name/description only)."""
    environment = get_object_or_404(Environment, pk=pk)

    if request.method == "POST":
        form = EnvironmentEditForm(request.POST, instance=environment)
        if form.is_valid():
            form.save()
            messages.success(
                request, f'Environment "{environment.name}" updated successfully.'
            )
            return redirect("cpanel:environment_detail", pk=pk)
    else:
        form = EnvironmentEditForm(instance=environment)

    return render(
        request,
        "cpanel/environments/edit.html",
        {
            "form": form,
            "environment": environment,
        },
    )


@login_required
@require_POST
def environment_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete an environment."""
    environment = get_object_or_404(Environment, pk=pk)

    if not environment.can_delete:
        if environment.is_default:
            messages.error(request, "Cannot delete the default environment.")
        else:
            messages.error(
                request,
                f"Cannot delete: {environment.script_count} script(s) are using this environment.",
            )
        return redirect("cpanel:environment_detail", pk=pk)

    # Delete the venv folder
    success, message = EnvironmentService.delete_environment(environment)

    if success:
        name = environment.name
        environment.delete()
        messages.success(request, f'Environment "{name}" deleted successfully.')
        return redirect("cpanel:environment_list")
    else:
        messages.error(request, f"Failed to delete environment: {message}")
        return redirect("cpanel:environment_detail", pk=pk)


@login_required
@require_POST
def environment_set_default_view(request: HttpRequest, pk) -> HttpResponse:
    """Set an environment as the default."""
    environment = get_object_or_404(Environment, pk=pk)

    environment.is_default = True
    environment.save()  # Model's save() handles unsetting other defaults

    messages.success(request, f'"{environment.name}" is now the default environment.')
    return redirect("cpanel:environment_detail", pk=pk)


# Package Management Views


@login_required
def environment_packages_view(request: HttpRequest, pk) -> HttpResponse:
    """View and manage packages in an environment."""
    environment = get_object_or_404(Environment, pk=pk)

    # Get installed packages
    packages = EnvironmentService.get_installed_packages(environment)

    # Sort and filter
    sort_by = request.GET.get("sort", "name")
    search = request.GET.get("search", "").lower().strip()

    if search:
        packages = [p for p in packages if search in p["name"].lower()]

    if sort_by == "version":
        packages = sorted(packages, key=lambda p: p["version"])
    else:
        packages = sorted(packages, key=lambda p: p["name"].lower())

    # Recent operations
    operations = environment.package_operations.all()[:10]

    # Check for any running operations
    has_running_operation = environment.package_operations.filter(
        status__in=[PackageOperation.Status.PENDING, PackageOperation.Status.RUNNING]
    ).exists()

    return render(
        request,
        "cpanel/environments/packages.html",
        {
            "environment": environment,
            "packages": packages,
            "package_count": len(packages),
            "operations": operations,
            "has_running_operation": has_running_operation,
            "sort_by": sort_by,
            "search": search,
            "install_form": PackageInstallForm(),
            "bulk_form": BulkInstallForm(),
        },
    )


@login_required
@require_POST
def package_install_view(request: HttpRequest, pk) -> HttpResponse:
    """Install a package (async via django-q2)."""
    environment = get_object_or_404(Environment, pk=pk)
    form = PackageInstallForm(request.POST)

    if form.is_valid():
        package_spec = form.cleaned_data["package_spec"]

        # Create operation record
        operation = PackageOperation.objects.create(
            environment=environment,
            operation=PackageOperation.Operation.INSTALL,
            package_spec=package_spec,
            created_by=request.user,
        )

        # Queue async task
        task_id = async_task(
            "core.tasks.execute_package_operation",
            str(operation.id),
            task_name=f"pkg-install-{operation.id}",
        )

        operation.task_id = task_id
        operation.save(update_fields=["task_id"])

        messages.info(request, f'Installing "{package_spec}"...')
    else:
        for error in form.errors.values():
            messages.error(request, error[0])

    return redirect("cpanel:environment_packages", pk=pk)


@login_required
@require_POST
def package_uninstall_view(request: HttpRequest, pk) -> HttpResponse:
    """Uninstall a package (async via django-q2)."""
    environment = get_object_or_404(Environment, pk=pk)
    package_name = request.POST.get("package_name", "").strip()

    if not package_name:
        messages.error(request, "Package name is required.")
        return redirect("cpanel:environment_packages", pk=pk)

    if not EnvironmentService.validate_package_spec(package_name):
        messages.error(request, "Invalid package name.")
        return redirect("cpanel:environment_packages", pk=pk)

    # Create operation record
    operation = PackageOperation.objects.create(
        environment=environment,
        operation=PackageOperation.Operation.UNINSTALL,
        package_spec=package_name,
        created_by=request.user,
    )

    # Queue async task
    task_id = async_task(
        "core.tasks.execute_package_operation",
        str(operation.id),
        task_name=f"pkg-uninstall-{operation.id}",
    )

    operation.task_id = task_id
    operation.save(update_fields=["task_id"])

    messages.info(request, f'Uninstalling "{package_name}"...')
    return redirect("cpanel:environment_packages", pk=pk)


@login_required
@require_POST
def bulk_install_view(request: HttpRequest, pk) -> HttpResponse:
    """Bulk install packages from requirements."""
    environment = get_object_or_404(Environment, pk=pk)
    form = BulkInstallForm(request.POST, request.FILES)

    if form.is_valid():
        requirements = form.cleaned_data["requirements"]

        # Count packages
        package_count = sum(
            1
            for line in requirements.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

        # Create operation record
        operation = PackageOperation.objects.create(
            environment=environment,
            operation=PackageOperation.Operation.BULK_INSTALL,
            package_spec=requirements,
            created_by=request.user,
        )

        # Queue async task
        task_id = async_task(
            "core.tasks.execute_package_operation",
            str(operation.id),
            task_name=f"pkg-bulk-{operation.id}",
        )

        operation.task_id = task_id
        operation.save(update_fields=["task_id"])

        messages.info(request, f"Installing {package_count} package(s)...")
    else:
        for error in form.errors.values():
            if isinstance(error, list):
                messages.error(request, error[0])
            else:
                messages.error(request, str(error))

    return redirect("cpanel:environment_packages", pk=pk)


@login_required
def export_requirements_view(request: HttpRequest, pk) -> HttpResponse:
    """Export requirements.txt file."""
    environment = get_object_or_404(Environment, pk=pk)

    requirements = EnvironmentService.pip_freeze(environment)

    response = HttpResponse(requirements, content_type="text/plain")
    response["Content-Disposition"] = (
        f'attachment; filename="{environment.name}-requirements.txt"'
    )
    return response


@login_required
def package_operation_status_view(request: HttpRequest, operation_id) -> JsonResponse:
    """AJAX endpoint for checking operation status."""
    operation = get_object_or_404(PackageOperation, pk=operation_id)

    return JsonResponse(
        {
            "id": str(operation.id),
            "status": operation.status,
            "output": operation.output,
            "error": operation.error,
            "completed": operation.is_finished,
            "success": operation.is_successful,
            "duration": operation.duration_display,
        }
    )
