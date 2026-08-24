"""
core.plugins.api — the stable, versioned SDK facade for PyRunner plugins (WS2).

A plugin orchestrates PyRunner primitives (scripts, secrets, datastores,
schedules, environments, runs) THROUGH this module instead of importing
``core.models`` / ``core.tasks`` / ``core.services`` directly. The facade:

  * auto-stamps ownership — every resource a plugin creates carries the plugin's
    ``owner_plugin`` slug AND a ``workspace`` (the two scoping axes from the
    foundations), so it groups, filters, cleans up, and is delete-guarded;
  * is idempotent — ``upsert(...)`` keyed on ``(owner_plugin, owner_key)`` updates
    the same row instead of spawning duplicates on re-provision (the thing the
    qdrant reference plugin hand-rolled via a stored ``script_id``);
  * auto-names DataStores ``"<owner>:<key>"`` so two plugins never collide on the
    globally-/per-workspace-unique ``name`` while referring to a store by a short
    key, and injects owner-scoped secrets under their CLEAN name;
  * never bypasses the run/sandbox seams — runs go through ``queue_script_run``
    (→ RunBackend + ``resolve_isolation``); plugin scripts default to
    ``isolation_mode='inherit'`` so the DB isolation policy decides.

CONTRACT NOTES
- ``owner=None`` selects the LEGACY global/user lane (no ownership stamping), so a
  ported ``upsert_secret(key=...)`` keeps working and old plugins are never forced
  into ``injection_mode='selected'``.
- ``workspace=None`` resolves to the default workspace (the only authoritative,
  request-free source — same rule the scheduler uses).
- Every core import is LAZY (inside a function body), mirroring
  ``run_in_environment``, so importing this module never needs the app registry —
  the light-import boot guard stays intact and a plugin's ``apps.py`` can import
  the SDK without pulling in ``core.models``.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Plugins declare the SDK version they target in plugin.json (e.g. "2.0"); bump
# on a breaking change to the facade. The wrapped CORE services stay stable.
# 2.1 (additive): ScriptAPI run-lifecycle surface (latest_run/runs/
# cancel_latest_run + the RunView read-model).
# 2.2 (additive): DatabaseAPI — owner-scoped managed Postgres databases
# (provision/get/list/grant/dsn) on the attached data server.
# 2.3 (additive): external-API surface — @resource handler marker, APIRequest,
# APIError (docs/PLAN_plugin_api.md) — plus read-only workspace-scoped
# ChannelAPI.list(), and public pages (@page, PageRequest, PublicPageAPI).
# 2.4 (additive): LibraryAPI — shared multi-module code attached to scripts and
# materialized onto a run's PYTHONPATH, pinned to the revision stamped at queue
# time (docs/PLAN_script_libraries.md).
# 2.5 (additive): StorageAPI — owner-scoped object storage on the instance's
# assets StorageConnection, every key forced under apps/<owner>/; worker scripts
# reach the same prefix through the pyrunner_storage helper
# (docs/PLAN_storage_seam.md).
API_VERSION = "2.5"


# --------------------------------------------------------------------------- #
# Shared helpers (all lazy)
# --------------------------------------------------------------------------- #

def _resolve_workspace(workspace):
    """Return ``workspace`` as-is, or the default workspace when None."""
    if workspace is not None:
        return workspace
    from core.models import Workspace

    return Workspace.get_default()


def _resolve_environment(environment):
    """Accept an Environment instance or a name string; return the instance/None."""
    if environment is None or not isinstance(environment, str):
        return environment
    from core.models import Environment

    return Environment.objects.filter(name=environment).first()


# --------------------------------------------------------------------------- #
# Environments — SELECTED, never owned or created by plugins
# --------------------------------------------------------------------------- #

class EnvironmentAPI:
    """Read-only view of the shared Environments. Plugins SELECT one; they never
    build venvs or pip-install (that work belongs to PyRunner's environment UI)."""

    def list(self):
        from core.models import Environment

        return list(Environment.objects.all().order_by("name"))

    def get(self, name):
        from core.models import Environment

        return Environment.objects.filter(name=name).first()


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

class SecretAPI:
    """Owner-scoped encrypted secrets. ``owner=None`` ⇒ legacy global/user lane."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Secret

        ws = self._ws()
        qs = Secret.objects.filter(workspace=ws)
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(self, key, value, *, description=None, owner_key=None):
        """Create or update a secret (idempotent on (owner_plugin, key, workspace)).

        The value is always (re-)encrypted. ``key`` is the clean env-var name the
        secret injects under; for an owned secret it is also the idempotency handle.
        """
        from core.models import Secret

        secret = self._qs().filter(key=key).first()
        if secret is None:
            secret = Secret(key=key, workspace=self._ws(), owner_plugin=self.owner)
        secret.owner_key = owner_key if owner_key is not None else (key if self.owner else None)
        if description is not None:
            secret.description = description
        secret.set_value(value)
        secret.save()
        return secret

    def get(self, key):
        return self._qs().filter(key=key).first()

    def list(self):
        return list(self._qs().order_by("key"))

    def grant(self, script, secret, *, active=True):
        """Attach ``secret`` to ``script`` for selected-mode injection (idempotent)."""
        from core.models import SecretGrant

        grant, created = SecretGrant.objects.get_or_create(
            script=script, secret=secret, defaults={"active": active}
        )
        if not created and grant.active != active:
            grant.active = active
            grant.save(update_fields=["active"])
        return grant


# --------------------------------------------------------------------------- #
# Libraries — shared multi-module code, attached to scripts
# --------------------------------------------------------------------------- #

class LibraryAPI:
    """Owner-scoped shared Python code that scripts attach and import.

    The seam for any plugin bigger than one file: bundle the pipeline once as a
    Library and ``attach()`` it to each worker script, instead of duplicating it
    into every script's code. The canonical provisioning shape is

        libs = LibraryAPI(OWNER)
        lib = libs.upsert("myplugin_lib", modules=read_lib_folder())
        for script in workers:
            libs.attach(script, lib)

    ``upsert`` is idempotent BY CONTENT: re-provisioning on every settings save
    writes a new revision only when the code actually changed, so history stays
    signal rather than one entry per save.

    Note ``key`` is a Python import name (``from myplugin_lib.pipeline import
    run``), so unlike DataStoreAPI it is NOT auto-prefixed with the owner slug —
    ``"myplugin:lib"`` is not importable. Keys are unique per workspace; prefix
    yours with the plugin slug to stay collision-free.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Library

        qs = Library.objects.filter(workspace=self._ws())
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(self, key, *, modules, name=None, description=None, owner_key=None,
               created_by=None):
        """Create or update an owned library and save ``modules`` as a revision.

        Idempotent on ``(owner_plugin, owner_key)`` — ``owner_key`` defaults to
        ``key``, so a plugin may rename the import key without orphaning the row.
        A new revision is written ONLY if the module content changed.

        Raises ``ValueError`` if the key is not a legal import name, is reserved,
        or is already taken in this workspace by a different owner — never
        silently hijacks another owner's library.
        """
        from django.core.exceptions import ValidationError

        from core.models import Library
        from core.models.library import validate_library_key, validate_modules

        try:
            validate_library_key(key)
            validate_modules(modules)
        except ValidationError as e:
            raise ValueError("; ".join(e.messages)) from e

        handle = owner_key if owner_key is not None else key
        library = self._qs().filter(owner_key=handle).first() if self.owner else None
        if library is None:
            library = self._qs().filter(key=key).first()

        # The key is unique per workspace across ALL owners, so a row here that
        # isn't the one we're upserting belongs to someone else. Checked for both
        # create AND rename (a plugin changing its import key), so provisioning
        # gets a named error instead of an IntegrityError from deep in save().
        clash = Library.objects.filter(workspace=self._ws(), key=key)
        if library is not None:
            clash = clash.exclude(pk=library.pk)
        clash = clash.first()
        if clash is not None:
            owner_desc = (
                f"plugin '{clash.owner_plugin}'" if clash.owner_plugin else "a user"
            )
            raise ValueError(
                f"Library key '{key}' is already used in this workspace by {owner_desc}."
            )

        if library is None:
            library = Library(
                key=key,
                workspace=self._ws(),
                owner_plugin=self.owner,
                created_by=created_by,
            )

        library.key = key
        library.owner_key = handle if self.owner else None
        if name is not None:
            library.name = name
        elif not library.name:
            library.name = key
        if description is not None:
            library.description = description
        library.save()

        library.save_revision(modules, created_by=created_by)
        return library

    def upsert_from_folder(self, key, folder, **kwargs):
        """``upsert`` with ``modules`` read from ``folder``'s top-level ``*.py`` files.

        The canonical provisioning call: point it at the plugin's bundled ``lib/``
        folder and the folder becomes the library. Uses the same reader the dev-mode
        auto-resync uses, so what you get on activation and what you get from a
        dev-mode edit can never diverge.

        Raises ``ValueError`` if the folder has no modules — a silent no-op here
        would leave the plugin's scripts importing nothing.
        """
        from core.services.library_service import read_library_folder

        modules = read_library_folder(folder)
        if not modules:
            raise ValueError(
                f"upsert_from_folder: no .py modules found in {folder!r}"
            )
        return self.upsert(key, modules=modules, **kwargs)

    def get(self, key):
        return self._qs().filter(key=key).first()

    def list(self):
        return list(self._qs().order_by("key"))

    def attach(self, script, library):
        """Attach ``library`` to ``script`` so its runs can import it (idempotent).

        Raises ``ValueError`` on a cross-workspace attach — a script must never
        import code its workspace cannot see.
        """
        from django.core.exceptions import ValidationError

        from core.models import ScriptLibrary

        existing = ScriptLibrary.objects.filter(script=script, library=library).first()
        if existing is not None:
            return existing

        attachment = ScriptLibrary(script=script, library=library)
        try:
            attachment.full_clean()
        except ValidationError as e:
            raise ValueError("; ".join(e.messages)) from e
        attachment.save()
        return attachment

    def detach(self, script, library):
        """Detach ``library`` from ``script``. Returns True if an attachment went away."""
        from core.models import ScriptLibrary

        deleted, _ = ScriptLibrary.objects.filter(
            script=script, library=library
        ).delete()
        return bool(deleted)


# --------------------------------------------------------------------------- #
# Runs — a read-model DECOUPLED from the ORM
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RunView:
    """An immutable, ORM-free snapshot of a single Run.

    Plugins OBSERVE runs through this read-model instead of holding a live
    ``core.models.Run``: no ORM object leaks past the SDK boundary, so a plugin's
    status endpoint can't accidentally trigger a query or couple itself to the
    core schema (the very coupling the "sdk-usage" doctor warn discourages).
    ``as_dict()`` returns only JSON-serializable values (datetimes → ISO 8601),
    so a plugin can hand it straight to ``JsonResponse``.
    """

    id: str
    status: str
    trigger_type: str
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    duration: Optional[float]
    exit_code: Optional[int]
    pid: Optional[int]
    task_id: str
    is_finished: bool
    is_running: bool

    @classmethod
    def _from_run(cls, run):
        """Build a RunView from a Run instance (no module-top core import — the
        instance carries its own ``Status`` choices, so we stay import-light)."""
        return cls(
            id=str(run.id),
            status=str(run.status),
            trigger_type=str(run.trigger_type),
            created_at=run.created_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            duration=run.duration,
            exit_code=run.exit_code,
            pid=run.pid,
            task_id=run.task_id or "",
            is_finished=run.is_finished,
            is_running=run.status == run.Status.RUNNING,
        )

    def as_dict(self) -> dict:
        """JSON-serializable dict (datetimes → ISO 8601) for a status endpoint."""

        def _iso(value):
            return value.isoformat() if value is not None else None

        return {
            "id": self.id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration": self.duration,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "task_id": self.task_id,
            "is_finished": self.is_finished,
            "is_running": self.is_running,
        }


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #

class ScriptAPI:
    """Owner-scoped scripts. ``upsert`` is idempotent on (owner_plugin, owner_key)."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Script

        ws = self._ws()
        qs = Script.objects.filter(workspace=ws)
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(
        self,
        *,
        key=None,
        name=None,
        code=None,
        environment=None,
        timeout_seconds=None,
        injection_mode=None,
        description=None,
        is_enabled=None,
        notify_on=None,
        notify_email=None,
        created_by=None,
    ):
        """Create or update an owned script (idempotent on (owner_plugin, owner_key)).

        ``key`` is the stable per-owner handle (required for owned scripts). The
        human-facing ``name`` is auto-derived from it if omitted. Plugin-owned
        scripts default to ``injection_mode='selected'`` (so they receive only
        granted/same-owner/global secrets); ``isolation_mode`` is left at the model
        default ``'inherit'`` so ``resolve_isolation`` (the sandbox policy) decides.
        """
        from core.models import Script

        if self.owner and not key:
            raise ValueError("ScriptAPI.upsert requires key= for an owned script")

        if self.owner:
            script = self._qs().filter(owner_key=key).first()
        else:
            # Legacy lane: match by name within the workspace (owner-NULL).
            script = self._qs().filter(name=name).first() if name else None

        creating = script is None
        if creating:
            script = Script(workspace=self._ws(), owner_plugin=self.owner, owner_key=key)
            # Default scoped injection for owned scripts; legacy stays 'all'.
            script.injection_mode = injection_mode or (
                Script.InjectionMode.SELECTED if self.owner else Script.InjectionMode.ALL
            )
            if created_by is not None:
                script.created_by = created_by

        script.name = name or script.name or (f"{self.owner}:{key}" if self.owner else key)
        if code is not None:
            script.code = code
        if injection_mode is not None:
            script.injection_mode = injection_mode
        if description is not None:
            script.description = description
        if is_enabled is not None:
            script.is_enabled = is_enabled
        if notify_on is not None:
            script.notify_on = notify_on
        if notify_email is not None:
            script.notify_email = notify_email
        if timeout_seconds is not None:
            script.timeout_seconds = timeout_seconds

        env = _resolve_environment(environment)
        if env is not None:
            script.environment = env
        elif creating:
            raise ValueError(
                "ScriptAPI.upsert requires environment= (instance or name) on create"
            )

        script.save()
        return script

    def set_environment(self, environment):
        """Bulk-set the Environment on EVERY script this plugin owns (one call).

        The "pick a venv on the plugin page, all connected scripts follow"
        behavior. No-op in the legacy (owner=None) lane. Returns the count updated.
        """
        if not self.owner:
            return 0
        env = _resolve_environment(environment)
        if env is None:
            raise ValueError("set_environment: unknown environment")
        return self._qs().update(environment=env)

    def queue_run(self, key, *, triggered_by=None):
        """Queue a tracked Run for the owned script ``key`` (via the RunBackend seam)."""
        from core.models import Run
        from core.tasks import queue_script_run

        script = self.get(key)
        if script is None:
            raise ValueError(f"No owned script with key={key!r}")
        run = Run.objects.create(
            script=script,
            workspace_id=script.workspace_id,
            status=Run.Status.PENDING,
            triggered_by=triggered_by,
            trigger_type=Run.TriggerType.MANUAL,
            code_snapshot=script.code,
        )
        queue_script_run(run)
        return run

    def get(self, key):
        if self.owner:
            return self._qs().filter(owner_key=key).first()
        return self._qs().filter(name=key).first()

    def list(self):
        return list(self._qs().order_by("name"))

    # -- Run lifecycle: OBSERVE + CONTROL a provisioned script's runs --------- #

    def _runs_qs(self, key):
        """Runs of the owned script ``key``, newest first (owner+workspace scoped).

        Scoping is exact and free: we resolve the single Script via ``get()``
        (already owner+workspace filtered) and return its runs, so a plugin can
        never observe another owner's or another workspace's runs. Empty when no
        such owned script exists.
        """
        from core.models import Run

        script = self.get(key)
        if script is None:
            return Run.objects.none()
        return Run.objects.filter(script=script).order_by("-created_at")

    def latest_run(self, key):
        """Return the most recent Run of the owned script ``key`` as a RunView,
        or None when the script has never run (or doesn't exist). Poll this for a
        plugin's live status badge."""
        run = self._runs_qs(key).first()
        return RunView._from_run(run) if run is not None else None

    def runs(self, key, *, limit=20):
        """Return recent run history (newest first) as RunViews, owner-scoped.

        ``limit`` caps the count (default 20); a non-positive limit yields []. The
        result is a list of plain RunViews — no ORM objects leak to the caller.
        """
        return [RunView._from_run(r) for r in self._runs_qs(key)[: max(0, int(limit))]]

    def cancel_latest_run(self, key):
        """Cancel the latest pending/running run of the owned script ``key``.

        Reuses the shared force-stop path (``TaskService.force_stop_run``) — the
        SAME code the tasks Stop button calls — so a RUNNING run's process tree is
        killed and a PENDING run is dequeued, both flipped to CANCELLED. Returns
        True if a run was cancelled, False when nothing was cancellable (no run in
        a pending/running state, or no such owned script). Drives a plugin's
        "Stop" button without importing core.models.
        """
        from core.models import Run
        from core.services.task_service import TaskService

        script = self.get(key)
        if script is None:
            return False
        run = (
            Run.objects.filter(
                script=script,
                status__in=[Run.Status.RUNNING, Run.Status.PENDING],
            )
            .order_by("-created_at")
            .first()
        )
        if run is None:
            return False
        changed, _msg = TaskService.force_stop_run(run)
        return changed


# --------------------------------------------------------------------------- #
# DataStores — the plugin's database (no plugin models/migrations)
# --------------------------------------------------------------------------- #

class _OwnedDataStore:
    """Thin dict-ish handle over a DataStore's entries (JSON values), via the ORM."""

    def __init__(self, store):
        self._store = store

    @property
    def name(self):
        return self._store.name

    @property
    def model(self):
        return self._store

    def set(self, key, value):
        from core.models import DataStoreEntry

        DataStoreEntry.objects.update_or_create(
            datastore=self._store, key=key, defaults={"value_json": json.dumps(value)}
        )

    def get(self, key, default=None):
        from core.models import DataStoreEntry

        entry = DataStoreEntry.objects.filter(datastore=self._store, key=key).first()
        return json.loads(entry.value_json) if entry is not None else default

    def all(self):
        from core.models import DataStoreEntry

        return {
            e.key: json.loads(e.value_json)
            for e in DataStoreEntry.objects.filter(datastore=self._store)
        }


class DataStoreAPI:
    """Owner-scoped key-value stores. The stored ``name`` is auto-derived
    ``"<owner>:<key>"`` (owner=None ⇒ the raw key), keeping ``name`` unique while
    the plugin refers to it by a short key."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _name_for(self, key):
        return f"{self.owner}:{key}" if self.owner else key

    def upsert(self, key, *, description=None, created_by=None):
        """Ensure the owned store exists (idempotent); return a handle for entries."""
        from core.models import DataStore

        name = self._name_for(key)
        store = DataStore.objects.filter(workspace=self._ws(), name=name).first()
        if store is None:
            store = DataStore(name=name, workspace=self._ws())
            if created_by is not None:
                store.created_by = created_by
        store.owner_plugin = self.owner
        store.owner_key = key
        if description is not None:
            store.description = description
        store.save()
        return _OwnedDataStore(store)

    def get(self, key):
        from core.models import DataStore

        store = DataStore.objects.filter(
            workspace=self._ws(), name=self._name_for(key)
        ).first()
        return _OwnedDataStore(store) if store is not None else None

    def list(self):
        from core.models import DataStore

        qs = DataStore.objects.filter(workspace=self._ws())
        qs = qs.filter(owner_plugin=self.owner) if self.owner else qs.filter(
            owner_plugin__isnull=True
        )
        return [_OwnedDataStore(s) for s in qs.order_by("name")]


# --------------------------------------------------------------------------- #
# Databases — real SQL (managed Postgres schemas on the data server)
# --------------------------------------------------------------------------- #

class DatabaseAPI:
    """Owner-scoped managed SQL databases — a real Postgres schema + role per
    database, isolated by Postgres itself. The stored ``name`` is auto-derived
    ``"<owner>:<key>"`` (owner=None ⇒ the raw key), exactly like DataStoreAPI.

    ``provision()`` creates REAL server-side objects, so the instance must have
    a data server attached (``PYRUNNER_DATA_DB_URL``); it raises
    ``DatabaseProvisionError`` otherwise — call ``is_available()`` first for a
    graceful degrade path. The plugin's WORKER script then connects with
    ``pyrunner_db.connect("<owner>:<key>")`` after a ``grant()``; the plugin's
    own VIEWS (a dashboard reading its tables) use ``dsn()`` + psycopg.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _name_for(self, key):
        return f"{self.owner}:{key}" if self.owner else key

    def is_available(self):
        """Whether this instance has a data server attached."""
        from core.services import DatabaseService

        return DatabaseService.is_configured()

    def provision(self, key, *, description=None, created_by=None):
        """Ensure the owned database exists AND is provisioned server-side.

        Idempotent on the auto-derived name: re-running updates the same row and
        re-stamps the server objects (which also heals a row stuck in
        ``status='error'`` from an earlier failure). Raises
        ``DatabaseProvisionError`` when the data server is unreachable or not
        configured — fail-closed, never a silently-unusable database.
        """
        from core.models import Database
        from core.services import DatabaseService

        name = self._name_for(key)
        database = Database.objects.filter(workspace=self._ws(), name=name).first()
        if database is None:
            return DatabaseService.create_database(
                name=name,
                workspace=self._ws(),
                description=description or "",
                created_by=created_by,
                owner_plugin=self.owner,
                owner_key=key if self.owner else None,
            )

        database.owner_plugin = self.owner
        database.owner_key = key if self.owner else None
        if description is not None:
            database.description = description
        database.save()
        DatabaseService.provision(database)
        return database

    def get(self, key):
        from core.models import Database

        return Database.objects.filter(
            workspace=self._ws(), name=self._name_for(key)
        ).first()

    def list(self):
        from core.models import Database

        qs = Database.objects.filter(workspace=self._ws())
        qs = (
            qs.filter(owner_plugin=self.owner)
            if self.owner
            else qs.filter(owner_plugin__isnull=True)
        )
        return list(qs.order_by("name"))

    def grant(self, script, database, *, active=True):
        """Attach ``database`` to ``script`` so its runs can connect (idempotent).

        Database access is EXPLICIT-ONLY — unlike secrets there is no 'all'
        injection mode, so a plugin's worker script needs this grant before
        ``pyrunner_db`` will resolve the database's credentials.
        """
        from core.models import DatabaseGrant

        grant, created = DatabaseGrant.objects.get_or_create(
            script=script, database=database, defaults={"active": active}
        )
        if not created and grant.active != active:
            grant.active = active
            grant.save(update_fields=["active"])
        return grant

    def dsn(self, key):
        """The scoped DSN of the owned database, or None when it doesn't exist
        or isn't ready. For the plugin's OWN server-side code (e.g. a dashboard
        view reading its tables with psycopg) — handle it like a password; the
        connection is confined to the database's schema either way."""
        from core.services import DatabaseService

        database = self.get(key)
        if database is None or not database.is_ready:
            return None
        return DatabaseService.scoped_dsn(database)


# --------------------------------------------------------------------------- #
# Object storage
# --------------------------------------------------------------------------- #

class StorageKeyError(ValueError):
    """Raised when a storage key would escape the owner's prefix, or when the
    owner is missing. Loud on purpose: silently sanitizing a traversal attempt
    turns it into a write to the WRONG key, which is worse than a refusal."""


class StorageError(Exception):
    """Raised when a storage operation fails or the seam is unavailable."""


class StorageAPI:
    """Owner- and workspace-scoped object storage on the assets connection.

    Every key is forced under ``apps/<owner>/<workspace-id>/``, and callers only
    ever see keys RELATIVE to that prefix — a plugin cannot name, read, or delete
    another plugin's object, nor another workspace's, because it has no way to
    express one.

    Both scoping axes match the rest of the SDK: ``owner`` stamps the resource
    like every other facade, and ``workspace`` scopes it like Secrets/DataStores/
    Databases (``workspace=None`` ⇒ the default workspace, the same rule the
    scheduler uses). The plugin slug comes FIRST in the key so uninstall is one
    prefix delete across every workspace — see ``delete_all``.

    The connection itself is instance-global (Services → Object Storage), like
    the active AI provider: buckets and credentials are operator infrastructure,
    not tenant data. Without one the seam is unavailable and every operation
    raises ``StorageError``; call ``is_available()`` first for a graceful degrade
    path, exactly like ``DatabaseAPI``.

        storage = StorageAPI(OWNER)
        if storage.is_available():
            storage.put("avatars/abc.jpg", data, content_type="image/jpeg")
            url = storage.url("avatars/abc.jpg")

    ``url()`` returns a permanent public URL when the connection has a
    ``public_base_url``, else a presigned URL that EXPIRES (default 1 hour, 7-day
    ceiling). Bake presigned URLs into a long-lived page or an email and they will
    rot; that is what the public base URL is for.
    """

    PREFIX_ROOT = "apps"

    def __init__(self, owner=None, workspace=None):
        # Unlike Secrets/DataStores there is NO unowned lane: a key with no owner
        # prefix is exactly the escape this class exists to prevent.
        if not owner:
            raise StorageKeyError("StorageAPI requires an owner (plugin slug)")
        self.owner = owner
        # Resolved lazily (see _ws_id): uninstall builds a StorageAPI purely to
        # call delete_all(), which is workspace-independent and must not require a
        # default workspace to exist.
        self._workspace = workspace

    # ------------------------------------------------------------------ #
    # Prefix enforcement
    # ------------------------------------------------------------------ #

    def _ws_id(self):
        """The id of the workspace this instance addresses.

        Accepts a Workspace instance OR a bare id, because the loopback endpoint
        derives only an id from the run's signed token. Raises rather than
        building an ``apps/<owner>/None/`` prefix — a key that silently pools
        every workspace's objects is the leak this scoping prevents.
        """
        from core.models import Workspace

        workspace = self._workspace
        if workspace is None:
            workspace = Workspace.get_default()
        if workspace is None:
            raise StorageError("No workspace is available to scope storage to.")
        return getattr(workspace, "pk", workspace)

    @property
    def owner_root(self):
        """Every workspace's space for this owner, e.g. ``apps/yt_comments/``.

        Only uninstall addresses this level; ordinary calls go through ``prefix``.
        """
        return f"{self.PREFIX_ROOT}/{self.owner}/"

    @property
    def prefix(self):
        """This owner's key prefix in THIS workspace,
        e.g. ``apps/yt_comments/3f2b…/``."""
        return f"{self.owner_root}{self._ws_id()}/"

    def _full_key(self, key):
        """Validate a caller key and return the absolute bucket key.

        Rejects (never rewrites): empty keys, absolute keys, backslashes, NUL, and
        any ``..`` segment. Backslashes are rejected rather than normalized
        because S3 treats ``\\`` as an ordinary character while a Windows-minded
        caller means a separator — the two readings disagree, so refuse.
        """
        if not isinstance(key, str) or not key.strip():
            raise StorageKeyError("Storage key must be a non-empty string")

        key = key.strip()

        if key.startswith("/"):
            raise StorageKeyError(f"Storage key must be relative, got: {key!r}")
        if "\\" in key:
            raise StorageKeyError(f"Storage key must not contain backslashes: {key!r}")
        if "\x00" in key:
            raise StorageKeyError("Storage key must not contain NUL")
        if any(segment == ".." for segment in key.split("/")):
            raise StorageKeyError(f"Storage key must not traverse upward: {key!r}")

        return f"{self.prefix}{key}"

    def _relative_key(self, full_key):
        """Strip the owner prefix off a bucket key for return to the caller."""
        if full_key.startswith(self.prefix):
            return full_key[len(self.prefix):]
        return full_key

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _connection(self):
        from core.services.s3_service import S3Service

        connection = S3Service.for_assets()
        if connection is None or not connection.enabled or not connection.is_configured:
            return None
        return connection

    def is_available(self):
        """Whether storage can actually be used here.

        Both axes have to resolve: a usable assets connection AND a workspace to
        scope keys under. Never raises — the whole point is a cheap degrade check
        a plugin can branch on.
        """
        if self._connection() is None:
            return False
        try:
            self._ws_id()
        except StorageError:
            return False
        return True

    def _require_connection(self):
        connection = self._connection()
        if connection is None:
            raise StorageError(
                "No assets storage connection is configured. Set one in "
                "Services → Object Storage."
            )
        return connection

    # ------------------------------------------------------------------ #
    # Objects
    # ------------------------------------------------------------------ #

    def put(self, key, data, content_type="application/octet-stream"):
        """Write ``data`` (bytes or str) at ``key``, relative to the owner prefix.

        Returns the relative key on success; raises ``StorageError`` on failure —
        a silent False would let a plugin believe it archived something it didn't.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        full_key = self._full_key(key)

        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            raise StorageError(f"Storage data must be bytes or str, got {type(data).__name__}")

        result = S3Service.upload_file(
            connection, bytes(data), full_key, content_type=content_type
        )
        if not result.get("success"):
            raise StorageError(f"Storage put failed for {key!r}: {result.get('error')}")
        return key

    def get(self, key):
        """Read the object's bytes, or None when it doesn't exist."""
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        return S3Service.get_file(connection, self._full_key(key))

    def delete(self, key):
        """Delete one object. True when the delete was issued cleanly."""
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        return S3Service.delete_file(connection, self._full_key(key))

    def list(self, prefix=""):
        """List this owner's objects as dicts of ``key`` (RELATIVE to the owner
        prefix), ``size``, ``last_modified``, ``etag``.

        ``prefix`` filters within the owner's own space and is validated like any
        other key, so ``list("../")`` cannot enumerate the bucket.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        # An empty prefix means "everything of mine", not "everything".
        full_prefix = self._full_key(prefix) if prefix else self.prefix

        return [
            {**f, "key": self._relative_key(f["key"])}
            for f in S3Service.list_files(connection, full_prefix)
        ]

    def url(self, key, expires_in=3600):
        """A URL for the object.

        Public connection (``public_base_url`` set) → a permanent, hot-linkable
        URL, and ``expires_in`` is irrelevant. Otherwise → a presigned URL that
        expires after ``expires_in`` seconds (7-day ceiling). Returns None when a
        presigned URL could not be signed.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        full_key = self._full_key(key)

        if connection.is_public:
            return f"{connection.public_base_url.rstrip('/')}/{full_key}"

        return S3Service.presigned_url(connection, full_key, expires_in=expires_in)

    def delete_all(self):
        """Delete this owner's objects in EVERY workspace. Returns the count.

        Deliberately owner-wide rather than workspace-scoped: its caller is plugin
        uninstall (`remove_data`), and a plugin is installed — and removed —
        instance-wide, exactly like the owned-row cleanup beside it, which also
        filters on ``owner_plugin`` with no workspace clause. Leaving one
        workspace's files behind after an uninstall would strand them with no UI
        that can ever reach them again.

        Uses ``owner_root``, so it needs no default workspace to exist.
        """
        from core.services.s3_service import S3Service

        connection = self._connection()
        if connection is None:
            return 0
        return S3Service.delete_prefix(connection, self.owner_root)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

class ScheduleAPI:
    """Wraps ScriptSchedule + ScheduleService so a plugin schedules a run without
    touching django-q directly (and the global-pause rules still apply)."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def sync(
        self,
        script,
        *,
        mode,
        time_str=None,
        weekday=None,
        month=None,
        day=None,
        interval_minutes=None,
        cron=None,
        tz="UTC",
    ):
        """Create/update the script's schedule and push it to django-q2.

        ``mode`` is a ``ScriptSchedule.RunMode`` value ('manual'/'interval'/'daily'
        /'weekly'/'monthly'/'yearly'/'cron'). Mirrors the hand-rolled qdrant
        sync_schedule, generalized. Yearly mode takes ``month``, ``day`` and one
        ``time_str`` in HH:MM format; ``cron`` mode takes a raw 5-field expression
        via ``cron``. Both are written in ``tz``.
        """
        from core.models import ScriptSchedule
        from core.services.schedule_service import ScheduleService

        sched, _ = ScriptSchedule.objects.get_or_create(
            script=script, defaults={"workspace_id": script.workspace_id}
        )
        sched.run_mode = mode
        sched.timezone = tz or "UTC"
        sched.interval_minutes = None
        sched.daily_times = []
        sched.weekly_days = []
        sched.weekly_times = []
        sched.monthly_days = []
        sched.monthly_times = []
        sched.cron_expression = ""
        sched.yearly_month = None
        sched.yearly_day = None
        sched.yearly_time = ""

        if mode == ScriptSchedule.RunMode.INTERVAL:
            if not interval_minutes:
                raise ValueError("interval mode requires interval_minutes")
            sched.interval_minutes = int(interval_minutes)
        elif mode == ScriptSchedule.RunMode.DAILY:
            if not time_str:
                raise ValueError("daily mode requires time_str (HH:MM)")
            sched.daily_times = [time_str]
        elif mode == ScriptSchedule.RunMode.WEEKLY:
            if weekday is None:
                raise ValueError("weekly mode requires weekday (0=Mon … 6=Sun)")
            if not time_str:
                raise ValueError("weekly mode requires time_str (HH:MM)")
            sched.weekly_days = [int(weekday)]
            sched.weekly_times = [time_str]
        elif mode == ScriptSchedule.RunMode.CRON:
            is_valid, error = ScheduleService.validate_cron_expression(cron, sched.timezone)
            if not is_valid:
                raise ValueError(f"Invalid cron expression: {error}")
            sched.cron_expression = (cron or "").strip()
        elif mode == ScriptSchedule.RunMode.YEARLY:
            if month is None or day is None:
                raise ValueError("yearly mode requires month and day")
            if not time_str:
                raise ValueError("yearly mode requires time_str (HH:MM)")
            from calendar import monthrange

            try:
                month, day = int(month), int(day)
                if not 1 <= month <= 12 or not 1 <= day <= monthrange(2024, month)[1]:
                    raise ValueError
                hour, minute = map(int, time_str.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
                time_str = f"{hour:02d}:{minute:02d}"
            except (TypeError, ValueError):
                raise ValueError("yearly mode requires a valid month, day, and time_str (HH:MM)")
            sched.yearly_month = month
            sched.yearly_day = day
            sched.yearly_time = time_str

        sched.is_active = mode != ScriptSchedule.RunMode.MANUAL
        sched.save()
        ScheduleService.sync_schedule(sched)
        return sched

    def list(self):
        from core.models import ScriptSchedule

        qs = ScriptSchedule.objects.filter(script__workspace=_resolve_workspace(self._workspace))
        if self.owner:
            qs = qs.filter(script__owner_plugin=self.owner)
        return list(qs)


# --------------------------------------------------------------------------- #
# External HTTP API (API 2.3) — handlers for /api/v1/plugins/<slug>/…
#
# A plugin declares resources in plugin.json ("provides.api_resources") and
# marks handlers in its <plugin>/api.py with @resource. Core's dispatcher
# (core.views.api.plugins) owns 100% of auth, rate limiting, and workspace
# scoping — a handler never sees an HttpRequest, a token, or a tenancy
# decision. A plugin's api.py imports ONLY this module (import-light lane).
# --------------------------------------------------------------------------- #

# Attribute the @resource marker sets; the dispatcher scans for it. An
# attribute (not a global registry) keeps re-imports idempotent and makes slug
# inference a non-problem.
API_RESOURCE_ATTR = "_pyrunner_api_resource"


def resource(name):
    """Mark a function in a plugin's ``api.py`` as the handler for ``name``.

    The handler receives an :class:`APIRequest` and returns a JSON-serializable
    dict that IS the response body (core adds no envelope). Raise
    :class:`APIError` for a clean 4xx; any other exception becomes a generic
    500 with the traceback logged server-side.

    The resource must ALSO be declared in plugin.json under
    ``provides.api_resources`` — an undeclared handler is never served.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@resource requires a non-empty name string")

    def marker(func):
        setattr(func, API_RESOURCE_ATTR, name)
        return func

    return marker


@dataclass(frozen=True)
class APIRequest:
    """Frozen request context handed to a @resource handler.

    ``workspace`` is derived server-side from the API token — treat it as an
    opaque handle to pass into SDK constructors; the handler cannot widen it.
    ``params`` holds the first value per query key; ``params_list`` holds every
    value (``?tag=a&tag=b`` → ``{"tag": ["a", "b"]}``).
    """

    workspace: object
    resource: str
    item_id: Optional[str]
    method: str
    params: dict
    params_list: dict


class APIError(Exception):
    """The one sanctioned way for a handler to return an error response.

    ``status`` must be 400–499 (the dispatcher rejects anything else as a
    plugin bug — a plugin must not fabricate redirects or fake 5xx).
    """

    def __init__(self, message, *, code="BAD_REQUEST", status=400):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.status = status


class ChannelAPI:
    """Read-only, workspace-scoped view of the notification Channels.

    The channel-picker seam (e.g. an alert form listing where a plugin may
    send via ``pyrunner_notify``). List-only by design: channels are
    user-configured infrastructure a plugin selects, never creates — and the
    listing is scoped to the workspace so channel names can't leak across
    tenants through a plugin page.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def list(self):
        from core.models import Channel

        ws = _resolve_workspace(self._workspace)
        return [
            {"name": c.name, "channel_type": c.provider, "is_enabled": c.enabled}
            for c in Channel.objects.for_workspace(ws).order_by("name")
        ]


# --------------------------------------------------------------------------- #
# Public pages (API 2.3, Stage 5) — shareable HTML at /p/<token>/
#
# Same registration surface as API resources: declare in plugin.json
# ("provides.public_pages"), mark the handler in <plugin>/api.py with @page.
# The handler returns an HTML STRING rendered via Django templates
# (auto-escape — never string-concatenated HTML: public pages render scraped
# content, and a poisoned title must never become stored XSS). Core serves it
# script-free (strict CSP) with noindex/no-store/no-referrer headers.
# --------------------------------------------------------------------------- #

API_PAGE_ATTR = "_pyrunner_public_page"


def page(name):
    """Mark a function in a plugin's ``api.py`` as the renderer for public
    page ``name`` (declared in plugin.json under ``provides.public_pages``).

    The handler receives a :class:`PageRequest` and returns an HTML string.
    Render through Django templates only — auto-escaping is the XSS defense
    the strict CSP backs up, not replaces.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@page requires a non-empty name string")

    def marker(func):
        setattr(func, API_PAGE_ATTR, name)
        return func

    return marker


@dataclass(frozen=True)
class PageRequest:
    """Frozen context handed to a @page handler.

    ``workspace`` comes from the share row (set when the page was shared),
    never from the anonymous caller. ``params`` is the query string, first
    value per key — use it for display toggles only, never authorization.
    """

    workspace: object
    page: str
    params: dict


class PublicPageAPI:
    """Share/revoke a plugin's public pages (owner-stamped, workspace-scoped).

    ``share()`` is an idempotent upsert per (plugin, page, workspace) — but a
    revoked or expired row is reactivated with a FRESHLY ROTATED token: the
    old URL stays dead permanently (a dead capability URL never resurrects).
    Plain re-share of a live page returns the existing URL unchanged.
    """

    def __init__(self, owner=None, workspace=None):
        if not owner:
            raise ValueError("PublicPageAPI requires the plugin slug as owner")
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import PluginPublicPage

        return PluginPublicPage.objects.filter(
            plugin_slug=self.owner, workspace=self._ws()
        )

    # Sentinel: distinguishes "share() called without expires_at" (keep the
    # row's current expiry) from an explicit expires_at=None (clear it).
    _KEEP = object()

    def share(self, page_name, *, expires_at=_KEEP, created_by=None):
        """Ensure ``page_name`` is shared; return its public URL path.

        ``expires_at`` is only written when explicitly passed — a plain
        re-share never silently clears (or changes) an existing expiry. Pass
        ``expires_at=None`` to explicitly remove one. A revoked/expired row is
        always reactivated with a fresh expiry state and a ROTATED token.
        """
        from django.utils import timezone as _tz

        from core.models import PluginPublicPage

        explicit_expiry = expires_at is not self._KEEP
        row = self._qs().filter(page=page_name).first()
        if row is None:
            row = PluginPublicPage.objects.create(
                plugin_slug=self.owner,
                page=page_name,
                workspace=self._ws(),
                token=PluginPublicPage.generate_token(),
                expires_at=expires_at if explicit_expiry else None,
                created_by=created_by,
            )
            return row.url_path

        was_dead = (not row.is_active) or (
            row.expires_at and row.expires_at < _tz.now()
        )
        if was_dead:
            # Reactivate with a rotated token — the old URL is dead forever.
            # The stale expiry never carries over (it would dead-end the new
            # link immediately unless the caller re-specifies one).
            row.token = PluginPublicPage.generate_token()
            row.is_active = True
            row.expires_at = expires_at if explicit_expiry else None
        elif explicit_expiry:
            row.expires_at = expires_at
        row.save(update_fields=["token", "is_active", "expires_at"])
        return row.url_path

    def get(self, page_name):
        """The live share row for ``page_name`` (dict), or None."""
        row = self._qs().filter(page=page_name).first()
        if row is None:
            return None
        return {
            "page": row.page,
            "url_path": row.url_path,
            "is_active": row.is_active,
            "is_expired": row.is_expired,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
            "last_accessed_at": row.last_accessed_at,
        }

    def list(self):
        return [self.get(row.page) for row in self._qs().order_by("page")]

    def revoke(self, page_name):
        """Deactivate the share (the URL 404s permanently). True if it existed."""
        updated = self._qs().filter(page=page_name).update(is_active=False)
        return bool(updated)
