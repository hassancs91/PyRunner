"""
API Token model for external HTTP API access (datastores + plugin APIs).
"""

import secrets
import uuid

from django.conf import settings
from django.db import models


class APIToken(models.Model):
    """
    API token for accessing PyRunner data via the external REST API.

    Scopes (least privilege, one scope per token):
    - ``datastore``  — a single datastore (the FK).
    - ``datastores`` — all datastores in the token's workspace (legacy "global").
    - ``plugin``     — one plugin's declared API resources (``plugin_slug``).

    Historically named ``DataStoreAPIToken`` (the alias survives in
    ``core.models``); the table name is pinned so the rename touched no SQL.
    """

    class Scope(models.TextChoices):
        DATASTORE = "datastore", "Single datastore"
        DATASTORES = "datastores", "All datastores"
        PLUGIN = "plugin", "Plugin API"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The actual token value (64-char URL-safe string)
    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="API token value (auto-generated)",
    )

    # Friendly name to identify this token
    name = models.CharField(
        max_length=100,
        help_text="Friendly name for this token",
    )

    # What this token can reach. Enforced by CheckConstraints below:
    # scope="datastore" ⇔ the datastore FK is set; scope="plugin" ⇔ plugin_slug
    # is non-empty. A legacy pre-scope row was backfilled from its FK.
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.DATASTORES,
        help_text="What this token grants access to",
    )

    # Optional: Restrict to specific datastore (null = access to all datastores)
    datastore = models.ForeignKey(
        "DataStore",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_tokens",
        help_text="If set, token only grants access to this datastore. Leave empty for global access.",
    )

    # Plugin scope target. Deliberately NOT an FK: plugins aren't rows (dev-mode
    # plugins have no Plugin row at all); validated against loaded plugins at
    # creation time only. A token for a later-removed plugin 404s at dispatch.
    plugin_slug = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Plugin this token is scoped to (scope=plugin only)",
    )

    # Tenancy Stage 3: the workspace this token acts in. A global (no-datastore)
    # token lists/resolves only this workspace's datastores; NULL falls back to
    # the default workspace (today's behavior on a single-workspace instance).
    # Plugin-scoped tokens are FAIL-CLOSED instead: NULL workspace → 403.
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="api_tokens",
        help_text="Workspace this token is scoped to (tenancy; nullable).",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this token was used",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional expiration date. Leave empty for no expiration.",
    )

    # Who created this token
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_api_tokens",
    )

    # Active flag for soft-disable
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive tokens cannot be used for API access",
    )

    class Meta:
        db_table = "datastore_api_tokens"
        verbose_name = "API token"
        verbose_name_plural = "API tokens"
        ordering = ["-created_at"]
        constraints = [
            # scope="datastore" ⇔ datastore FK set (integrity, not just form
            # validation — a plugin/global token must never carry a store FK).
            models.CheckConstraint(
                condition=(
                    models.Q(scope="datastore", datastore__isnull=False)
                    | (~models.Q(scope="datastore") & models.Q(datastore__isnull=True))
                ),
                name="apitoken_scope_datastore_fk",
            ),
            # scope="plugin" ⇔ plugin_slug non-empty.
            models.CheckConstraint(
                condition=(
                    (models.Q(scope="plugin") & ~models.Q(plugin_slug=""))
                    | (~models.Q(scope="plugin") & models.Q(plugin_slug=""))
                ),
                name="apitoken_scope_plugin_slug",
            ),
        ]

    def __str__(self):
        if self.scope == self.Scope.PLUGIN:
            return f"{self.name} (plugin: {self.plugin_slug})"
        if self.datastore:
            return f"{self.name} ({self.datastore.name})"
        return f"{self.name} (global)"

    def save(self, *args, **kwargs):
        # Legacy creation sites set only the datastore FK (pre-scope shape);
        # derive the single-store scope so those callers keep working unchanged.
        # An explicit plugin scope with an FK still hits the CheckConstraint.
        if self.datastore_id and self.scope == self.Scope.DATASTORES:
            self.scope = self.Scope.DATASTORE
        super().save(*args, **kwargs)

    @staticmethod
    def generate_token() -> str:
        """Generate a secure random API token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)

    def get_masked_token(self) -> str:
        """Return a masked version of the token for display."""
        if len(self.token) <= 12:
            return "*" * len(self.token)
        return f"{self.token[:8]}...{self.token[-4:]}"

    @property
    def is_global(self) -> bool:
        """Return True if this is a global token (not scoped to a datastore)."""
        return self.datastore is None

    @property
    def scope_display(self) -> str:
        """Return a human-readable scope description."""
        if self.scope == self.Scope.PLUGIN:
            return f"Plugin: {self.plugin_slug}"
        if self.datastore:
            return f"Datastore: {self.datastore.name}"
        return "All datastores"


# Historical name — kept importable so existing code and migrations referencing
# the old class keep working (the rename was state-only; table name unchanged).
DataStoreAPIToken = APIToken
