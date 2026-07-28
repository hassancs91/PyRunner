"""
PluginPublicPage — capability-URL shares of plugin-rendered pages (/p/<token>/).
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class PluginPublicPage(models.Model):
    """One shared public page of a plugin, addressed by an unguessable token.

    The /webhook/<token>/ idiom: anyone with the link reads the page — that's
    the feature; the controls are revocation (permanent: a re-share after
    revoke/expiry ROTATES the token, a dead URL never resurrects), optional
    expiry, and the noindex/no-referrer/no-store headers the view sends.

    ``workspace`` is CASCADE on purpose (fail-closed): a SET_NULL +
    default-workspace fallback would make a deleted workspace's share links
    serve the default workspace's data. A share link dies with its workspace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Capability token routing /p/<token>/ (auto-generated).",
    )

    plugin_slug = models.CharField(max_length=100, db_index=True)

    # The declared page name (plugin.json "provides.public_pages").
    page = models.CharField(max_length=100)

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="plugin_public_pages",
        help_text="Workspace whose data the page renders (share dies with it).",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Revoked pages 404; re-sharing rotates the token.",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional expiry. An expired link 404s and never comes back.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_public_pages",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Write-throttled by the view (at most one UPDATE per 60s): an anonymous
    # public hit must not be a guaranteed DB write.
    last_accessed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "plugin_public_pages"
        verbose_name = "plugin public page"
        verbose_name_plural = "plugin public pages"
        ordering = ["plugin_slug", "page"]
        constraints = [
            # share() is idempotent per (plugin, page, workspace).
            models.UniqueConstraint(
                fields=["plugin_slug", "page", "workspace"],
                name="uniq_public_page_per_ws",
            ),
        ]

    def __str__(self):
        return f"{self.plugin_slug}:{self.page} ({self.workspace_id})"

    @staticmethod
    def generate_token() -> str:
        """Unguessable capability token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at < timezone.now())

    @property
    def url_path(self) -> str:
        return f"/p/{self.token}/"
