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

from django.conf import settings
from django.db import models


class WorkspaceScopedQuerySet(models.QuerySet):
    """QuerySet for the six scoped models, adding the tenancy sweep's primitive.

    ``Model.objects.for_workspace(ws)`` is the single, greppable way every
    list/service query narrows to the active workspace once the Stage 3 sweep
    lands. Added now (unused) so the eventual flip is mechanical. Managers are
    not serialized into migrations (``use_in_migrations`` defaults False), so
    attaching this is a behavior-only, drift-free change.
    """

    def for_workspace(self, workspace):
        return self.filter(workspace=workspace)


# Default manager that keeps all standard behavior and adds ``for_workspace``.
WorkspaceScopedManager = models.Manager.from_queryset(WorkspaceScopedQuerySet)


class Workspace(models.Model):
    """A tenancy boundary that scoped resources belong to.

    In Phase A there is exactly one — the default workspace the backfill creates
    — and nothing filters by it. It exists so new code can be written
    workspace-aware and the rows already carry the column.
    """

    # Per-workspace execution-isolation policy (sandbox Stage 3). Null = inherit
    # the instance default (GlobalSettings.sandbox_default). An Owner/Admin can
    # only TIGHTEN toward 'required' relative to the instance default; it never
    # weakens the instance floor (resolve_isolation takes the stricter of the two).
    class SandboxPolicy(models.TextChoices):
        OFF = "off", "Off"
        OPTIONAL = "optional", "Optional"
        REQUIRED = "required", "Required"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, default="Default Workspace")
    is_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text="The fallback workspace assigned to existing/un-scoped resources.",
    )
    sandbox_policy = models.CharField(
        max_length=20,
        choices=SandboxPolicy.choices,
        null=True,
        blank=True,
        help_text="Execution-isolation policy for this workspace. Blank = inherit "
        "the instance default. Can only tighten toward 'required'.",
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

    @classmethod
    def for_user(cls, user):
        """Workspaces the user may act in. Superusers see all; anonymous sees none."""
        if user is None or not getattr(user, "is_authenticated", False):
            return cls.objects.none()
        if user.is_superuser:
            return cls.objects.all()
        return cls.objects.filter(memberships__user=user).distinct()

    @classmethod
    def resolve_for(cls, user, requested_id=None):
        """Resolve the active workspace for a request.

        Returns ``(workspace, ok)``:
        - ``requested_id`` given and the user may access it ⇒ ``(ws, True)``.
        - ``requested_id`` given but unknown / not a member ⇒ ``(None, False)``
          (the middleware turns this into a 404 — the URL is never trusted).
        - no ``requested_id`` ⇒ the user's default workspace ⇒ ``(ws, True)``
          (resolve-in-place; never a redirect).
        """
        if requested_id is not None:
            ws = cls.objects.filter(pk=requested_id).first()
            if ws is None:
                return None, False
            if user is not None and user.is_superuser:
                return ws, True
            is_member = WorkspaceMembership.objects.filter(
                user=user, workspace=ws
            ).exists()
            return (ws, True) if is_member else (None, False)

        # No explicit request: pick the user's default-ish workspace.
        if user is not None and user.is_superuser:
            return cls.get_default(), True
        member_ws = cls.for_user(user)
        default = cls.get_default()
        if default is not None and member_ws.filter(pk=default.pk).exists():
            return default, True
        return member_ws.order_by("-is_default", "name").first(), True


class WorkspaceMembership(models.Model):
    """A user's membership in a workspace, with a role (RBAC, Decision 4).

    This is the *only* source of "which workspaces a user may act in" — there is
    no per-user ownership on the scoped resources. The Phase-A backfill seeds one
    membership per existing user in the default workspace; the security sweep
    keys isolation off membership, and roles gate management actions in a later
    stage. ``role`` is present now so the column never needs a later migration,
    but Stage 0 does not yet enforce it.
    """

    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_MEMBER, "Member"),
    ]
    # Roles allowed to manage the workspace (members, rename, delete).
    MANAGE_ROLES = (ROLE_OWNER, ROLE_ADMIN)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_MEMBER,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspace_memberships"
        verbose_name = "workspace membership"
        verbose_name_plural = "workspace memberships"
        ordering = ["workspace__name", "user__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "workspace"], name="uniq_user_workspace"
            ),
        ]

    def __str__(self):
        return f"{self.user} → {self.workspace} ({self.role})"

    @property
    def can_manage(self) -> bool:
        """Whether this role may manage the workspace (members/rename/delete)."""
        return self.role in self.MANAGE_ROLES

    @classmethod
    def ensure(cls, user, workspace=None, role=ROLE_MEMBER):
        """Idempotently ensure ``user`` is a member of ``workspace``.

        Used by the backfill and the new-user hook so every user always has at
        least one membership (the middleware needs one to resolve a workspace).
        If the membership exists, the role is upgraded toward owner but never
        silently downgraded. Returns the membership, or ``None`` if there is no
        workspace to attach to yet (very early setup, before the backfill).
        """
        if workspace is None:
            workspace = Workspace.get_default()
        if workspace is None or user is None:
            return None

        membership, created = cls.objects.get_or_create(
            user=user, workspace=workspace, defaults={"role": role}
        )
        if not created and role == cls.ROLE_OWNER and membership.role != cls.ROLE_OWNER:
            membership.role = cls.ROLE_OWNER
            membership.save(update_fields=["role", "updated_at"])
        return membership
