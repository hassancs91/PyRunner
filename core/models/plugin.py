"""
Plugin registry model.

Tracks installed plugins and their lifecycle state. The DB is the source of
truth for *what should load*; the filesystem (``PLUGINS_DIR``) is the source of
truth for *what code exists*. Only ``status=ACTIVE`` plugins are ever imported
into the running process (see the guarded loader in ``pyrunner/settings.py``).
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from core.plugins import is_valid_plugin_slug


def validate_plugin_slug(value: str) -> None:
    if not is_valid_plugin_slug(value):
        raise ValidationError(
            "Slug must start with a lowercase letter and contain only "
            "lowercase letters, digits, and underscores."
        )


class Plugin(models.Model):
    """A plugin known to PyRunner (its files live on the data volume)."""

    class Status(models.TextChoices):
        INSTALLED = "installed", "Installed"  # files on disk, not loaded
        ACTIVE = "active", "Active"  # loaded into the running process
        DISABLED = "disabled", "Disabled"  # deactivated by an admin (data kept)
        ERRORED = "errored", "Errored"  # failed preflight/boot; quarantined

    class Source(models.TextChoices):
        UPLOAD = "upload", "Upload"
        GIT = "git", "Git"
        BUILTIN = "builtin", "Built-in"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    slug = models.CharField(
        max_length=100,
        unique=True,
        validators=[validate_plugin_slug],
        help_text="Matches the plugin folder name and its app_name.",
    )
    name = models.CharField(max_length=200)
    version = models.CharField(max_length=50, blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INSTALLED,
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.UPLOAD,
    )

    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Last preflight/boot failure (shown in the UI).",
    )
    manifest = models.JSONField(
        default=dict,
        blank=True,
        help_text="Parsed plugin.json contents.",
    )
    checksum = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Checksum of the unpacked folder (integrity check).",
    )

    installed_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins"
        ordering = ["name"]
        verbose_name = "plugin"
        verbose_name_plural = "plugins"

    def __str__(self):
        return f"{self.name} ({self.slug}) [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    # ------------------------------------------------------------- metadata
    # Plugin Platform v2 Stage 6: the manifest (plugin.json) is the single carrier
    # for marketplace-prep metadata. It is stored verbatim in ``manifest`` on
    # install, so these read-only accessors expose new fields with NO migration.
    # Every field is optional; missing ⇒ "" / [] / {} (the console renders calmly).

    def manifest_value(self, key, default=""):
        """Read a key from the manifest JSONField (the Stage 6 metadata carrier)."""
        manifest = self.manifest if isinstance(self.manifest, dict) else {}
        value = manifest.get(key, default)
        return value if value is not None else default

    @property
    def summary(self) -> str:
        """Short tagline for cards (falls back to the long description)."""
        return self.manifest_value("summary") or self.manifest_value("description")

    @property
    def description(self) -> str:
        return self.manifest_value("description")

    @property
    def author(self) -> str:
        return self.manifest_value("author")

    @property
    def author_url(self) -> str:
        return self.manifest_value("author_url")

    @property
    def publisher(self) -> str:
        return self.manifest_value("publisher")

    @property
    def license(self) -> str:
        return self.manifest_value("license")

    @property
    def homepage(self) -> str:
        return self.manifest_value("homepage")

    @property
    def repository(self) -> str:
        return self.manifest_value("repository")

    @property
    def documentation(self) -> str:
        return self.manifest_value("documentation")

    @property
    def categories(self) -> list:
        value = self.manifest_value("categories", [])
        return [str(c) for c in value] if isinstance(value, list) else []

    @property
    def keywords(self) -> list:
        value = self.manifest_value("keywords", [])
        return [str(k) for k in value] if isinstance(value, list) else []

    @property
    def has_icon(self) -> bool:
        """True when the manifest declares a bundled icon file path."""
        return bool(self.manifest_value("icon"))

    @property
    def icon_fallback(self) -> str:
        """Emoji shown when there's no bundled icon (or it fails to load)."""
        return self.manifest_value("icon_fallback")

    @property
    def icon_url(self):
        """URL of the bundled-icon serve view, or None when no icon is declared."""
        if not self.has_icon:
            return None
        from django.urls import reverse

        return reverse("cpanel:plugin_icon", args=[self.slug])

    @property
    def provisions(self) -> dict:
        """Declared resources the plugin creates (counts + secret_keys)."""
        value = self.manifest_value("provisions", {})
        return value if isinstance(value, dict) else {}

    @property
    def provisions_summary(self) -> str:
        """Human one-liner, e.g. '1 script, 3 secrets, 1 schedule' (or '')."""
        p = self.provisions
        parts = []
        for key, label in (
            ("scripts", "script"),
            ("secrets", "secret"),
            ("datastores", "data store"),
            ("schedules", "schedule"),
        ):
            n = p.get(key)
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                parts.append(f"{n} {label}{'' if n == 1 else 's'}")
        return ", ".join(parts)

    def mark_errored(self, message: str) -> None:
        """Quarantine this plugin so the next boot will not load it."""
        self.status = self.Status.ERRORED
        self.error_message = message or "Plugin failed preflight."
        self.save(update_fields=["status", "error_message", "updated_at"])
