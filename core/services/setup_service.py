"""
Service for handling first-run setup operations.
"""

import logging
import os
import subprocess
import sys
from io import StringIO

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class SetupService:
    """
    Handles initial setup operations including migrations,
    default environment creation, and setup completion tracking.
    """

    @classmethod
    def is_setup_needed(cls) -> bool:
        """
        Check if initial setup is required.

        Returns True if:
        - GlobalSettings.setup_completed is False, OR
        - No default Environment exists
        """
        try:
            from core.models import GlobalSettings, Environment

            # Check setup_completed flag
            settings_obj = GlobalSettings.get_settings()
            if not settings_obj.setup_completed:
                return True

            # Check if default environment exists
            if not Environment.objects.filter(is_default=True).exists():
                return True

            return False
        except Exception as e:
            # Tables might not exist yet (before migrations)
            logger.debug(f"Setup check failed: {e}")
            return True

    @classmethod
    def get_status(cls) -> dict:
        """
        Get detailed status of setup components.

        Returns dict with status of each component.
        """
        status = {
            "database_ready": False,
            "migrations_pending": True,
            "default_env_exists": False,
            "setup_completed": False,
            "errors": [],
        }

        try:
            # Check if database tables exist
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='global_settings'"
                )
                status["database_ready"] = cursor.fetchone() is not None
        except Exception as e:
            status["errors"].append(f"Database check failed: {e}")

        # Check pending migrations
        try:
            from django.db.migrations.executor import MigrationExecutor
            from django.db import connection
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            status["migrations_pending"] = len(plan) > 0
        except Exception as e:
            status["errors"].append(f"Migration check failed: {e}")

        # Check default environment
        try:
            from core.models import Environment
            status["default_env_exists"] = Environment.objects.filter(
                is_default=True
            ).exists()
        except Exception as e:
            status["errors"].append(f"Environment check failed: {e}")

        # Check setup_completed flag
        try:
            from core.models import GlobalSettings
            settings_obj = GlobalSettings.get_settings()
            status["setup_completed"] = settings_obj.setup_completed
        except Exception as e:
            status["errors"].append(f"Settings check failed: {e}")

        return status

    @classmethod
    def run_migrations(cls) -> tuple[bool, str]:
        """
        Run pending database migrations programmatically.

        Returns:
            Tuple of (success: bool, output: str)
        """
        try:
            from django.core.management import call_command

            output = StringIO()
            call_command("migrate", verbosity=1, stdout=output, stderr=output)

            result = output.getvalue()
            logger.info("Migrations completed successfully")
            return True, result
        except Exception as e:
            error_msg = f"Migration failed: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    @classmethod
    def create_default_environment(cls) -> tuple[bool, str]:
        """
        Create the default Python virtual environment.

        Reuses logic from setup_default_env management command.

        Returns:
            Tuple of (success: bool, message: str)
        """
        from core.models import Environment

        # Check if default environment already exists
        existing = Environment.objects.filter(is_default=True).first()
        if existing:
            # Check if directory exists
            if existing.exists():
                return True, f"Default environment already exists: {existing.name}"
            else:
                # Directory missing, need to recreate
                logger.warning(
                    f"Default environment record exists but directory missing, recreating..."
                )

        # Define paths
        env_path = "default"
        full_path = os.path.join(settings.ENVIRONMENTS_ROOT, env_path)

        # Check if directory already exists (orphaned)
        if os.path.exists(full_path):
            logger.info(f"Found existing venv at {full_path}, will use it")
        else:
            # Create the virtual environment
            logger.info(f"Creating virtual environment at {full_path}...")
            try:
                creationflags = (
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                )
                result = subprocess.run(
                    [sys.executable, "-m", "venv", full_path],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    creationflags=creationflags,
                )
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to create virtual environment: {e.stderr}"
                logger.error(error_msg)
                return False, error_msg
            except subprocess.TimeoutExpired:
                return False, "Timeout creating virtual environment"
            except Exception as e:
                return False, f"Error creating virtual environment: {str(e)}"

        # Get Python version from the new venv
        if os.name == "nt":
            python_path = os.path.join(full_path, "Scripts", "python.exe")
        else:
            python_path = os.path.join(full_path, "bin", "python")

        python_version = "unknown"
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                [python_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
            )
            python_version = result.stdout.strip().replace("Python ", "")
        except Exception as e:
            logger.warning(f"Could not get Python version: {e}")

        # Create or update the Environment record
        if existing:
            existing.path = env_path
            existing.python_version = python_version
            existing.save()
            msg = f"Updated default environment (Python {python_version})"
        else:
            Environment.objects.create(
                name="Default Environment",
                description="Auto-created default Python environment",
                path=env_path,
                python_version=python_version,
                is_default=True,
                is_active=True,
            )
            msg = f"Created default environment (Python {python_version})"

        logger.info(msg)
        return True, msg

    @classmethod
    def needs_admin_setup(cls) -> bool:
        """Check if admin user needs to be created."""
        try:
            from core.models import User
            return not User.objects.filter(is_superuser=True).exists()
        except Exception:
            return False

    @classmethod
    def create_admin_user(cls, email: str, password: str) -> tuple[bool, str]:
        """
        Create the initial admin user with password.

        Args:
            email: Admin email address
            password: Admin password

        Returns:
            Tuple of (success: bool, message: str)
        """
        from core.models import User, GlobalSettings

        try:
            # Check if admin already exists
            if User.objects.filter(is_superuser=True).exists():
                return False, "An admin user already exists."

            # Create the admin user with password
            user = User.objects.create_user(
                email=email,
                username=email,
                is_staff=True,
                is_superuser=True,
                is_verified=True,
            )
            user.set_password(password)
            user.save()

            # Disable open registration after first user
            settings_obj = GlobalSettings.get_settings()
            settings_obj.allow_registration = False
            settings_obj.save(update_fields=["allow_registration"])

            logger.info(f"Admin user created: {email}")
            return True, f"Admin user {email} created successfully."

        except Exception as e:
            error_msg = f"Failed to create admin user: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    @classmethod
    def complete_setup(cls) -> None:
        """Mark initial setup as completed."""
        from core.models import GlobalSettings

        settings_obj = GlobalSettings.get_settings()
        settings_obj.setup_completed = True
        settings_obj.setup_completed_at = timezone.now()
        settings_obj.save()

        logger.info("Setup marked as completed")

    @classmethod
    def run_full_setup(cls) -> dict:
        """
        Run the complete setup process.

        Returns dict with results of each step.
        """
        results = {
            "migrations": {"success": False, "message": ""},
            "default_env": {"success": False, "message": ""},
            "completed": False,
        }

        # Run migrations
        success, message = cls.run_migrations()
        results["migrations"] = {"success": success, "message": message}

        if not success:
            return results

        # Create default environment
        success, message = cls.create_default_environment()
        results["default_env"] = {"success": success, "message": message}

        if not success:
            return results

        # Mark setup as completed
        cls.complete_setup()
        results["completed"] = True

        return results
