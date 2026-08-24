"""
Service for creating and restoring PyRunner backups.
"""
import gzip
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from core.models import (
    Database,
    DatabaseGrant,
    DataStore,
    DataStoreEntry,
    Environment,
    GlobalSettings,
    Library,
    PackageOperation,
    Run,
    ScheduleHistory,
    Script,
    ScriptSchedule,
    Secret,
    SecretProvider,
    Tag,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.models.library import hash_modules
from core.services.encryption_service import EncryptionService
from core.services.schedule_service import ScheduleService
from pyrunner.version import __version__ as PYRUNNER_VERSION


class BackupService:
    """
    Service for creating and restoring PyRunner backups.
    """

    # 1.2.0 — tenancy: a ``workspaces`` array + ``workspace_id`` on each scoped
    # row, so a whole-instance restore round-trips workspaces instead of
    # collapsing every tenant into the default (tenancy Stage 5). Backward
    # compatible: a pre-1.2.0 backup has neither, and restore maps its rows to the
    # default workspace.
    # 1.3.0 — Plugin Platform v2: ``owner_plugin``/``owner_key`` on
    # Script/Secret/DataStore, ``injection_mode`` + embedded ``secret_grants`` on
    # scripts. Backward compatible: a pre-1.3.0 backup has none of these, and
    # restore defaults them to NULL owner / ``injection_mode='all'`` / no grants
    # (= today's behavior), so old backups never hit a UNIQUE violation.
    # 1.4.0 — close the fields added after the format was first designed: weekly/
    # monthly schedule fields (``weekly_days``/``weekly_times``/``monthly_days``/
    # ``monthly_times`` — a weekly/monthly schedule previously restored as an empty
    # shell) and, on scripts, ``tags`` (embedded ``{name, color}``; get_or_create
    # by the globally-unique name on restore), ``archived_at``/``archived_by`` (soft
    # delete), and ``isolation_mode`` (sandbox opt-in). Backward compatible: a
    # pre-1.4.0 backup omits all of these and restore defaults them (empty lists /
    # not-archived / ``isolation_mode='inherit'``).
    #
    # 1.5.0 — Databases (managed Postgres): a ``databases`` array with row
    # metadata (schema/role names preserved verbatim — a dump only replays into
    # the same schema name), the Fernet-encrypted role password (same same-key
    # requirement as secrets), embedded per-database script ``grants``, and —
    # when the data server is reachable and pg_dump is installed — a plain-SQL
    # ``dump_sql`` of each ready schema (``dump_skipped_reason`` records why one
    # is absent). Restore re-provisions and replays; with no data server
    # attached, databases are skipped with a warning. Backward compatible: a
    # pre-1.5.0 backup has no ``databases`` key.
    #
    # 1.6.0 — External Secret Providers: a ``secret_providers`` array (one row per
    # configured external secrets backend — Vault/AWS/Infisical/Doppler/custom —
    # with ``credentials_encrypted`` carried VERBATIM, the same same-ENCRYPTION_KEY
    # stance as secrets) and three new fields on each secret: ``source``
    # (local/external), ``provider_name`` (the referenced profile BY NAME — ids
    # differ across instances, names are the stable join key), and ``external_ref``.
    # On restore, providers are upserted by name (never delete existing — they are
    # instance credential config, like the AI-provider rows below) and external
    # secrets relink to them by name. Backward compatible: a pre-1.6.0 backup has no
    # ``secret_providers`` key and no ``source`` on its secrets → every secret
    # defaults to ``source="local"`` (today's path, byte-for-byte).
    #
    # 1.7.0 — Script Libraries: a ``libraries`` array (one row per library, with
    # its HEAD revision's ``modules`` inlined and ``current_version`` carried
    # verbatim), plus ``libraries`` (attachment ids) on each script and
    # ``library_versions`` on each run. HEAD ONLY, by design: a backup restores a
    # working system, and forensic revision history is not operational state —
    # so restore recreates a single revision numbered ``current_version`` rather
    # than renumbering to 1. That keeps every restored run's stamp meaningful
    # (a run pinned to the head still resolves); a run pinned to an OLDER version
    # fails closed after restore, which is the documented trade. Backward
    # compatible: a pre-1.7.0 backup has no ``libraries`` key, no attachments and
    # no run stamps → nothing to materialize, exactly today's behavior.
    #
    # Deliberately NOT in the backup (documented so the omission reads as a choice,
    # not an oversight): S3/backup credentials, AI provider rows + settings,
    # reCAPTCHA keys, ``admin_url_slug``, worker/heartbeat settings, the Channels
    # subsystem (channels + members + messages), and public API tokens. These are
    # deployment-/secret-bound to a specific instance — restoring them onto a
    # different host would be wrong (stale endpoints) or a credential-leak vector —
    # so they are reconfigured per instance rather than carried in a portable dump.
    BACKUP_VERSION = "1.8.0"
    MAX_BACKUP_SIZE_MB = 100

    # Backup format constants
    FORMAT_JSON = "json"
    FORMAT_GZIP = "gzip"
    SUPPORTED_FORMATS = [FORMAT_JSON, FORMAT_GZIP]

    @classmethod
    def create_backup(
        cls,
        include_runs: bool = True,
        max_runs: int = 1000,
        include_package_operations: bool = False,
        include_datastores: bool = True,
        created_by_user=None,
    ) -> dict:
        """
        Create a complete backup of the PyRunner instance.

        Args:
            include_runs: Include run history in backup
            max_runs: Maximum number of runs to include (0 = all, default 1000)
            include_package_operations: Include package operation history
            include_datastores: Include datastores and their entries
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
            "workspaces": cls._export_workspaces(),
            "environments": cls._export_environments(),
            "users": cls._export_users(),
            "libraries": cls._export_libraries(),
            "scripts": cls._export_scripts(),
            "script_schedules": cls._export_schedules(),
            "schedule_history": cls._export_schedule_history(),
            "secret_providers": cls._export_secret_providers(),
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

        if include_datastores:
            backup_data["datastores"] = cls._export_datastores()
        else:
            backup_data["datastores"] = []

        backup_data["databases"] = cls._export_databases()

        return backup_data

    @classmethod
    def _export_global_settings(cls) -> dict:
        """Export GlobalSettings to dict."""
        settings_obj = GlobalSettings.get_settings()
        return {
            "instance_name": settings_obj.instance_name,
            "timezone": settings_obj.timezone,
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
    def _export_workspaces(cls) -> List[dict]:
        """Export workspaces so a restore can round-trip tenancy (Stage 5)."""
        workspaces = []
        for ws in Workspace.objects.all().order_by("created_at"):
            workspaces.append({
                "id": str(ws.id),
                "name": ws.name,
                "is_default": ws.is_default,
                "created_at": cls._serialize_datetime(ws.created_at),
                "updated_at": cls._serialize_datetime(ws.updated_at),
            })
        return workspaces

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
                "workspace_id": str(env.workspace_id) if env.workspace_id else None,
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
    def _export_libraries(cls) -> List[dict]:
        """Export libraries with their HEAD revision inlined (never full history).

        ``current_version`` is carried verbatim so restore can recreate the head
        under its original number — a restored run's ``library_versions`` stamp
        must keep pointing at the content it actually ran.
        """
        from core.models import Library, LibraryRevision

        libraries = []
        queryset = Library.objects.select_related("created_by").order_by("created_at")
        # One query for every head revision instead of one per library.
        heads = {
            rev.library_id: rev
            for rev in LibraryRevision.objects.filter(
                library__in=queryset, version=models.F("library__current_version")
            )
        }
        for library in queryset:
            head = heads.get(library.id)
            libraries.append({
                "id": str(library.id),
                "key": library.key,
                "name": library.name,
                "description": library.description,
                "workspace_id": str(library.workspace_id) if library.workspace_id else None,
                "owner_plugin": library.owner_plugin,
                "owner_key": library.owner_key,
                "current_version": library.current_version,
                "modules": head.modules if head else {},
                "created_at": cls._serialize_datetime(library.created_at),
                "updated_at": cls._serialize_datetime(library.updated_at),
                "created_by_email": library.created_by.email if library.created_by else None,
            })
        return libraries

    @classmethod
    def _export_scripts(cls) -> List[dict]:
        """Export scripts with all fields."""
        scripts = []
        queryset = (
            Script.objects.select_related("environment", "created_by", "archived_by")
            .prefetch_related("tags", "secret_grants", "library_attachments")
            .all()
            .order_by("created_at")
        )
        for script in queryset:
            scripts.append({
                "id": str(script.id),
                "name": script.name,
                "description": script.description,
                "code": script.code,
                "environment_id": str(script.environment.id),
                "workspace_id": str(script.workspace_id) if script.workspace_id else None,
                "owner_plugin": script.owner_plugin,
                "owner_key": script.owner_key,
                "injection_mode": script.injection_mode,
                "isolation_mode": script.isolation_mode,
                "timeout_seconds": script.timeout_seconds,
                "is_enabled": script.is_enabled,
                "webhook_token": script.webhook_token,
                "notify_on": script.notify_on,
                "notify_email": script.notify_email,
                "notify_webhook_url": script.notify_webhook_url,
                "notify_webhook_enabled": script.notify_webhook_enabled,
                "retention_days_override": script.retention_days_override,
                "retention_count_override": script.retention_count_override,
                # Tags are instance-global (unique name). Exported as {name, color}
                # and re-attached via get_or_create-by-name on restore, so a restore
                # onto an instance that already has the tag reuses it.
                "tags": [
                    {"name": t.name, "color": t.color}
                    for t in script.tags.all()
                ],
                # Soft-delete (archive) state. archived_by follows the same
                # simplified user mapping as created_by on restore.
                "archived_at": cls._serialize_datetime(script.archived_at),
                "archived_by_email": script.archived_by.email if script.archived_by else None,
                # Embedded per-script secret grants (selected-mode injection).
                # Imported after secrets+scripts exist; old backups have none.
                "secret_grants": [
                    {"secret_id": str(g.secret_id), "active": g.active}
                    for g in script.secret_grants.all()
                ],
                # Attached libraries (ids; UUIDs are preserved across restore).
                # Imported in their own pass once both sides exist, exactly like
                # secret_grants. Old backups carry none.
                "libraries": [
                    str(a.library_id) for a in script.library_attachments.all()
                ],
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
                "workspace_id": str(schedule.workspace_id) if schedule.workspace_id else None,
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "weekly_days": schedule.weekly_days,
                "weekly_times": schedule.weekly_times,
                "monthly_days": schedule.monthly_days,
                "monthly_times": schedule.monthly_times,
                "yearly_month": schedule.yearly_month,
                "yearly_day": schedule.yearly_day,
                "yearly_time": schedule.yearly_time,
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
    def _export_secret_providers(cls) -> List[dict]:
        """Export external secret-provider profiles (External Secret Providers).

        Instance-global config (no ``workspace_id``), like the AI-provider rows —
        but unlike those, these ARE carried, so external secrets round-trip. The
        credential blob is exported VERBATIM: it is Fernet-encrypted with the
        instance ``ENCRYPTION_KEY``, which the backup already pins via
        ``encryption_key_hash`` — so it is decryptable on any target restoring with
        the same key (and unreadable, as intended, without it).
        """
        providers = []
        for p in SecretProvider.objects.all().order_by("created_at"):
            providers.append({
                "id": str(p.id),
                "provider_type": p.provider_type,
                "name": p.name,
                "config": p.config,
                "credentials_encrypted": p.credentials_encrypted,
                "cache_ttl": p.cache_ttl,
                "on_error": p.on_error,
                "last_tested_at": cls._serialize_datetime(p.last_tested_at),
                "created_at": cls._serialize_datetime(p.created_at),
                "updated_at": cls._serialize_datetime(p.updated_at),
            })
        return providers

    @classmethod
    def _export_secrets(cls) -> List[dict]:
        """Export secrets (keep encrypted values)."""
        secrets = []
        for secret in Secret.objects.select_related("created_by", "provider").all().order_by("created_at"):
            secrets.append({
                "id": str(secret.id),
                "key": secret.key,
                "workspace_id": str(secret.workspace_id) if secret.workspace_id else None,
                "owner_plugin": secret.owner_plugin,
                "owner_key": secret.owner_key,
                # External Secret Providers: a local secret carries its Fernet
                # value; an external secret carries no value (encrypted_value is
                # blank) and instead points at a provider BY NAME + a reference.
                "source": secret.source,
                "provider_name": secret.provider.name if secret.provider_id else None,
                "external_ref": secret.external_ref,
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
                "workspace_id": str(run.workspace_id) if run.workspace_id else None,
                "status": run.status,
                "exit_code": run.exit_code,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "started_at": cls._serialize_datetime(run.started_at),
                "ended_at": cls._serialize_datetime(run.ended_at),
                "code_snapshot": run.code_snapshot,
                # code_snapshot's counterpart for shared code — without it a
                # restored run couldn't say which library versions it ran.
                "library_versions": run.library_versions,
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
    def _export_datastores(cls) -> List[dict]:
        """Export DataStores and their entries."""
        datastores = []
        for ds in DataStore.objects.select_related("created_by").all().order_by("created_at"):
            entries = []
            for entry in ds.entries.all().order_by("key"):
                entries.append({
                    "id": str(entry.id),
                    "key": entry.key,
                    "value_json": entry.value_json,
                    "created_at": cls._serialize_datetime(entry.created_at),
                    "updated_at": cls._serialize_datetime(entry.updated_at),
                })

            datastores.append({
                "id": str(ds.id),
                "name": ds.name,
                "workspace_id": str(ds.workspace_id) if ds.workspace_id else None,
                "owner_plugin": ds.owner_plugin,
                "owner_key": ds.owner_key,
                "description": ds.description,
                "created_at": cls._serialize_datetime(ds.created_at),
                "updated_at": cls._serialize_datetime(ds.updated_at),
                "created_by_email": ds.created_by.email if ds.created_by else None,
                "entries": entries,
            })
        return datastores

    @classmethod
    def _export_databases(cls) -> List[dict]:
        """Export managed databases: row metadata + grants + a pg_dump of each
        READY schema when the data server and client binaries allow it.

        Best-effort per database: a failed/unavailable dump records its reason
        in ``dump_skipped_reason`` instead of failing the whole backup — the
        metadata (and everything else in the backup) is still exact.
        """
        from core.services.database_service import DatabaseService

        databases = []
        for db in Database.objects.select_related("created_by").all().order_by("created_at"):
            dump_sql, skip_reason = None, ""
            if not DatabaseService.is_configured():
                skip_reason = "no data server attached (PYRUNNER_DATA_DB_URL unset)"
            elif not db.is_ready:
                skip_reason = f"database status is '{db.status}'"
            else:
                try:
                    dump_sql = DatabaseService.dump_schema(db)
                except Exception as e:
                    skip_reason = str(e)

            databases.append({
                "id": str(db.id),
                "name": db.name,
                "workspace_id": str(db.workspace_id) if db.workspace_id else None,
                "owner_plugin": db.owner_plugin,
                "owner_key": db.owner_key,
                # Preserved verbatim: the dump's statements are qualified with
                # this schema name, so restore must recreate it 1:1.
                "schema_name": db.schema_name,
                "role_name": db.role_name,
                # Fernet-encrypted — same same-ENCRYPTION_KEY requirement the
                # backup already imposes for secrets (encryption_key_hash).
                "encrypted_password": db.encrypted_password,
                "description": db.description,
                "grants": [
                    {"script_id": str(g.script_id), "active": g.active}
                    for g in db.grants.all()
                ],
                "dump_sql": dump_sql,
                "dump_skipped_reason": skip_reason,
                "created_at": cls._serialize_datetime(db.created_at),
                "updated_at": cls._serialize_datetime(db.updated_at),
                "created_by_email": db.created_by.email if db.created_by else None,
            })
        return databases

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
    # SERIALIZATION METHODS
    # =====================================================================

    @classmethod
    def serialize_backup(cls, backup_data: dict, format: str = FORMAT_GZIP) -> Tuple[bytes, str]:
        """
        Serialize backup data to bytes.

        Args:
            backup_data: The backup dict to serialize
            format: "json" or "gzip" (default: gzip)

        Returns:
            tuple: (bytes_data, content_type)
        """
        json_str = json.dumps(backup_data, indent=2)

        if format == cls.FORMAT_GZIP:
            # Compress with gzip
            buffer = io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
                gz.write(json_str.encode("utf-8"))
            return buffer.getvalue(), "application/gzip"
        else:
            return json_str.encode("utf-8"), "application/json"

    @classmethod
    def deserialize_backup(cls, data: bytes, filename: str = "") -> dict:
        """
        Deserialize backup data from bytes.
        Auto-detects format based on magic bytes or filename.

        Args:
            data: Raw bytes from backup file
            filename: Original filename (helps with detection)

        Returns:
            dict: Parsed backup data

        Raises:
            ValueError: If format is unrecognized or parsing fails
        """
        # Detect gzip by magic bytes (1f 8b)
        is_gzip = data[:2] == b"\x1f\x8b"

        # Also check filename as fallback
        if not is_gzip and filename.endswith(".gz"):
            is_gzip = True

        if is_gzip:
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                    json_str = gz.read().decode("utf-8")
            except Exception as e:
                raise ValueError(f"Failed to decompress gzip backup: {e}")
        else:
            json_str = data.decode("utf-8")

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in backup: {e}")

    @classmethod
    def get_file_extension(cls, format: str) -> str:
        """Return appropriate file extension for format."""
        if format == cls.FORMAT_GZIP:
            return ".json.gz"
        return ".json"

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

        datastores = backup_data.get("datastores", [])
        counts = {
            "scripts": len(backup_data.get("scripts", [])),
            "workspaces": len(backup_data.get("workspaces", [])),
            "environments": len(backup_data.get("environments", [])),
            "secret_providers": len(backup_data.get("secret_providers", [])),
            "secrets": len(backup_data.get("secrets", [])),
            "runs": len(backup_data.get("runs", [])),
            "schedules": len(backup_data.get("script_schedules", [])),
            "users": len(backup_data.get("users", [])),
            "package_operations": len(backup_data.get("package_operations", [])),
            "datastores": len(datastores),
            "datastore_entries": sum(len(ds.get("entries", [])) for ds in datastores),
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
            warnings: List[str] = []

            # Delete existing data (reverse dependency order)
            Run.objects.all().delete()
            PackageOperation.objects.all().delete()
            ScheduleHistory.objects.all().delete()
            ScriptSchedule.objects.all().delete()
            Script.objects.all().delete()
            # After Scripts (whose deletion cascades the attachment rows) and
            # before the import: restore is a full replace, and a library key is
            # unique per workspace, so a surviving row would collide with the
            # incoming one. Cascades to revisions.
            Library.objects.all().delete()
            Secret.objects.all().delete()
            Environment.objects.all().delete()
            DataStoreEntry.objects.all().delete()
            DataStore.objects.all().delete()
            # Databases are full-replace like everything else, but their data
            # lives on the data server — deprovision each (best-effort; a
            # failure becomes a warning, never an aborted restore) before the
            # rows go, or the schemas would be orphaned server-side.
            warnings.extend(cls._cleanup_existing_databases())
            Database.objects.all().delete()
            # Note: We don't delete User objects to preserve authentication
            # GlobalSettings is singleton, so we update rather than delete

            # Import data in dependency order
            cls._import_global_settings(backup_data.get("global_settings", {}))

            # Tenancy Stage 5: rebuild the workspace topology FIRST so every scoped
            # row can be re-associated. The source's default workspace maps to THIS
            # instance's default (a single default always exists); non-default
            # source workspaces are recreated (UUID preserved). A pre-1.2.0 backup
            # has no workspaces/workspace_id → every row falls back to the default
            # (no tenant collapse to worry about — there was only one tenant).
            default_ws = Workspace.get_default()
            if default_ws is None:
                default_ws = Workspace.objects.create(
                    name="Default Workspace", is_default=True
                )
            ws_map = cls._import_workspaces(
                backup_data.get("workspaces", []), default_ws, current_user
            )

            user_map = cls._import_users(backup_data.get("users", []), current_user)
            env_map = cls._import_environments(backup_data.get("environments", []), user_map, current_user, ws_map, default_ws)
            # Secret providers first (external secrets relink to them by name).
            # Upsert, not full-replace: an older backup omits the key → no-op, and
            # existing provider credentials are never wiped by an unrelated restore.
            providers_restored = cls._import_secret_providers(backup_data.get("secret_providers", []))
            warnings.extend(
                cls._import_secrets(backup_data.get("secrets", []), user_map, current_user, ws_map, default_ws)
            )
            libraries_restored = cls._import_libraries(
                backup_data.get("libraries", []), user_map, current_user, ws_map, default_ws
            )
            script_map = cls._import_scripts(backup_data.get("scripts", []), env_map, user_map, current_user, ws_map, default_ws)
            # Grants reference both Secrets (imported above) and Scripts (just
            # imported), so they go in their own pass. Old backups carry none.
            cls._import_secret_grants(backup_data.get("scripts", []), script_map)
            # Library attachments need both sides too — same pass-ordering reason.
            cls._import_library_attachments(backup_data.get("scripts", []), script_map)
            cls._import_schedules(backup_data.get("script_schedules", []), script_map, user_map, current_user, ws_map, default_ws)
            cls._import_schedule_history(backup_data.get("schedule_history", []), user_map, current_user)

            if restore_runs:
                cls._import_runs(backup_data.get("runs", []), script_map, user_map, current_user, ws_map, default_ws)

            cls._import_package_operations(backup_data.get("package_operations", []), env_map, user_map, current_user)
            ds_map = cls._import_datastores(backup_data.get("datastores", []), user_map, current_user, ws_map, default_ws)
            databases_restored, db_warnings = cls._import_databases(
                backup_data.get("databases", []), script_map, user_map,
                current_user, ws_map, default_ws,
            )
            warnings.extend(db_warnings)

            # Regenerate django-q2 schedules
            schedules_created = cls._regenerate_all_schedules()

            counts = {
                "environments": len(env_map),
                "scripts": len(script_map),
                "secret_providers": providers_restored,
                "secrets": Secret.objects.count(),
                "runs": Run.objects.count() if restore_runs else 0,
                "schedules": ScriptSchedule.objects.count(),
                "schedules_regenerated": schedules_created,
                "datastores": len(ds_map),
                "datastore_entries": DataStoreEntry.objects.count(),
                "databases": databases_restored,
                "libraries": libraries_restored,
            }

            return {"success": True, "counts": counts, "errors": [], "warnings": warnings}

        except Exception as e:
            return {"success": False, "counts": {}, "errors": [str(e)], "warnings": []}

    @classmethod
    def _import_global_settings(cls, data: dict) -> None:
        """Import GlobalSettings."""
        settings_obj = GlobalSettings.get_settings()
        settings_obj.instance_name = data.get("instance_name", "PyRunner")
        settings_obj.timezone = data.get("timezone", "UTC")
        # date_format/time_format were removed (stored but never displayed);
        # old backups may still carry the keys — ignored harmlessly on restore.
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
    def _import_workspaces(cls, workspaces_data: List[dict], default_ws, current_user) -> dict:
        """Rebuild the workspace topology and map backup-ws-id → local Workspace.

        The source's default workspace maps to this instance's default (keeping a
        single default); each non-default source workspace is recreated with its
        UUID preserved (reused if it already exists). The user performing the
        restore is made an Owner of each recreated workspace so the restored
        tenants are actually reachable (membership rows themselves are not part of
        the whole-instance backup — see the simplified user import).
        """
        ws_map = {}
        for w in workspaces_data:
            old_id = w["id"]
            if w.get("is_default"):
                ws_map[old_id] = default_ws  # source default → local default
                continue
            ws = Workspace.objects.filter(pk=old_id).first()
            if ws is None:
                ws = Workspace.objects.create(
                    id=old_id,
                    name=w.get("name", "Workspace"),
                    is_default=False,
                    created_at=cls._deserialize_datetime(w.get("created_at")),
                    updated_at=cls._deserialize_datetime(w.get("updated_at")),
                )
                if current_user is not None:
                    WorkspaceMembership.ensure(
                        current_user, ws, role=WorkspaceMembership.ROLE_OWNER
                    )
            ws_map[old_id] = ws
        return ws_map

    @classmethod
    def _resolve_workspace(cls, workspace_id, ws_map: dict, default_ws):
        """Map a backed-up workspace_id to a local Workspace (default fallback)."""
        if not workspace_id:
            return default_ws
        return ws_map.get(workspace_id, default_ws)

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
    def _import_environments(cls, envs_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> dict:
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
                workspace=cls._resolve_workspace(env_data.get("workspace_id"), ws_map, default_ws),
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
    def _import_secret_providers(cls, providers_data: List[dict]) -> int:
        """Import external secret-provider profiles, keyed by NAME.

        Upsert (not full-replace): the profile ``name`` is unique and stable across
        instances — the join key both for idempotent import and for relinking
        external secrets — so an existing profile of the same name is updated to
        match the backup (credentials VERBATIM; the backup's key-hash gate
        guarantees they decrypt on the target). Nothing is deleted: providers are
        instance credential config, and an older archive omits this key entirely,
        so a restore never wipes providers the backup simply predates.

        Returns the number of profiles imported.
        """
        for p in providers_data:
            SecretProvider.objects.update_or_create(
                name=p["name"],
                defaults={
                    "provider_type": p["provider_type"],
                    "config": p.get("config", {}),
                    "credentials_encrypted": p.get("credentials_encrypted", ""),
                    "cache_ttl": p.get("cache_ttl", 300),
                    "on_error": p.get("on_error", SecretProvider.OnError.FAIL),
                    "last_tested_at": cls._deserialize_datetime(p.get("last_tested_at")),
                },
            )
        return len(providers_data)

    @classmethod
    def _import_secrets(cls, secrets_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> List[str]:
        """Import secrets (encrypted values unchanged).

        External Secret Providers: an external secret relinks to its provider BY
        NAME (providers are imported first, so a live lookup resolves them — same
        by-key pattern as ``_import_secret_grants``). A pre-1.6.0 backup has no
        ``source`` → defaults to ``local``, today's path byte-for-byte. An external
        secret whose provider name isn't present is imported UNLINKED with a warning
        (fail-closed: it surfaces for the user to re-point, rather than silently
        becoming an empty local secret). Returns any such warnings.
        """
        warnings: List[str] = []
        for secret_data in secrets_data:
            created_by = user_map.get(secret_data.get("created_by_email"), current_user)

            source = secret_data.get("source", Secret.Source.LOCAL)
            provider = None
            if source == Secret.Source.EXTERNAL:
                provider_name = secret_data.get("provider_name")
                provider = SecretProvider.objects.filter(name=provider_name).first()
                if provider is None:
                    warnings.append(
                        f"Secret '{secret_data.get('key')}': external provider "
                        f"'{provider_name}' was not found — imported unlinked; "
                        "re-point it to a provider in the console."
                    )

            Secret.objects.create(
                id=secret_data["id"],  # Preserve UUID
                key=secret_data["key"],
                workspace=cls._resolve_workspace(secret_data.get("workspace_id"), ws_map, default_ws),
                owner_plugin=secret_data.get("owner_plugin"),
                owner_key=secret_data.get("owner_key"),
                source=source,
                provider=provider,
                external_ref=secret_data.get("external_ref", ""),
                encrypted_value=secret_data.get("encrypted_value", ""),
                description=secret_data.get("description", ""),
                created_at=cls._deserialize_datetime(secret_data.get("created_at")),
                updated_at=cls._deserialize_datetime(secret_data.get("updated_at")),
                created_by=created_by,
            )
        return warnings

    @classmethod
    def _import_scripts(cls, scripts_data: List[dict], env_map: dict, user_map: dict, current_user, ws_map: dict, default_ws) -> dict:
        """Import scripts with proper foreign key mapping."""
        script_map = {}

        for script_data in scripts_data:
            old_id = script_data["id"]
            env = env_map.get(script_data["environment_id"])
            created_by = user_map.get(script_data.get("created_by_email"), current_user)

            if not env:
                continue  # Skip if environment not found

            # Archive state: only resolve archived_by when the script is actually
            # archived (pre-1.4.0 backups omit both keys → not archived).
            archived_at = cls._deserialize_datetime(script_data.get("archived_at"))
            archived_by_email = script_data.get("archived_by_email")
            archived_by = (
                user_map.get(archived_by_email, current_user)
                if archived_at and archived_by_email
                else None
            )

            script = Script.objects.create(
                id=old_id,  # Preserve UUID
                name=script_data["name"],
                description=script_data.get("description", ""),
                code=script_data["code"],
                environment=env,
                workspace=cls._resolve_workspace(script_data.get("workspace_id"), ws_map, default_ws),
                owner_plugin=script_data.get("owner_plugin"),
                owner_key=script_data.get("owner_key"),
                injection_mode=script_data.get("injection_mode", "all"),
                isolation_mode=script_data.get("isolation_mode", Script.IsolationMode.INHERIT),
                timeout_seconds=script_data.get("timeout_seconds", 300),
                is_enabled=script_data.get("is_enabled", False),
                webhook_token=script_data.get("webhook_token"),
                notify_on=script_data.get("notify_on", "never"),
                notify_email=script_data.get("notify_email", ""),
                notify_webhook_url=script_data.get("notify_webhook_url", ""),
                notify_webhook_enabled=script_data.get("notify_webhook_enabled", False),
                retention_days_override=script_data.get("retention_days_override"),
                retention_count_override=script_data.get("retention_count_override"),
                archived_at=archived_at,
                archived_by=archived_by,
                created_at=cls._deserialize_datetime(script_data.get("created_at")),
                updated_at=cls._deserialize_datetime(script_data.get("updated_at")),
                created_by=created_by,
            )

            # Re-attach tags by their globally-unique name (created if absent).
            # Tags aren't wiped by restore, so an existing tag is reused as-is.
            for tag_data in script_data.get("tags", []):
                tag, _ = Tag.objects.get_or_create(
                    name=tag_data["name"],
                    defaults={"color": tag_data.get("color", Tag.Color.GRAY)},
                )
                script.tags.add(tag)

            script_map[old_id] = script

        return script_map

    @classmethod
    def _import_libraries(cls, libraries_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> int:
        """Recreate libraries and their head revision. Returns the count restored.

        The head is recreated under its ORIGINAL version number (not renumbered to
        1), so a restored run's ``library_versions`` stamp still resolves and
        ``current_version`` never moves backwards. History before the head is not
        in the backup by design — see the 1.7.0 note above.
        """
        from core.models import Library, LibraryRevision

        restored = 0
        for data in libraries_data:
            created_by = user_map.get(data.get("created_by_email"), current_user)
            library, _ = Library.objects.update_or_create(
                id=data["id"],  # Preserve UUID (script attachments resolve by id)
                defaults={
                    "key": data["key"],
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "workspace": cls._resolve_workspace(data.get("workspace_id"), ws_map, default_ws),
                    "owner_plugin": data.get("owner_plugin"),
                    "owner_key": data.get("owner_key"),
                    "current_version": data.get("current_version", 0),
                    "created_by": created_by,
                },
            )
            modules = data.get("modules") or {}
            version = data.get("current_version", 0)
            if version and modules:
                LibraryRevision.objects.update_or_create(
                    library=library,
                    version=version,
                    defaults={
                        "modules": modules,
                        "content_hash": hash_modules(modules),
                        "created_by": created_by,
                    },
                )
            restored += 1
        return restored

    @classmethod
    def _import_library_attachments(cls, scripts_data: List[dict], script_map: dict) -> None:
        """Reattach libraries to scripts from the embedded ids in the script export.

        Runs after both libraries and scripts exist (UUIDs preserved, so ids
        resolve). An attachment pointing at a missing library is skipped
        (best-effort). Pre-1.7.0 backups carry no ``libraries`` key.
        """
        from core.models import Library, ScriptLibrary

        for script_data in scripts_data:
            script = script_map.get(script_data["id"])
            if not script:
                continue
            for library_id in script_data.get("libraries", []):
                library = Library.objects.filter(id=library_id).first()
                if library is None:
                    continue
                ScriptLibrary.objects.get_or_create(script=script, library=library)

    @classmethod
    def _import_secret_grants(cls, scripts_data: List[dict], script_map: dict) -> None:
        """Recreate per-script secret grants embedded in the script export.

        Runs after both secrets and scripts are imported (UUIDs preserved, so
        secrets resolve by id). A grant pointing at a missing secret/script is
        skipped (best-effort). Pre-1.3.0 backups carry no ``secret_grants`` key.
        """
        from core.models import SecretGrant

        for script_data in scripts_data:
            script = script_map.get(script_data["id"])
            if not script:
                continue
            for grant in script_data.get("secret_grants", []):
                secret = Secret.objects.filter(id=grant.get("secret_id")).first()
                if secret is None:
                    continue
                SecretGrant.objects.get_or_create(
                    script=script,
                    secret=secret,
                    defaults={"active": grant.get("active", True)},
                )

    @classmethod
    def _import_schedules(cls, schedules_data: List[dict], script_map: dict, user_map: dict, current_user, ws_map: dict, default_ws) -> None:
        """Import schedules (q_schedule_ids will be regenerated later)."""
        for schedule_data in schedules_data:
            script = script_map.get(schedule_data["script_id"])
            created_by = user_map.get(schedule_data.get("created_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            ScriptSchedule.objects.create(
                id=schedule_data["id"],  # Preserve UUID
                script=script,
                workspace=cls._resolve_workspace(schedule_data.get("workspace_id"), ws_map, default_ws),
                run_mode=schedule_data.get("run_mode", "manual"),
                interval_minutes=schedule_data.get("interval_minutes"),
                daily_times=schedule_data.get("daily_times", []),
                weekly_days=schedule_data.get("weekly_days", []),
                weekly_times=schedule_data.get("weekly_times", []),
                monthly_days=schedule_data.get("monthly_days", []),
                monthly_times=schedule_data.get("monthly_times", []),
                yearly_month=schedule_data.get("yearly_month"),
                yearly_day=schedule_data.get("yearly_day"),
                yearly_time=schedule_data.get("yearly_time", ""),
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
    def _import_runs(cls, runs_data: List[dict], script_map: dict, user_map: dict, current_user, ws_map: dict, default_ws) -> None:
        """Import runs."""
        for run_data in runs_data:
            script = script_map.get(run_data["script_id"])
            triggered_by = user_map.get(run_data.get("triggered_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            Run.objects.create(
                id=run_data["id"],  # Preserve UUID
                script=script,
                workspace=cls._resolve_workspace(run_data.get("workspace_id"), ws_map, default_ws),
                status=run_data["status"],
                exit_code=run_data.get("exit_code"),
                stdout=run_data.get("stdout", ""),
                stderr=run_data.get("stderr", ""),
                started_at=cls._deserialize_datetime(run_data.get("started_at")),
                ended_at=cls._deserialize_datetime(run_data.get("ended_at")),
                code_snapshot=run_data.get("code_snapshot", ""),
                library_versions=run_data.get("library_versions"),
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
    def _import_datastores(cls, datastores_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> dict:
        """Import DataStores and their entries."""
        ds_map = {}

        for ds_data in datastores_data:
            old_id = ds_data["id"]
            created_by = user_map.get(ds_data.get("created_by_email"), current_user)

            datastore = DataStore.objects.create(
                id=old_id,  # Preserve UUID
                name=ds_data["name"],
                workspace=cls._resolve_workspace(ds_data.get("workspace_id"), ws_map, default_ws),
                owner_plugin=ds_data.get("owner_plugin"),
                owner_key=ds_data.get("owner_key"),
                description=ds_data.get("description", ""),
                created_at=cls._deserialize_datetime(ds_data.get("created_at")),
                updated_at=cls._deserialize_datetime(ds_data.get("updated_at")),
                created_by=created_by,
            )
            ds_map[old_id] = datastore

            # Import entries for this datastore
            for entry_data in ds_data.get("entries", []):
                DataStoreEntry.objects.create(
                    id=entry_data["id"],  # Preserve UUID
                    datastore=datastore,
                    key=entry_data["key"],
                    value_json=entry_data["value_json"],
                    created_at=cls._deserialize_datetime(entry_data.get("created_at")),
                    updated_at=cls._deserialize_datetime(entry_data.get("updated_at")),
                )

        return ds_map

    @classmethod
    def _cleanup_existing_databases(cls) -> List[str]:
        """Deprovision the current instance's databases before full-replace.

        Best-effort: an unreachable data server (or none attached) yields a
        warning about orphaned schemas instead of aborting the restore — core
        data must remain restorable even when the data server is down.
        """
        from core.services.database_service import DatabaseService

        warnings = []
        existing = list(Database.objects.all())
        if not existing:
            return warnings
        if not DatabaseService.is_configured():
            warnings.append(
                f"{len(existing)} existing database(s) removed from PyRunner "
                "without server cleanup (no data server attached) — their "
                "schemas/roles may remain on the old server."
            )
            return warnings
        for db in existing:
            try:
                DatabaseService.deprovision(db)
            except Exception as e:
                warnings.append(
                    f"Database '{db.name}': server cleanup failed ({e}) — its "
                    f"schema '{db.schema_name}' may remain on the data server."
                )
        return warnings

    @classmethod
    def _import_databases(
        cls, databases_data: List[dict], script_map: dict, user_map: dict,
        current_user, ws_map: dict, default_ws,
    ) -> Tuple[int, List[str]]:
        """Recreate managed databases: row → provision → replay dump → grants.

        Schema/role names are restored VERBATIM (the dump's statements are
        qualified with the schema name), so this expects a fresh target — a
        clashing schema is reconciled by the idempotent provision, and a dump
        replay onto non-empty state fails loudly into a warning. With no data
        server attached, all databases are skipped with one warning.
        """
        from core.services.database_service import (
            DatabaseProvisionError,
            DatabaseService,
        )

        warnings: List[str] = []
        if not databases_data:
            return 0, warnings
        if not DatabaseService.is_configured():
            warnings.append(
                f"Skipped {len(databases_data)} database(s): no data server "
                "attached (PYRUNNER_DATA_DB_URL). Attach one and restore the "
                "backup again to recover them."
            )
            return 0, warnings

        restored = 0
        for data in databases_data:
            name = data.get("name", "?")
            try:
                db = Database.objects.create(
                    id=data["id"],  # Preserve UUID
                    name=name,
                    workspace=cls._resolve_workspace(data.get("workspace_id"), ws_map, default_ws),
                    owner_plugin=data.get("owner_plugin"),
                    owner_key=data.get("owner_key"),
                    schema_name=data["schema_name"],
                    role_name=data["role_name"],
                    encrypted_password=data.get("encrypted_password", ""),
                    description=data.get("description", ""),
                    status=Database.STATUS_PROVISIONING,
                    created_by=user_map.get(data.get("created_by_email"), current_user),
                )
            except Exception as e:
                warnings.append(f"Database '{name}': row import failed: {e}")
                continue

            restored += 1
            # Grants first — they are pure rows, restorable even if the server
            # half fails below (the script then just waits on a Retry).
            for grant in data.get("grants", []):
                script = script_map.get(grant.get("script_id"))
                if script is not None:
                    DatabaseGrant.objects.get_or_create(
                        script=script, database=db,
                        defaults={"active": grant.get("active", True)},
                    )

            try:
                DatabaseService.provision(db)
            except DatabaseProvisionError as e:
                warnings.append(
                    f"Database '{name}': provisioning failed ({e}) — left in "
                    "error status; fix the data server and use Retry on its page."
                )
                continue

            dump_sql = data.get("dump_sql")
            if dump_sql:
                try:
                    DatabaseService.restore_schema_dump(db, dump_sql)
                except DatabaseProvisionError as e:
                    warnings.append(
                        f"Database '{name}': data replay failed ({e}) — the "
                        "database is provisioned but its tables may be missing "
                        "or partial."
                    )
            elif data.get("dump_skipped_reason"):
                warnings.append(
                    f"Database '{name}': the backup carried no data "
                    f"({data['dump_skipped_reason']}) — restored empty."
                )
        return restored, warnings

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
