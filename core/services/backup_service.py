"""
Service for creating and restoring PyRunner backups.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    PackageOperation,
    Run,
    ScheduleHistory,
    Script,
    ScriptSchedule,
    Secret,
    User,
)
from core.services.encryption_service import EncryptionService
from core.services.schedule_service import ScheduleService
from pyrunner.version import __version__ as PYRUNNER_VERSION


class BackupService:
    """
    Service for creating and restoring PyRunner backups.
    """

    BACKUP_VERSION = "1.0.0"
    MAX_BACKUP_SIZE_MB = 100

    @classmethod
    def create_backup(
        cls,
        include_runs: bool = True,
        max_runs: int = 1000,
        include_package_operations: bool = False,
        created_by_user=None,
    ) -> dict:
        """
        Create a complete backup of the PyRunner instance.

        Args:
            include_runs: Include run history in backup
            max_runs: Maximum number of runs to include (0 = all, default 1000)
            include_package_operations: Include package operation history
            created_by_user: User creating the backup (for metadata)

        Returns:
            dict: Backup data structure (ready to serialize to JSON)
        """
        backup_data = {
            "backup_metadata": {
                "version": cls.BACKUP_VERSION,
                "pyrunner_version": PYRUNNER_VERSION,
                "created_at": cls._serialize_datetime(timezone.now()),
                "instance_name": GlobalSettings.get_settings().instance_name,
                "encryption_key_hash": cls._calculate_encryption_key_hash(),
                "include_runs": include_runs,
                "include_run_history_count": max_runs if include_runs else 0,
                "created_by_email": created_by_user.email if created_by_user else None,
            },
            "global_settings": cls._export_global_settings(),
            "environments": cls._export_environments(),
            "users": cls._export_users(),
            "scripts": cls._export_scripts(),
            "script_schedules": cls._export_schedules(),
            "schedule_history": cls._export_schedule_history(),
            "secrets": cls._export_secrets(),
        }

        if include_runs:
            backup_data["runs"] = cls._export_runs(max_runs)
        else:
            backup_data["runs"] = []

        if include_package_operations:
            backup_data["package_operations"] = cls._export_package_operations()
        else:
            backup_data["package_operations"] = []

        return backup_data

    @classmethod
    def _export_global_settings(cls) -> dict:
        """Export GlobalSettings to dict."""
        settings_obj = GlobalSettings.get_settings()
        return {
            "instance_name": settings_obj.instance_name,
            "timezone": settings_obj.timezone,
            "date_format": settings_obj.date_format,
            "time_format": settings_obj.time_format,
            "email_backend": settings_obj.email_backend,
            "smtp_host": settings_obj.smtp_host,
            "smtp_port": settings_obj.smtp_port,
            "smtp_username": settings_obj.smtp_username,
            "smtp_password_encrypted": settings_obj.smtp_password_encrypted,
            "smtp_use_tls": settings_obj.smtp_use_tls,
            "smtp_from_email": settings_obj.smtp_from_email,
            "resend_api_key_encrypted": settings_obj.resend_api_key_encrypted,
            "resend_from_email": settings_obj.resend_from_email,
            "default_notification_email": settings_obj.default_notification_email,
            "retention_days": settings_obj.retention_days,
            "retention_count": settings_obj.retention_count,
            "auto_cleanup_enabled": settings_obj.auto_cleanup_enabled,
            "schedules_paused": settings_obj.schedules_paused,
        }

    @classmethod
    def _export_environments(cls) -> List[dict]:
        """Export all environments to list of dicts."""
        environments = []
        for env in Environment.objects.all().order_by("created_at"):
            environments.append({
                "id": str(env.id),
                "name": env.name,
                "description": env.description,
                "path": env.path,
                "python_version": env.python_version,
                "requirements": env.requirements,
                "is_default": env.is_default,
                "is_active": env.is_active,
                "created_at": cls._serialize_datetime(env.created_at),
                "updated_at": cls._serialize_datetime(env.updated_at),
                "created_by_email": env.created_by.email if env.created_by else None,
            })
        return environments

    @classmethod
    def _export_users(cls) -> List[dict]:
        """Export users (basic info only, no passwords)."""
        users = []
        for user in User.objects.all().order_by("date_joined"):
            users.append({
                "id": user.id,
                "email": user.email,
                "is_verified": user.is_verified,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                "date_joined": cls._serialize_datetime(user.date_joined),
            })
        return users

    @classmethod
    def _export_scripts(cls) -> List[dict]:
        """Export scripts with all fields."""
        scripts = []
        for script in Script.objects.select_related("environment", "created_by").all().order_by("created_at"):
            scripts.append({
                "id": str(script.id),
                "name": script.name,
                "description": script.description,
                "code": script.code,
                "environment_id": str(script.environment.id),
                "timeout_seconds": script.timeout_seconds,
                "is_enabled": script.is_enabled,
                "webhook_token": script.webhook_token,
                "notify_on": script.notify_on,
                "notify_email": script.notify_email,
                "notify_webhook_url": script.notify_webhook_url,
                "notify_webhook_enabled": script.notify_webhook_enabled,
                "retention_days_override": script.retention_days_override,
                "retention_count_override": script.retention_count_override,
                "created_at": cls._serialize_datetime(script.created_at),
                "updated_at": cls._serialize_datetime(script.updated_at),
                "created_by_email": script.created_by.email if script.created_by else None,
            })
        return scripts

    @classmethod
    def _export_schedules(cls) -> List[dict]:
        """Export script schedules (without q_schedule_ids - will be regenerated)."""
        schedules = []
        for schedule in ScriptSchedule.objects.select_related("script", "created_by").all():
            schedules.append({
                "id": str(schedule.id),
                "script_id": str(schedule.script.id),
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
                "created_at": cls._serialize_datetime(schedule.created_at),
                "updated_at": cls._serialize_datetime(schedule.updated_at),
                "created_by_email": schedule.created_by.email if schedule.created_by else None,
            })
        return schedules

    @classmethod
    def _export_schedule_history(cls) -> List[dict]:
        """Export schedule history for audit trail."""
        history = []
        for item in ScheduleHistory.objects.select_related("schedule", "changed_by").all().order_by("created_at"):
            history.append({
                "id": str(item.id),
                "schedule_id": str(item.schedule.id),
                "change_type": item.change_type,
                "previous_config": item.previous_config,
                "new_config": item.new_config,
                "changed_by_email": item.changed_by.email if item.changed_by else None,
                "created_at": cls._serialize_datetime(item.created_at),
            })
        return history

    @classmethod
    def _export_secrets(cls) -> List[dict]:
        """Export secrets (keep encrypted values)."""
        secrets = []
        for secret in Secret.objects.select_related("created_by").all().order_by("created_at"):
            secrets.append({
                "id": str(secret.id),
                "key": secret.key,
                "encrypted_value": secret.encrypted_value,
                "description": secret.description,
                "created_at": cls._serialize_datetime(secret.created_at),
                "updated_at": cls._serialize_datetime(secret.updated_at),
                "created_by_email": secret.created_by.email if secret.created_by else None,
            })
        return secrets

    @classmethod
    def _export_runs(cls, max_count: int = 1000) -> List[dict]:
        """Export most recent runs."""
        runs = []
        queryset = Run.objects.select_related("script", "triggered_by").all().order_by("-created_at")
        if max_count > 0:
            queryset = queryset[:max_count]

        for run in queryset:
            runs.append({
                "id": str(run.id),
                "script_id": str(run.script.id),
                "status": run.status,
                "exit_code": run.exit_code,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "started_at": cls._serialize_datetime(run.started_at),
                "ended_at": cls._serialize_datetime(run.ended_at),
                "code_snapshot": run.code_snapshot,
                "trigger_type": run.trigger_type,
                "triggered_by_email": run.triggered_by.email if run.triggered_by else None,
                "created_at": cls._serialize_datetime(run.created_at),
            })
        return runs

    @classmethod
    def _export_package_operations(cls) -> List[dict]:
        """Export package operations."""
        operations = []
        for op in PackageOperation.objects.select_related("environment", "created_by").all().order_by("created_at"):
            operations.append({
                "id": str(op.id),
                "environment_id": str(op.environment.id),
                "operation": op.operation,
                "package_spec": op.package_spec,
                "status": op.status,
                "output": op.output,
                "error": op.error,
                "created_at": cls._serialize_datetime(op.created_at),
                "started_at": cls._serialize_datetime(op.started_at),
                "completed_at": cls._serialize_datetime(op.completed_at),
                "created_by_email": op.created_by.email if op.created_by else None,
            })
        return operations

    @classmethod
    def _calculate_encryption_key_hash(cls) -> str:
        """Calculate SHA256 hash of current ENCRYPTION_KEY."""
        if not EncryptionService.is_configured():
            return ""

        encryption_key = settings.ENCRYPTION_KEY
        return hashlib.sha256(encryption_key.encode()).hexdigest()

    @classmethod
    def _serialize_datetime(cls, dt: Optional[datetime]) -> Optional[str]:
        """Serialize datetime to ISO format string."""
        if dt is None:
            return None
        return dt.isoformat()

    @classmethod
    def _deserialize_datetime(cls, dt_str: Optional[str]) -> Optional[datetime]:
        """Deserialize ISO format string to datetime."""
        if not dt_str:
            return None
        return datetime.fromisoformat(dt_str)

    # =====================================================================
    # VALIDATION METHODS
    # =====================================================================

    @classmethod
    def validate_backup(cls, backup_data: dict) -> dict:
        """
        Validate backup structure and content.

        Returns:
            dict: {
                "valid": bool,
                "errors": list,
                "warnings": list,
                "metadata": dict
            }
        """
        errors = []
        warnings = []

        # Check required top-level keys
        required_keys = [
            "backup_metadata",
            "global_settings",
            "environments",
            "users",
            "scripts",
            "script_schedules",
            "schedule_history",
            "secrets",
            "runs",
        ]

        for key in required_keys:
            if key not in backup_data:
                errors.append(f"Missing required key: {key}")

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings, "metadata": {}}

        # Validate metadata
        metadata = backup_data.get("backup_metadata", {})
        if "version" not in metadata:
            errors.append("Missing backup version in metadata")
        elif metadata["version"] != cls.BACKUP_VERSION:
            warnings.append(
                f"Backup version {metadata['version']} differs from current version {cls.BACKUP_VERSION}"
            )

        # Validate encryption key
        if not cls.validate_encryption_key(backup_data):
            errors.append(
                "ENCRYPTION_KEY mismatch: Backup was created with a different encryption key. "
                "Encrypted secrets and credentials will be unreadable."
            )

        # Validate foreign key references
        env_ids = {env["id"] for env in backup_data.get("environments", [])}
        script_ids = {script["id"] for script in backup_data.get("scripts", [])}

        # Check script references to environments
        for script in backup_data.get("scripts", []):
            if script.get("environment_id") not in env_ids:
                errors.append(
                    f"Script '{script.get('name')}' references non-existent environment ID: {script.get('environment_id')}"
                )

        # Check schedule references to scripts
        for schedule in backup_data.get("script_schedules", []):
            if schedule.get("script_id") not in script_ids:
                errors.append(
                    f"Schedule references non-existent script ID: {schedule.get('script_id')}"
                )

        # Check run references to scripts
        for run in backup_data.get("runs", []):
            if run.get("script_id") not in script_ids:
                errors.append(
                    f"Run references non-existent script ID: {run.get('script_id')}"
                )

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "metadata": metadata,
        }

    @classmethod
    def validate_encryption_key(cls, backup_data: dict) -> bool:
        """
        Verify ENCRYPTION_KEY matches backup.

        Returns:
            bool: True if key matches or no encryption used
        """
        metadata = backup_data.get("backup_metadata", {})
        backup_key_hash = metadata.get("encryption_key_hash", "")

        if not backup_key_hash:
            # Backup has no encryption key hash (old format or no encryption)
            return True

        current_key_hash = cls._calculate_encryption_key_hash()
        return current_key_hash == backup_key_hash

    @classmethod
    def get_backup_preview(cls, backup_data: dict) -> dict:
        """
        Generate preview summary for user confirmation.

        Returns:
            dict: {
                "instance_name": str,
                "created_at": str,
                "created_by_email": str,
                "counts": {
                    "scripts": int,
                    "environments": int,
                    "secrets": int,
                    "runs": int,
                    ...
                },
                "warnings": list
            }
        """
        metadata = backup_data.get("backup_metadata", {})
        warnings = []

        # Check encryption key
        if not cls.validate_encryption_key(backup_data):
            warnings.append("ENCRYPTION_KEY mismatch - encrypted data may be unreadable")

        # Check version compatibility
        if metadata.get("version") != cls.BACKUP_VERSION:
            warnings.append(
                f"Backup version ({metadata.get('version')}) differs from current version ({cls.BACKUP_VERSION})"
            )

        counts = {
            "scripts": len(backup_data.get("scripts", [])),
            "environments": len(backup_data.get("environments", [])),
            "secrets": len(backup_data.get("secrets", [])),
            "runs": len(backup_data.get("runs", [])),
            "schedules": len(backup_data.get("script_schedules", [])),
            "users": len(backup_data.get("users", [])),
            "package_operations": len(backup_data.get("package_operations", [])),
        }

        return {
            "instance_name": metadata.get("instance_name", "Unknown"),
            "created_at": metadata.get("created_at", ""),
            "created_by_email": metadata.get("created_by_email", "Unknown"),
            "counts": counts,
            "warnings": warnings,
        }

    # =====================================================================
    # RESTORE METHODS
    # =====================================================================

    @classmethod
    @transaction.atomic
    def restore_backup(
        cls,
        backup_data: dict,
        restore_runs: bool = True,
        current_user=None,
    ) -> dict:
        """
        Restore backup data to database.

        Performs full replace of existing data.
        Wrapped in transaction for safety.

        Args:
            backup_data: Validated backup data dict
            restore_runs: Whether to restore run history
            current_user: User performing the restore (for ownership mapping)

        Returns:
            dict: {
                "success": bool,
                "counts": dict,
                "errors": list
            }
        """
        try:
            # Delete existing data (reverse dependency order)
            Run.objects.all().delete()
            PackageOperation.objects.all().delete()
            ScheduleHistory.objects.all().delete()
            ScriptSchedule.objects.all().delete()
            Script.objects.all().delete()
            Secret.objects.all().delete()
            Environment.objects.all().delete()
            # Note: We don't delete User objects to preserve authentication
            # GlobalSettings is singleton, so we update rather than delete

            # Import data in dependency order
            cls._import_global_settings(backup_data.get("global_settings", {}))

            user_map = cls._import_users(backup_data.get("users", []), current_user)
            env_map = cls._import_environments(backup_data.get("environments", []), user_map, current_user)
            cls._import_secrets(backup_data.get("secrets", []), user_map, current_user)
            script_map = cls._import_scripts(backup_data.get("scripts", []), env_map, user_map, current_user)
            cls._import_schedules(backup_data.get("script_schedules", []), script_map, user_map, current_user)
            cls._import_schedule_history(backup_data.get("schedule_history", []), user_map, current_user)

            if restore_runs:
                cls._import_runs(backup_data.get("runs", []), script_map, user_map, current_user)

            cls._import_package_operations(backup_data.get("package_operations", []), env_map, user_map, current_user)

            # Regenerate django-q2 schedules
            schedules_created = cls._regenerate_all_schedules()

            counts = {
                "environments": len(env_map),
                "scripts": len(script_map),
                "secrets": Secret.objects.count(),
                "runs": Run.objects.count() if restore_runs else 0,
                "schedules": ScriptSchedule.objects.count(),
                "schedules_regenerated": schedules_created,
            }

            return {"success": True, "counts": counts, "errors": []}

        except Exception as e:
            return {"success": False, "counts": {}, "errors": [str(e)]}

    @classmethod
    def _import_global_settings(cls, data: dict) -> None:
        """Import GlobalSettings."""
        settings_obj = GlobalSettings.get_settings()
        settings_obj.instance_name = data.get("instance_name", "PyRunner")
        settings_obj.timezone = data.get("timezone", "UTC")
        settings_obj.date_format = data.get("date_format", "YYYY-MM-DD")
        settings_obj.time_format = data.get("time_format", "24h")
        settings_obj.email_backend = data.get("email_backend", "disabled")
        settings_obj.smtp_host = data.get("smtp_host", "")
        settings_obj.smtp_port = data.get("smtp_port", 587)
        settings_obj.smtp_username = data.get("smtp_username", "")
        settings_obj.smtp_password_encrypted = data.get("smtp_password_encrypted", "")
        settings_obj.smtp_use_tls = data.get("smtp_use_tls", True)
        settings_obj.smtp_from_email = data.get("smtp_from_email", "")
        settings_obj.resend_api_key_encrypted = data.get("resend_api_key_encrypted", "")
        settings_obj.resend_from_email = data.get("resend_from_email", "")
        settings_obj.default_notification_email = data.get("default_notification_email", "")
        settings_obj.retention_days = data.get("retention_days", 0)
        settings_obj.retention_count = data.get("retention_count", 0)
        settings_obj.auto_cleanup_enabled = data.get("auto_cleanup_enabled", False)
        settings_obj.schedules_paused = data.get("schedules_paused", False)
        settings_obj.save()

    @classmethod
    def _import_users(cls, users_data: List[dict], current_user) -> dict:
        """
        Import users and create email->user mapping.
        Maps all to current user for simplicity.

        Returns:
            dict: {"email@example.com": User instance}
        """
        user_map = {}

        # If current_user exists, map all emails to current user
        if current_user:
            for user_data in users_data:
                email = user_data.get("email")
                if email:
                    user_map[email] = current_user

        # Also handle None case
        user_map[None] = current_user if current_user else None

        return user_map

    @classmethod
    def _import_environments(cls, envs_data: List[dict], user_map: dict, current_user) -> dict:
        """
        Import environments.

        Returns:
            dict: {"old_uuid": Environment instance}
        """
        env_map = {}

        for env_data in envs_data:
            old_id = env_data["id"]
            created_by = user_map.get(env_data.get("created_by_email"), current_user)

            env = Environment.objects.create(
                id=old_id,  # Preserve UUID
                name=env_data["name"],
                description=env_data.get("description", ""),
                path=env_data["path"],
                python_version=env_data.get("python_version", ""),
                requirements=env_data.get("requirements", ""),
                is_default=False,  # Will set default after all are created
                is_active=env_data.get("is_active", True),
                created_at=cls._deserialize_datetime(env_data.get("created_at")),
                updated_at=cls._deserialize_datetime(env_data.get("updated_at")),
                created_by=created_by,
            )
            env_map[old_id] = env

            # Set default environment
            if env_data.get("is_default"):
                env.is_default = True
                env.save()

        return env_map

    @classmethod
    def _import_secrets(cls, secrets_data: List[dict], user_map: dict, current_user) -> None:
        """Import secrets (encrypted values unchanged)."""
        for secret_data in secrets_data:
            created_by = user_map.get(secret_data.get("created_by_email"), current_user)

            Secret.objects.create(
                id=secret_data["id"],  # Preserve UUID
                key=secret_data["key"],
                encrypted_value=secret_data["encrypted_value"],
                description=secret_data.get("description", ""),
                created_at=cls._deserialize_datetime(secret_data.get("created_at")),
                updated_at=cls._deserialize_datetime(secret_data.get("updated_at")),
                created_by=created_by,
            )

    @classmethod
    def _import_scripts(cls, scripts_data: List[dict], env_map: dict, user_map: dict, current_user) -> dict:
        """Import scripts with proper foreign key mapping."""
        script_map = {}

        for script_data in scripts_data:
            old_id = script_data["id"]
            env = env_map.get(script_data["environment_id"])
            created_by = user_map.get(script_data.get("created_by_email"), current_user)

            if not env:
                continue  # Skip if environment not found

            script = Script.objects.create(
                id=old_id,  # Preserve UUID
                name=script_data["name"],
                description=script_data.get("description", ""),
                code=script_data["code"],
                environment=env,
                timeout_seconds=script_data.get("timeout_seconds", 300),
                is_enabled=script_data.get("is_enabled", False),
                webhook_token=script_data.get("webhook_token"),
                notify_on=script_data.get("notify_on", "never"),
                notify_email=script_data.get("notify_email", ""),
                notify_webhook_url=script_data.get("notify_webhook_url", ""),
                notify_webhook_enabled=script_data.get("notify_webhook_enabled", False),
                retention_days_override=script_data.get("retention_days_override"),
                retention_count_override=script_data.get("retention_count_override"),
                created_at=cls._deserialize_datetime(script_data.get("created_at")),
                updated_at=cls._deserialize_datetime(script_data.get("updated_at")),
                created_by=created_by,
            )
            script_map[old_id] = script

        return script_map

    @classmethod
    def _import_schedules(cls, schedules_data: List[dict], script_map: dict, user_map: dict, current_user) -> None:
        """Import schedules (q_schedule_ids will be regenerated later)."""
        for schedule_data in schedules_data:
            script = script_map.get(schedule_data["script_id"])
            created_by = user_map.get(schedule_data.get("created_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            ScriptSchedule.objects.create(
                id=schedule_data["id"],  # Preserve UUID
                script=script,
                run_mode=schedule_data.get("run_mode", "manual"),
                interval_minutes=schedule_data.get("interval_minutes"),
                daily_times=schedule_data.get("daily_times", []),
                timezone=schedule_data.get("timezone", "UTC"),
                is_active=schedule_data.get("is_active", True),
                q_schedule_ids=[],  # Will be regenerated
                next_run=None,  # Will be calculated
                created_at=cls._deserialize_datetime(schedule_data.get("created_at")),
                updated_at=cls._deserialize_datetime(schedule_data.get("updated_at")),
                created_by=created_by,
            )

    @classmethod
    def _import_schedule_history(cls, history_data: List[dict], user_map: dict, current_user) -> None:
        """Import schedule history."""
        for item_data in history_data:
            # Get schedule by ID
            try:
                schedule = ScriptSchedule.objects.get(id=item_data["schedule_id"])
            except ScriptSchedule.DoesNotExist:
                continue  # Skip if schedule not found

            changed_by = user_map.get(item_data.get("changed_by_email"), current_user)

            ScheduleHistory.objects.create(
                id=item_data["id"],  # Preserve UUID
                schedule=schedule,
                change_type=item_data["change_type"],
                previous_config=item_data.get("previous_config"),
                new_config=item_data.get("new_config"),
                changed_by=changed_by,
                created_at=cls._deserialize_datetime(item_data.get("created_at")),
            )

    @classmethod
    def _import_runs(cls, runs_data: List[dict], script_map: dict, user_map: dict, current_user) -> None:
        """Import runs."""
        for run_data in runs_data:
            script = script_map.get(run_data["script_id"])
            triggered_by = user_map.get(run_data.get("triggered_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            Run.objects.create(
                id=run_data["id"],  # Preserve UUID
                script=script,
                status=run_data["status"],
                exit_code=run_data.get("exit_code"),
                stdout=run_data.get("stdout", ""),
                stderr=run_data.get("stderr", ""),
                started_at=cls._deserialize_datetime(run_data.get("started_at")),
                ended_at=cls._deserialize_datetime(run_data.get("ended_at")),
                code_snapshot=run_data.get("code_snapshot", ""),
                trigger_type=run_data.get("trigger_type", "manual"),
                triggered_by=triggered_by,
                created_at=cls._deserialize_datetime(run_data.get("created_at")),
            )

    @classmethod
    def _import_package_operations(cls, ops_data: List[dict], env_map: dict, user_map: dict, current_user) -> None:
        """Import package operations."""
        for op_data in ops_data:
            env = env_map.get(op_data["environment_id"])
            created_by = user_map.get(op_data.get("created_by_email"), current_user)

            if not env:
                continue  # Skip if environment not found

            PackageOperation.objects.create(
                id=op_data["id"],  # Preserve UUID
                environment=env,
                operation=op_data["operation"],
                package_spec=op_data.get("package_spec", ""),
                status=op_data["status"],
                output=op_data.get("output", ""),
                error=op_data.get("error", ""),
                created_at=cls._deserialize_datetime(op_data.get("created_at")),
                started_at=cls._deserialize_datetime(op_data.get("started_at")),
                completed_at=cls._deserialize_datetime(op_data.get("completed_at")),
                created_by=created_by,
            )

    @classmethod
    def _regenerate_all_schedules(cls) -> int:
        """
        Regenerate all django-q2 schedules after restore.

        Returns:
            int: Number of schedules created
        """
        count = 0
        for script_schedule in ScriptSchedule.objects.filter(is_active=True):
            if script_schedule.is_scheduled:
                ScheduleService.sync_schedule(script_schedule)
                count += 1
        return count
