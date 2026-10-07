"""
Vector collections (vector seam).

A ``VectorCollection`` is a workspace-scoped, named set of points (vector +
payload + optional text) living on one ``VectorConnection``. Shaped like
``Database``: tenancy through ``workspace`` + ``WorkspaceScopedManager``, plugin
ownership through ``owner_plugin``/``owner_key``, ``status``/``last_error`` for
the backend handle. The embedding provider is optional — a vectors-only
collection takes vectors from the caller — but when set, the collection **pins**
``embedding_model`` and ``dimension`` at creation: changing the provider's model
later is a new collection + re-embed, never a silent change of space.

``backend_ref`` is the physical handle: ``vectors/<uuid>.sqlite`` under the data
volume for the Local backend, ``<prefix>pyrun_<ws8>_<slug>`` for Qdrant.
"""

import re
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from .workspace import WorkspaceScopedManager

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
NAME_RULE = "lowercase letters, digits, '_' and '-', starting with a letter (max 64)"


class VectorCollection(models.Model):
    STATUS_READY = "ready"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [(STATUS_READY, "Ready"), (STATUS_ERROR, "Error")]

    class Metric(models.TextChoices):
        COSINE = "cosine", "Cosine"
        DOT = "dot", "Dot product"
        EUCLID = "euclid", "Euclidean"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Unique PER WORKSPACE (tenancy Decision 2B, like Database/DataStore).
    name = models.CharField(
        max_length=64,
        help_text=f"Name used by scripts to open the collection ({NAME_RULE}); unique per workspace",
    )
    description = models.TextField(blank=True, default="")

    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="vector_collections",
        help_text="Workspace this collection belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Plugin ownership (same contract as Database/DataStore/Secret): grouping and
    # cleanup metadata only, never part of the uniqueness key.
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this collection (NULL = user/system).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    connection = models.ForeignKey(
        "core.VectorConnection",
        on_delete=models.PROTECT,
        related_name="collections",
        help_text="The vector store this collection lives on",
    )
    embedding_provider = models.ForeignKey(
        "core.EmbeddingProvider",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="collections",
        help_text="Provider used by add_texts()/search(); blank = vectors-only collection",
    )
    embedding_model = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="The provider's model at creation time — pinned; a different model is a new collection",
    )
    dimension = models.PositiveIntegerField(
        help_text="Vector size, pinned at creation (from the provider's recorded dimension)",
    )
    metric = models.CharField(
        max_length=10,
        choices=Metric.choices,
        default=Metric.COSINE,
    )
    backend_ref = models.CharField(
        max_length=255,
        help_text="Physical handle: data-relative SQLite path (local) or the Qdrant collection name",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_READY,
        help_text="Backend handle state.",
    )
    last_error = models.TextField(blank=True, default="")

    # Cost tracking v1: counters on the row (a usage page is deferred).
    points_count = models.BigIntegerField(default=0)
    tokens_embedded = models.BigIntegerField(default=0)
    embed_calls = models.BigIntegerField(default=0)

    include_in_backup = models.BooleanField(
        default=True,
        help_text="Carry this collection's points in backups (Local backend only; size shown in the console)",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_vector_collections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vector_collections"
        verbose_name = "vector collection"
        verbose_name_plural = "vector collections"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_vector_collection_workspace_name",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(workspace__isnull=True),
                name="uniq_vector_collection_name_when_no_workspace",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_ready(self) -> bool:
        return self.status == self.STATUS_READY

    @property
    def is_local(self) -> bool:
        return self.connection.is_local

    @property
    def has_embeddings(self) -> bool:
        return self.embedding_provider_id is not None

    @property
    def file_path(self):
        """Absolute path of the Local backend's SQLite file (``None`` for other backends)."""
        if not self.backend_ref.startswith("vectors/"):
            return None
        # ``backend_ref`` is the portable handle (data-relative); the directory it
        # resolves under is ``VECTORS_DIR`` so tests and operators can move it.
        return Path(settings.VECTORS_DIR) / self.backend_ref.split("/", 1)[1]

    @staticmethod
    def local_backend_ref(collection_id) -> str:
        return f"vectors/{collection_id}.sqlite"

    @classmethod
    def resolve_for_workspace(cls, name: str, workspace_id):
        """Resolve a collection by ``name`` within a workspace (same contract as Database)."""
        from .workspace import Workspace

        if workspace_id is None:
            default = Workspace.get_default()
            workspace_id = default.id if default else None
        if workspace_id is not None:
            try:
                return cls.objects.get(name=name, workspace_id=workspace_id)
            except cls.DoesNotExist:
                pass
        return cls.objects.get(name=name, workspace__isnull=True)
