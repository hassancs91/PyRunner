"""
Library models — shared multi-module Python code for scripts & plugins.

A ``Library`` is a named, workspace-scoped set of Python modules that scripts
*attach* and import at run time. It is the Secrets grammar applied to code:
create the resource → attach it to a script → it is injected (here: materialized
onto ``PYTHONPATH``) when the script runs. That shape is what lets N per-account
plugin worker scripts share ONE pipeline library instead of duplicating it.

Content does NOT live on ``Library``. The single source of truth is
``LibraryRevision``: every content-changing save writes a new immutable revision
and bumps ``Library.current_version``. A ``Run`` stamps ``{key: version}`` at
queue time and the executor materializes THAT revision — so editing a library
after a run is queued can never change what the queued run imports, and history
is append-only (Restore = a new revision carrying old content).

Storage scales with EDITS, not runs: a full per-run code snapshot of a ~4k-line
library would add ~140 MB/yr/account of duplicated text to the core (often
SQLite) DB, which is why the version stamp is a tiny int map instead.
"""

import hashlib
import json
import re
import sys
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from .workspace import WorkspaceScopedManager

# A library ``key`` is a Python import identifier — it becomes the package
# directory name on PYTHONPATH, so it must be a legal (lowercase) module name.
LIBRARY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Module filenames are FLAT (no path separators): v1 materializes a single-level
# package. Subpackages can come later without breaking this rule.
MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")

# A library's key becomes a package directory on PYTHONPATH, which Python
# searches BEFORE the standard library — so an innocently-named library ('email',
# 'types', 'secrets', 'logging', 'queue') would silently hijack `import email` for
# every script it is attached to, including from inside third-party packages. The
# break surfaces far from its cause, so the name is refused up front instead.
#
# Two reserved sets, both fences behind PYTHONPATH ordering (helpers resolve
# first): PyRunner's own helper prefix, and every stdlib top-level module. These
# are exactly the two namespaces PyRunner ships and can therefore guarantee —
# venv package names are unbounded and deliberately not policed here.
RESERVED_KEY_PREFIX = "pyrunner"
STDLIB_MODULE_NAMES = getattr(sys, "stdlib_module_names", frozenset())

# Sanity caps, not economy: a library is shared code, not a file store.
MAX_MODULES = 64
MAX_REVISION_BYTES = 512 * 1024

library_key_validator = RegexValidator(
    regex=LIBRARY_KEY_RE,
    message=(
        "Key must be a valid Python module name: lowercase letter first, then "
        "lowercase letters, digits or underscores (e.g. 'gmail_agent_lib')."
    ),
)


def validate_library_key(value: str) -> None:
    """Validate a library key as an import identifier + the reserved-name bans.

    Shared by the model field, the console form and the SDK so all three paths
    enforce the same rule (the SDK never goes through a form).
    """
    library_key_validator(value)
    if value.startswith(RESERVED_KEY_PREFIX):
        raise ValidationError(
            f"Key must not start with '{RESERVED_KEY_PREFIX}' — that prefix is "
            "reserved for PyRunner's built-in script helpers."
        )
    if value in STDLIB_MODULE_NAMES:
        raise ValidationError(
            f"Key '{value}' is the name of a Python standard-library module. A "
            "library with this key would shadow it in every script it is attached "
            f"to — pick something more specific, e.g. '{value}_lib'."
        )


def validate_modules(modules) -> None:
    """Validate a revision's ``{filename: code}`` map.

    Enforces flat, importable filenames and the sanity caps. Raises
    ``ValidationError`` with a specific cause — never a bare "invalid".
    """
    if not isinstance(modules, dict):
        raise ValidationError("Modules must be a mapping of {filename: code}.")
    if not modules:
        raise ValidationError("A library revision must contain at least one module.")
    if len(modules) > MAX_MODULES:
        raise ValidationError(
            f"Too many modules: {len(modules)} (maximum is {MAX_MODULES})."
        )

    total = 0
    for filename, code in modules.items():
        if not isinstance(filename, str) or not MODULE_NAME_RE.match(filename):
            raise ValidationError(
                f"Invalid module filename {filename!r}: must be a flat Python "
                "filename like 'helper.py' (no directories or path separators)."
            )
        if not isinstance(code, str):
            raise ValidationError(f"Module {filename!r}: code must be a string.")
        total += len(code.encode("utf-8"))

    if total > MAX_REVISION_BYTES:
        raise ValidationError(
            f"Library too large: {total} bytes (maximum is {MAX_REVISION_BYTES})."
        )


def hash_modules(modules: dict) -> str:
    """Stable content hash of a ``{filename: code}`` map.

    Canonical (sorted keys) so an unchanged library re-provisioned by a plugin
    hashes identically and writes NO new revision — idempotent provisioning must
    not churn history on every settings save.
    """
    canonical = json.dumps(modules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Library(models.Model):
    """A named set of Python modules that scripts attach and import."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The import identifier: ``from <key>.module import thing``. Unique PER
    # WORKSPACE (not per-owner like Secret.key) precisely BECAUSE it is a
    # directory name — two same-key libraries attached to one script would
    # collide in the staging dir. Per-workspace uniqueness makes that
    # unrepresentable; plugins avoid clashes by keying '<slug>_lib'.
    key = models.CharField(
        max_length=100,
        validators=[library_key_validator],
        help_text="Python import name for this library (e.g. 'gmail_agent_lib').",
    )

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # Tenancy: nullable + backfilled semantics identical to Script/Secret;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="libraries",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Plugin ownership (same contract as Script/Secret/Database): ``owner_plugin``
    # is a slug STRING (not an FK, survives plugin-row deletion); ``owner_key`` is
    # the SDK's stable handle for idempotent upsert on (owner_plugin, owner_key).
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this library (NULL = user-created).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    # Head revision number. Monotonic: Restore writes a NEW revision with old
    # content rather than moving this backwards, so a Run's stamped version
    # always resolves to the exact content that run imported.
    current_version = models.PositiveIntegerField(
        default=0,
        help_text="Version number of the head revision (0 = no content yet).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_libraries",
    )

    class Meta:
        db_table = "libraries"
        verbose_name = "library"
        verbose_name_plural = "libraries"
        ordering = ["key"]
        constraints = [
            # Per-workspace uniqueness (NULLs are SQL-distinct, so the partial
            # constraint below reproduces "globally unique among un-scoped rows").
            models.UniqueConstraint(
                fields=["workspace", "key"],
                name="uniq_library_workspace_key",
            ),
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(workspace__isnull=True),
                name="uniq_library_key_when_no_workspace",
            ),
        ]

    def __str__(self):
        return f"{self.key} [{self.owner_plugin}]" if self.owner_plugin else self.key

    def clean(self):
        super().clean()
        if self.key:
            validate_library_key(self.key)

    @property
    def head(self):
        """The current (head) revision, or None when the library has no content."""
        if not self.current_version:
            return None
        return self.revisions.filter(version=self.current_version).first()

    def revision(self, version: int):
        """Return an exact revision by version number, or None."""
        return self.revisions.filter(version=version).first()

    def save_revision(self, modules: dict, *, created_by=None):
        """Write ``modules`` as a new revision IFF the content actually changed.

        Returns ``(revision, created)``. ``created=False`` means the content was
        byte-identical to the head and nothing was written — the property that
        keeps idempotent plugin provisioning (which re-runs on every settings
        save) from turning revision history into noise.
        """
        validate_modules(modules)
        content_hash = hash_modules(modules)

        head = self.head
        if head is not None and head.content_hash == content_hash:
            return head, False

        revision = LibraryRevision.objects.create(
            library=self,
            version=self.current_version + 1,
            modules=modules,
            content_hash=content_hash,
            created_by=created_by,
        )
        self.current_version = revision.version
        self.save(update_fields=["current_version", "updated_at"])
        return revision, True


class LibraryRevision(models.Model):
    """One immutable content version of a Library.

    Append-only: revisions are never mutated or renumbered. This is what makes a
    Run's ``library_versions`` stamp meaningful forever — code_snapshot-grade
    reproducibility for shared code, at the cost of storage-per-edit only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    library = models.ForeignKey(
        "core.Library",
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    version = models.PositiveIntegerField()

    # {filename: code}. JSONField so the whole module set is one atomic row —
    # a revision is all-or-nothing, never a half-written package.
    modules = models.JSONField(
        default=dict,
        help_text="Module map {filename: code} for this revision.",
    )

    # sha256 of the canonical module map. Stored (not recomputed) so the
    # change-detection compare — which dev-mode plugins run on EVERY run queue —
    # is an indexed string compare instead of a full JSON load.
    content_hash = models.CharField(max_length=64, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_library_revisions",
    )

    class Meta:
        db_table = "library_revisions"
        verbose_name = "library revision"
        verbose_name_plural = "library revisions"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["library", "version"], name="uniq_library_revision_version"
            ),
        ]

    def __str__(self):
        return f"{self.library.key} v{self.version}"


class ScriptLibrary(models.Model):
    """Attaches one Library to one Script (the through table).

    Mirrors ``SecretGrant`` minus the ``active`` flag: a library is either
    attached or it isn't, and detach is a row delete. There is no injection_mode
    equivalent — libraries are EXPLICIT-ONLY, a script imports a library iff an
    attachment row exists.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        "core.Script",
        on_delete=models.CASCADE,
        related_name="library_attachments",
    )
    library = models.ForeignKey(
        "core.Library",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "script_libraries"
        verbose_name = "script library"
        verbose_name_plural = "script libraries"
        constraints = [
            models.UniqueConstraint(
                fields=["script", "library"], name="uniq_script_library"
            ),
        ]

    def __str__(self):
        return f"{self.script_id} → {self.library_id}"

    def clean(self):
        """A script may only attach libraries from its OWN workspace.

        Cross-workspace attachment would be a tenancy hole: it would let a script
        import code the workspace can neither see nor audit. Enforced here (forms
        + SDK both call full_clean) rather than by a DB constraint, which cannot
        span the two FKs' workspace columns.
        """
        super().clean()
        if self.script_id and self.library_id:
            if self.script.workspace_id != self.library.workspace_id:
                raise ValidationError(
                    f"Library '{self.library.key}' belongs to a different workspace "
                    f"than script '{self.script.name}'."
                )
