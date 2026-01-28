"""
Environment model for isolated Python virtual environments.
"""

import os
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def validate_environment_path(value: str) -> None:
    """
    Validate environment path to prevent path traversal attacks.

    Only allows simple directory names: alphanumeric, hyphens, underscores.
    Blocks: .., absolute paths, drive letters, special characters.
    """
    if not value:
        raise ValidationError("Path cannot be empty")

    # Block path traversal sequences
    if ".." in value:
        raise ValidationError("Path cannot contain '..'")

    # Block absolute paths (Unix and Windows)
    if value.startswith("/") or value.startswith("\\"):
        raise ValidationError("Path cannot be absolute")

    # Block Windows drive letters (e.g., C:, D:)
    if len(value) >= 2 and value[1] == ":":
        raise ValidationError("Path cannot contain drive letters")

    # Only allow safe characters: alphanumeric, hyphen, underscore
    if not re.match(r"^[a-zA-Z0-9_-]+$", value):
        raise ValidationError(
            "Path can only contain letters, numbers, hyphens, and underscores"
        )

    # Length limit
    if len(value) > 100:
        raise ValidationError("Path cannot exceed 100 characters")


class Environment(models.Model):
    """
    Represents an isolated Python virtual environment for script execution.
    Each environment has its own set of installed packages.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    # Path relative to ENVIRONMENTS_ROOT
    path = models.CharField(
        max_length=255,
        unique=True,
        validators=[validate_environment_path],
        help_text="Directory name within the environments folder",
    )

    # Python version info (captured at creation time)
    python_version = models.CharField(max_length=20, blank=True)

    # Installed packages (pip freeze output)
    requirements = models.TextField(
        blank=True,
        help_text="Installed packages in pip requirements format",
    )

    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default environment for new scripts",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this environment is available for use",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_environments",
    )

    class Meta:
        db_table = "environments"
        verbose_name = "environment"
        verbose_name_plural = "environments"
        ordering = ["-is_default", "name"]

    def __str__(self):
        suffix = " (default)" if self.is_default else ""
        return f"{self.name}{suffix}"

    def save(self, *args, **kwargs):
        # Ensure only one environment is marked as default
        if self.is_default:
            Environment.objects.filter(is_default=True).exclude(pk=self.pk).update(
                is_default=False
            )
        super().save(*args, **kwargs)

    def get_full_path(self) -> str:
        """Return the absolute path to this environment's directory."""
        # Runtime validation as defense-in-depth
        validate_environment_path(self.path)
        return os.path.join(settings.ENVIRONMENTS_ROOT, self.path)

    def get_python_executable(self) -> str:
        """Return the absolute path to this environment's Python executable."""
        base_path = self.get_full_path()
        if os.name == "nt":
            # Windows
            return os.path.join(base_path, "Scripts", "python.exe")
        else:
            # Unix/Linux/macOS
            return os.path.join(base_path, "bin", "python")

    def get_pip_executable(self) -> str:
        """Return the absolute path to this environment's pip executable."""
        base_path = self.get_full_path()
        if os.name == "nt":
            return os.path.join(base_path, "Scripts", "pip.exe")
        else:
            return os.path.join(base_path, "bin", "pip")

    def exists(self) -> bool:
        """Check if the environment directory exists on disk."""
        return os.path.isdir(self.get_full_path())

    def python_exists(self) -> bool:
        """Check if the Python executable exists."""
        return os.path.isfile(self.get_python_executable())

    @property
    def script_count(self) -> int:
        """Return the number of scripts using this environment."""
        return self.scripts.count()

    @property
    def can_delete(self) -> bool:
        """Check if this environment can be deleted (no scripts assigned)."""
        return self.script_count == 0 and not self.is_default
