"""
Workspace model — the tenancy seam (FOUNDATIONS Seam 1).

Phase A lands the SEAM ONLY: this model + a nullable ``workspace`` FK on the
scoped models + a default-workspace backfill. There is deliberately NO
query-scoping sweep and NO UI yet — every existing query is unchanged and a
single-workspace instance behaves exactly like today. The eventual multi-tenant
flip becomes a scoping sweep, not a schema rewrite.

``WorkspaceMembership`` (user × workspace × role) pairs with future RBAC and is
intentionally out of scope here.
"""

import uuid

from django.db import models


class Workspace(models.Model):
    """A tenancy boundary that scoped resources belong to.

    In Phase A there is exactly one — the default workspace the backfill creates
    — and nothing filters by it. It exists so new code can be written
    workspace-aware and the rows already carry the column.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, default="Default Workspace")
    is_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text="The fallback workspace assigned to existing/un-scoped resources.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces"
        verbose_name = "workspace"
        verbose_name_plural = "workspaces"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name}{' (default)' if self.is_default else ''}"

    def save(self, *args, **kwargs):
        # Mirror Environment: at most one row is the default.
        if self.is_default:
            Workspace.objects.filter(is_default=True).exclude(pk=self.pk).update(
                is_default=False
            )
        super().save(*args, **kwargs)

    @classmethod
    def get_default(cls):
        """Return the default workspace (the one the backfill created), or None."""
        return cls.objects.filter(is_default=True).order_by("created_at").first()
