"""
LibraryService — the run-time half of Script Libraries.

Two operations, deliberately split across the run lifecycle:

* ``stamp_library_versions(run)`` at QUEUE time — records ``{key: version}`` on
  the Run, alongside where ``code_snapshot`` is taken. Stamping late (at execute)
  would reintroduce the exact race the revision design exists to close: an edit
  landing between queue and execute would silently change what the run imports.
* ``materialize_libraries(run, dest)`` at LAUNCH time — writes the STAMPED
  revisions (never the live head) into a per-run staging dir that goes on
  PYTHONPATH.

Everything a run imports therefore comes from DB rows materialized at launch,
with no reach-back into the web container's ``plugins/`` folder — which is what
keeps the RunBackend seam (remote/isolated executors) intact.
"""

import difflib
import logging
import os

from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


#: Folder inside a plugin whose ``*.py`` files are the source of truth for that
#: plugin's library. Read at provisioning time (activation + every settings-save)
#: and, in dev mode only, re-read on every run-queue.
PLUGIN_LIB_DIRNAME = "lib"


def read_library_folder(folder) -> dict:
    """Read ``folder``'s top-level ``*.py`` files into a ``{filename: code}`` map.

    Flat by design — it mirrors what a library revision can hold, so a plugin
    author sees the same shape on disk and in the console. Subdirectories are
    ignored rather than silently flattened (which would collide names). Returns
    ``{}`` when the folder is absent or has no modules.
    """
    import os as _os

    if not folder or not _os.path.isdir(folder):
        return {}

    modules = {}
    for entry in sorted(_os.listdir(folder)):
        path = _os.path.join(folder, entry)
        if not _os.path.isfile(path) or not entry.endswith(".py"):
            continue
        with open(path, encoding="utf-8") as fh:
            modules[entry] = fh.read()
    return modules


def dev_plugin_slug() -> str | None:
    """The slug of the plugin loaded in dev mode, or None.

    ``settings.DEV_PLUGIN`` is ``"plugins.<slug>"`` and is only ever set under the
    triple guard in settings (DEBUG + PYRUNNER_PLUGIN_DEV + RUN_MAIN), so this is
    None in any production process — which is what keeps auto-resync out of
    production, where libraries stay DB-deterministic.
    """
    from django.conf import settings

    dev_app = getattr(settings, "DEV_PLUGIN", None)
    if not dev_app:
        return None
    return dev_app.rsplit(".", 1)[-1]


def _dev_plugin_lib_dir(slug: str):
    """Absolute path of the dev plugin's ``lib/`` folder, or None.

    Resolved from the imported module rather than re-reading the env var, so it
    always points at the folder actually spliced onto the ``plugins`` package.
    """
    import importlib
    import os as _os

    try:
        module = importlib.import_module(f"plugins.{slug}")
        plugin_dir = _os.path.dirname(module.__file__)
    except Exception as exc:
        logger.warning("Dev plugin %r could not be located for lib resync: %s", slug, exc)
        return None
    return _os.path.join(plugin_dir, PLUGIN_LIB_DIRNAME)


def resync_dev_libraries(script) -> bool:
    """Re-sync the dev plugin's ``lib/`` folder into its Library before a run.

    The fast edit-run loop: edit a module on disk → next run uses it, with no
    manual step. Only fires when the script's owner plugin is THE dev-mode plugin;
    production plugins sync on activation/settings-save only, so their runs stay
    deterministic from the DB.

    A new revision is written only when the content actually changed (the model's
    hash compare), so re-queuing an unedited dev plugin churns nothing.

    Returns True if a new revision was written.

    Target resolution: the plugin's own libraries IN THIS SCRIPT'S WORKSPACE. One
    library ⇒ that is the ``lib/`` folder's home (so a plugin with one library per
    workspace still resyncs fine). Several in the same workspace ⇒ ``lib/`` can't
    say which, so we log and skip rather than guess and overwrite the wrong one
    (such a plugin re-provisions on settings-save anyway).
    """
    owner = getattr(script, "owner_plugin", None)
    if not owner or owner != dev_plugin_slug():
        return False

    from core.models import Library

    libraries = list(Library.objects.filter(owner_plugin=owner, workspace_id=script.workspace_id))
    if not libraries:
        return False
    if len(libraries) > 1:
        logger.warning(
            "Dev resync skipped for plugin %r: it owns %d libraries (%s) but a "
            "single lib/ folder cannot say which to update.",
            owner, len(libraries), ", ".join(lib.key for lib in libraries),
        )
        return False

    library = libraries[0]
    lib_dir = _dev_plugin_lib_dir(owner)

    try:
        modules = read_library_folder(lib_dir)
        if not modules:
            return False
        _, created = library.save_revision(modules)
    except ValidationError as exc:
        # A dev typo (bad filename, oversized module) must not block the run
        # queue — the run proceeds on the last good revision, loudly.
        logger.warning(
            "Dev resync of library %r rejected: %s", library.key, "; ".join(exc.messages)
        )
        return False
    except (OSError, UnicodeDecodeError) as exc:
        # Same rule for an unreadable/not-UTF-8 file on disk. This runs inside
        # queue_script_run, so letting it escape would stop the run from being
        # queued at all — turning one bad byte into "nothing runs".
        logger.warning(
            "Dev resync of library %r could not read %s: %s", library.key, lib_dir, exc
        )
        return False

    if created:
        logger.info(
            "Dev resync: library %r updated to v%d from %s",
            library.key, library.current_version, lib_dir,
        )
    return created


class LibraryMaterializationError(Exception):
    """Raised when a run's stamped libraries cannot be materialized.

    Always fail-closed: the executor turns this into a FAILED run with a named
    cause BEFORE spawning, rather than launching a script that would die on a
    confusing ImportError deep inside user code.
    """


def stamp_library_versions(run) -> dict | None:
    """Resolve ``run``'s attached libraries and pin each to its head revision.

    Returns the ``{key: version}`` map (also saved onto the run), or ``None`` when
    the script has no attached libraries — the overwhelmingly common case, which
    stays byte-for-byte identical to a pre-Libraries instance (NULL column, no
    materialization, no staging dir).

    Libraries with no content yet (``current_version == 0``) are omitted: there is
    no revision to pin, so there is nothing to materialize.
    """
    if not getattr(run, "script_id", None):
        return None

    attached = run.script.libraries.all()
    versions = {
        lib.key: lib.current_version for lib in attached if lib.current_version
    }
    if not versions:
        return None

    run.library_versions = versions
    run.save(update_fields=["library_versions"])
    logger.debug("Run %s pinned libraries: %s", run.id, versions)
    return versions


def _resolve_revision(run, key: str, version: int):
    """Load the exact stamped revision for ``key``, scoped to the run's workspace.

    Scoped by workspace because ``Library.key`` is unique per workspace, not
    globally. Returns None when the library or that revision no longer exists
    (e.g. the library was deleted after the run was queued) — the caller fails
    the run rather than guessing.

    Note: a library deleted AND recreated under the same key between queue and
    execute could in principle resolve a same-numbered revision of the new
    library. Deletion is blocked while a library is attached to any script, which
    makes that sequence unreachable in practice.
    """
    from core.models import Library

    library = Library.objects.filter(
        key=key, workspace_id=run.script.workspace_id
    ).first()
    if library is None:
        return None
    return library.revision(version)


def materialize_libraries(run, dest_root: str) -> list:
    """Write ``run``'s stamped library revisions into ``dest_root`` as packages.

    Each library becomes ``<dest_root>/<key>/<filename>`` plus an auto-generated
    empty ``__init__.py`` when the revision doesn't ship one, so the script can
    ``from <key>.module import thing`` (and modules inside can ``from .sibling
    import ...``).

    Returns the list of materialized library keys. Raises
    ``LibraryMaterializationError`` if any stamped revision is missing or carries
    an illegal name — never partial-and-silent.
    """
    stamped = getattr(run, "library_versions", None) or {}
    if not stamped:
        return []

    # Lazy, like every other core import here: this module must stay importable
    # without the app registry (the light-import boot guard).
    from core.models.library import LIBRARY_KEY_RE, MODULE_NAME_RE

    written = []
    for key, version in sorted(stamped.items()):
        # Re-validate names at materialization: these become filesystem paths, so
        # a hand-edited/corrupted DB row must not be able to escape dest_root.
        # (Model validation already enforces this on every write path.)
        if not LIBRARY_KEY_RE.match(str(key)):
            raise LibraryMaterializationError(f"Illegal library key: {key!r}")

        revision = _resolve_revision(run, key, version)
        if revision is None:
            raise LibraryMaterializationError(
                f"Library '{key}' version {version} was pinned to this run but no "
                "longer exists. The library may have been deleted after the run "
                "was queued. The script was not executed."
            )

        package_dir = os.path.join(dest_root, key)
        os.makedirs(package_dir, exist_ok=True)

        for filename, code in revision.modules.items():
            if not MODULE_NAME_RE.match(str(filename)):
                raise LibraryMaterializationError(
                    f"Library '{key}' v{version} contains an illegal module "
                    f"filename: {filename!r}"
                )
            with open(
                os.path.join(package_dir, filename), "w", encoding="utf-8"
            ) as fh:
                fh.write(code)

        init_path = os.path.join(package_dir, "__init__.py")
        if not os.path.exists(init_path):
            with open(init_path, "w", encoding="utf-8"):
                pass

        written.append(key)

    logger.debug("Run %s materialized libraries %s into %s", run.id, written, dest_root)
    return written


def diff_module_maps(old_modules: dict, new_modules: dict) -> list:
    """Unified diff between two ``{filename: code}`` maps, one entry per module.

    Returns a list of ``{filename, status, diff_lines}`` covering the UNION of both
    sides — a module that was added or deleted is part of the change, not just the
    ones whose text moved. ``diff_lines`` is empty for ``status='unchanged'`` so the
    template can collapse the noise.

    Server-side (difflib) rather than a client-side diff editor: the whole module
    set diffs at once, it renders without the CDN, and it is directly testable.
    """
    old_modules = old_modules or {}
    new_modules = new_modules or {}

    results = []
    for filename in sorted(set(old_modules) | set(new_modules)):
        old_code = old_modules.get(filename)
        new_code = new_modules.get(filename)

        if old_code == new_code:
            status = "unchanged"
        elif old_code is None:
            status = "added"
        elif new_code is None:
            status = "removed"
        else:
            status = "changed"

        diff_lines = []
        if status != "unchanged":
            diff_lines = list(
                difflib.unified_diff(
                    (old_code or "").splitlines(),
                    (new_code or "").splitlines(),
                    fromfile=f"{filename} (old)",
                    tofile=f"{filename} (new)",
                    lineterm="",
                )
            )

        results.append(
            {"filename": filename, "status": status, "diff_lines": diff_lines}
        )
    return results
