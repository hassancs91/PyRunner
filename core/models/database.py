"""
Database models — managed Postgres schemas for scripts & plugins.

A ``Database`` row is PyRun's handle on one provisioned Postgres schema plus
its dedicated login role on the data server (``PYRUNNER_DATA_DB_URL``). The row
stores the derived ``schema_name``/``role_name`` (never re-derived — Postgres
identifiers are capped at 63 chars and the derivation may truncate) and the
role's password Fernet-encrypted, exactly like ``Secret`` values.

Isolation is enforced by Postgres itself: the role owns its schema and nothing
else, so granted scripts get full DDL+DML inside it and cannot reach any other
schema — or the core database, which lives elsewhere entirely.

Access is granted per script through ``DatabaseGrant`` rows (mirroring
``SecretGrant``): the internal loopback API only hands a database's scoped DSN
to a run whose script holds an active grant.
"""

import uuid

from django.conf import settings
from django.db import models

from .workspace import Workspace, WorkspaceScopedManager


class Database(models.Model):
    """A provisioned Postgres schema + role on the data server."""

    STATUS_PROVISIONING = "provisioning"
    STATUS_READY = "ready"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_PROVISIONING, "Provisioning"),
        (STATUS_READY, "Ready"),
        (STATUS_ERROR, "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ``name`` is unique PER WORKSPACE (tenancy Decision 2B, same as DataStore):
    # every by-name resolver scopes the lookup to a workspace.
    name = models.CharField(
        max_length=100,
        help_text="Name for this database (used in scripts), unique per workspace",
    )

    # Tenancy: nullable + backfilled semantics identical to DataStore/Secret;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="databases",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Plugin ownership (same contract as DataStore/Secret): grouping/cleanup
    # metadata only, never part of the uniqueness key. The SDK (Stage 2) avoids
    # collisions by auto-naming "<owner_plugin>:<owner_key>".
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this database (NULL = user/system).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    # Provisioned server-side identifiers. Stored verbatim at provision time and
    # NEVER re-derived: the derivation truncates to Postgres's 63-char identifier
    # limit, so recomputing after a rename could point at the wrong objects.
    # Globally unique — schemas/roles live in one server-side namespace.
    schema_name = models.CharField(
        max_length=63,
        unique=True,
        help_text="Postgres schema this database maps to (server-side identifier).",
    )
    role_name = models.CharField(
        max_length=63,
        unique=True,
        help_text="Postgres login role that owns the schema (server-side identifier).",
    )

    # The role's password, Fernet-encrypted (same discipline as Secret values).
    encrypted_password = models.TextField(
        help_text="Fernet-encrypted password of the database role",
    )

    description = models.TextField(
        blank=True,
        help_text="Optional description of what this database is used for",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PROVISIONING,
        help_text="Provisioning state on the data server.",
    )
    # Human-readable cause when status == error (shown on the detail page next
    # to the Retry button). Cleared on a successful provision.
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_databases",
    )

    class Meta:
        db_table = "databases"
        verbose_name = "database"
        verbose_name_plural = "databases"
        ordering = ["name"]
        constraints = [
            # Per-workspace uniqueness (NULLs are SQL-distinct, so the partial
            # constraint below reproduces "globally unique among un-scoped rows").
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_database_workspace_name",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(workspace__isnull=True),
                name="uniq_database_name_when_no_workspace",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_ready(self) -> bool:
        return self.status == self.STATUS_READY

    def set_password(self, plaintext: str) -> None:
        """Encrypt and store the role's password."""
        from core.services import EncryptionService

        self.encrypted_password = EncryptionService.encrypt(plaintext)

    def get_password(self) -> str:
        """Return the decrypted role password."""
        from core.services import EncryptionService

        return EncryptionService.decrypt(self.encrypted_password)

    @classmethod
    def resolve_for_workspace(cls, name: str, workspace_id):
        """Resolve a database by ``name`` within a workspace.

        Same contract as ``DataStore.resolve_for_workspace`` (tenancy Decision
        2B): an un-scoped lookup defaults to the default workspace, then falls
        back to a still-unassigned (``workspace IS NULL``) row. Raises
        ``Database.DoesNotExist`` if neither exists. The workspace source is
        always trusted/server-derived, never a value the script supplied.
        """
        if workspace_id is None:
            default = Workspace.get_default()
            workspace_id = default.id if default else None

        if workspace_id is not None:
            try:
                return cls.objects.get(name=name, workspace_id=workspace_id)
            except cls.DoesNotExist:
                pass
        return cls.objects.get(name=name, workspace__isnull=True)


class DatabaseGrant(models.Model):
    """A per-database grant attaching one Database to one Script.

    Mirrors ``SecretGrant``, but access is EXPLICIT-ONLY: there is no
    injection_mode='all' equivalent — a script can resolve a database's
    credentials iff an active grant row exists. Grants are managed on the
    database detail page (Owner/Admin) and, later, by the plugin SDK.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        "core.Script",
        on_delete=models.CASCADE,
        related_name="database_grants",
    )
    database = models.ForeignKey(
        "core.Database",
        on_delete=models.CASCADE,
        related_name="grants",
    )
    # An inactive grant is retained (for UI/history) but never resolvable — the
    # internal API filters on active=True.
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "database_grants"
        verbose_name = "database grant"
        verbose_name_plural = "database grants"
        constraints = [
            models.UniqueConstraint(
                fields=["script", "database"], name="uniq_database_grant"
            ),
        ]

    def __str__(self):
        return f"{self.script_id} → {self.database_id}{'' if self.active else ' (inactive)'}"
