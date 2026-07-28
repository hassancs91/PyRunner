"""
Object-storage connections.

One row per configured S3-compatible endpoint (Cloudflare R2, AWS S3, Backblaze
B2, DigitalOcean Spaces, MinIO, or any custom S3-compatible provider). All rows
persist their credentials (encrypted), so an instance can hold several buckets
at once — historically PyRunner had exactly one, wired to backups.

Two roles, resolved independently:

- **Backups** use the row flagged ``is_default`` (``StorageConnection.get_default()``).
  The pre-multi-connection single config data-migrates into exactly that row, so
  upgrading changes nothing about how backups behave.
- **Plugin assets** use ``GlobalSettings.assets_storage``, which has NO fallback to
  the default. Blank means the storage seam is simply unavailable
  (``StorageAPI.is_available()`` is False). That is deliberate: falling back would
  quietly write plugin objects into the private *backup* bucket.

Mirrors the ``AIProvider`` shape (many credentialed profiles + a GlobalSettings FK
selecting the active one), including the code-level presets table below.
"""

import uuid

from django.db import models, transaction


class StorageConnection(models.Model):
    class ProviderType(models.TextChoices):
        R2 = "r2", "Cloudflare R2"
        S3 = "s3", "AWS S3"
        B2 = "b2", "Backblaze B2"
        SPACES = "spaces", "DigitalOcean Spaces"
        MINIO = "minio", "MinIO / self-hosted"
        CUSTOM = "custom", "Custom S3-compatible"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_type = models.CharField(
        max_length=20,
        choices=ProviderType.choices,
        default=ProviderType.CUSTOM,
        help_text="Which S3-compatible backend this connection talks to",
    )
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Display name, e.g. 'Backups (R2)' or 'Public assets'",
    )
    endpoint_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="S3 endpoint URL (leave empty for AWS S3)",
    )
    region = models.CharField(
        max_length=50,
        blank=True,
        default="us-east-1",
        help_text="S3 region ('auto' for R2)",
    )
    bucket = models.CharField(
        max_length=255,
        blank=True,
        help_text="Bucket name",
    )
    access_key_encrypted = models.TextField(
        blank=True,
        help_text="Access key ID (encrypted)",
    )
    secret_key_encrypted = models.TextField(
        blank=True,
        help_text="Secret access key (encrypted)",
    )
    use_ssl = models.BooleanField(
        default=True,
        help_text="Use SSL/TLS for connections",
    )
    path_style = models.BooleanField(
        default=False,
        help_text="Use path-style addressing (required for MinIO)",
    )
    public_base_url = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            "Public base URL for hot-linkable objects, e.g. an R2 public domain. "
            "Blank = objects are served via presigned URLs that EXPIRE."
        ),
    )
    is_default = models.BooleanField(
        default=False,
        help_text="The connection backups use. Exactly one row carries this.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Switch off without losing the configuration",
    )
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this connection last tested successfully",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "storage_connections"
        ordering = ["name"]
        verbose_name = "storage connection"
        verbose_name_plural = "storage connections"

    def __str__(self):
        return f"{self.name} ({self.get_provider_type_display()})"

    def save(self, *args, **kwargs):
        """Persist, enforcing that at most one row is ``is_default``.

        Enforced here rather than in the form because the backup path resolves the
        default on every scheduled run — a second default would make which bucket
        gets the backup depend on row ordering.
        """
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_default:
                StorageConnection.objects.filter(is_default=True).exclude(
                    pk=self.pk
                ).update(is_default=False)

    @classmethod
    def get_default(cls):
        """The connection backups use, or None when nothing is configured."""
        return cls.objects.filter(is_default=True).first()

    @property
    def is_configured(self) -> bool:
        """Whether this row has everything needed to build a client."""
        return bool(
            self.bucket and self.access_key_encrypted and self.secret_key_encrypted
        )

    @property
    def is_public(self) -> bool:
        """Whether objects get permanent URLs instead of expiring presigned ones."""
        return bool(self.public_base_url)


# Static per-type defaults and UI hints, all consumed by the Services → Object
# Storage card's JS. Code-level by design — the DB stores only what the user
# entered; changing a preset here updates all instances. Mirrors
# ``PROVIDER_PRESETS`` in ai_provider.py.
#
# No "label" key: the display name comes from ``ProviderType.choices``
# (``get_provider_type_display``), and a second copy here would be one to drift.
STORAGE_PRESETS = {
    StorageConnection.ProviderType.R2: {
        "endpoint_placeholder": "https://<account-id>.r2.cloudflarestorage.com",
        "region_default": "auto",
        "path_style_default": False,
        "docs_url": "https://developers.cloudflare.com/r2/api/s3/api/",
        "notes": "Zero egress fees. For hot-linking, enable a public r2.dev domain or a "
        "custom domain and paste it as the public base URL.",
    },
    StorageConnection.ProviderType.S3: {
        "endpoint_placeholder": "(leave empty for AWS)",
        "region_default": "us-east-1",
        "path_style_default": False,
        "docs_url": "https://docs.aws.amazon.com/s3/",
        "notes": "Leave the endpoint blank; boto3 derives it from the region.",
    },
    StorageConnection.ProviderType.B2: {
        "endpoint_placeholder": "https://s3.<region>.backblazeb2.com",
        "region_default": "us-west-004",
        "path_style_default": False,
        "docs_url": "https://www.backblaze.com/docs/cloud-storage-s3-compatible-api",
        "notes": "Use an application key, not the master key.",
    },
    StorageConnection.ProviderType.SPACES: {
        "endpoint_placeholder": "https://<region>.digitaloceanspaces.com",
        "region_default": "nyc3",
        "path_style_default": False,
        "docs_url": "https://docs.digitalocean.com/products/spaces/",
        "notes": "The CDN endpoint makes a good public base URL.",
    },
    StorageConnection.ProviderType.MINIO: {
        "endpoint_placeholder": "https://minio.example.com",
        "region_default": "us-east-1",
        "path_style_default": True,
        "docs_url": "https://min.io/docs/minio/linux/developers/python/API.html",
        "notes": "Must be reachable at a PUBLIC hostname — endpoints resolving to "
        "localhost or a private IP are rejected as SSRF targets.",
    },
    StorageConnection.ProviderType.CUSTOM: {
        "endpoint_placeholder": "https://s3.example.com",
        "region_default": "us-east-1",
        "path_style_default": False,
        "docs_url": "",
        "notes": "Any S3-compatible endpoint (Wasabi, Scaleway, Ceph, …).",
    },
}
