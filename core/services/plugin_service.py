"""
Plugin management service.

Implements the lifecycle the management UI drives — install (from an uploaded
.zip), activate, deactivate, delete — plus the "restart required" detection and
the controlled-restart trigger.

Safety is the whole point (see docs/PLAN_plugin_system.md):
  * Upload NEVER imports the plugin. It unpacks (zip-slip safe) + writes a DB row
    with status INSTALLED. The running site is untouched.
  * Activate validates the plugin in an ISOLATED subprocess (plugin_preflight)
    before flipping it to ACTIVE. A failure leaves the live site alone.
  * The new active set only takes effect on a controlled restart (preflight on
    boot re-validates everything first).
"""

import hashlib
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.models import Plugin
from core.plugins import is_valid_plugin_slug

logger = logging.getLogger(__name__)

# Upload guards (zip-bomb / abuse protection).
MAX_MEMBERS = 2000
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB unpacked
MAX_SINGLE_FILE = 20 * 1024 * 1024  # 20 MB per member

PREFLIGHT_TIMEOUT = 120  # seconds


class PluginInstallError(Exception):
    """Raised when an uploaded plugin fails validation/unpacking."""


class PluginService:
    # ------------------------------------------------------------------ install

    @staticmethod
    def install_from_zip(uploaded_file, *, source="upload") -> Plugin:
        """Validate + unpack an uploaded plugin .zip and create an INSTALLED row.

        Never imports or executes the plugin code. Raises PluginInstallError with
        a user-facing message on any validation failure.
        """
        data = uploaded_file.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise PluginInstallError("That file is not a valid .zip archive.")

        with zf:
            slug = PluginService._validate_zip(zf)

            if Plugin.objects.filter(slug=slug).exists() or (
                Path(settings.PLUGINS_DIR) / slug
            ).exists():
                raise PluginInstallError(
                    f"A plugin named '{slug}' already exists. Delete it first to replace it."
                )

            # Unpack to a temp dir, validate structure + manifest, then atomically
            # move the slug folder into PLUGINS_DIR.
            tmp_root = tempfile.mkdtemp(prefix="pyrunner-plugin-")
            try:
                PluginService._safe_extract(zf, tmp_root)
                src = Path(tmp_root) / slug
                manifest = PluginService._read_manifest(src, slug)

                if not (src / "__init__.py").exists():
                    raise PluginInstallError(
                        f"Plugin '{slug}' is missing __init__.py (it must be a Python package)."
                    )
                if not (src / "apps.py").exists():
                    raise PluginInstallError(
                        f"Plugin '{slug}' is missing apps.py (it must define a PluginAppConfig)."
                    )

                dest = Path(settings.PLUGINS_DIR) / slug
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)

            plugin = Plugin.objects.create(
                slug=slug,
                name=manifest.get("name") or slug,
                version=str(manifest.get("version") or "0.0.0"),
                status=Plugin.Status.INSTALLED,
                source=source,
                manifest=manifest,
                checksum=PluginService._checksum_dir(Path(settings.PLUGINS_DIR) / slug),
            )
            logger.info("Plugin %r installed (status=INSTALLED)", slug)
            return plugin

    @staticmethod
    def _validate_zip(zf: zipfile.ZipFile) -> str:
        """Zip-slip / zip-bomb checks; return the single top-level folder (= slug)."""
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise PluginInstallError(
                f"Archive has too many entries ({len(infos)} > {MAX_MEMBERS})."
            )

        total = 0
        top_levels = set()
        for info in infos:
            name = info.filename
            # Reject absolute paths and any parent-traversal (zip-slip).
            norm = name.replace("\\", "/")
            if norm.startswith("/") or os.path.isabs(norm):
                raise PluginInstallError(f"Unsafe absolute path in archive: {name!r}")
            parts = [p for p in norm.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise PluginInstallError(f"Unsafe path traversal in archive: {name!r}")
            # Reject symlinks (mode high bits 0xA000 == symlink).
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise PluginInstallError(f"Archive contains a symlink: {name!r}")
            if info.file_size > MAX_SINGLE_FILE:
                raise PluginInstallError(f"File too large in archive: {name!r}")
            total += info.file_size
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise PluginInstallError("Archive is too large when unpacked.")
            if parts:
                top_levels.add(parts[0])

        if len(top_levels) != 1:
            raise PluginInstallError(
                "Archive must contain exactly one top-level folder (the plugin slug)."
            )
        slug = top_levels.pop()
        if not is_valid_plugin_slug(slug):
            raise PluginInstallError(
                f"Invalid plugin slug '{slug}'. Use lowercase letters, digits, underscores; "
                "must start with a letter."
            )
        return slug

    @staticmethod
    def _safe_extract(zf: zipfile.ZipFile, dest_root: str) -> None:
        """Extract members, re-checking each resolved path stays under dest_root."""
        dest_root_resolved = Path(dest_root).resolve()
        for info in zf.infolist():
            norm = info.filename.replace("\\", "/")
            if norm.endswith("/"):
                continue  # directory entry; created implicitly below
            target = (dest_root_resolved / norm).resolve()
            if not str(target).startswith(str(dest_root_resolved)):
                raise PluginInstallError(f"Blocked path traversal during extract: {norm!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

    @staticmethod
    def _read_manifest(plugin_dir: Path, slug: str) -> dict:
        import json

        manifest_path = plugin_dir / "plugin.json"
        if not manifest_path.exists():
            raise PluginInstallError(f"Plugin '{slug}' is missing plugin.json.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise PluginInstallError(f"plugin.json is not valid JSON: {exc}")
        if not isinstance(manifest, dict):
            raise PluginInstallError("plugin.json must be a JSON object.")
        if manifest.get("slug") != slug:
            raise PluginInstallError(
                f"plugin.json slug ({manifest.get('slug')!r}) must match the folder name ({slug!r})."
            )
        return manifest

    @staticmethod
    def _checksum_dir(folder: Path) -> str:
        h = hashlib.sha256()
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                h.update(path.relative_to(folder).as_posix().encode())
                h.update(path.read_bytes())
        return h.hexdigest()

    # ----------------------------------------------------------------- dev mode

    @staticmethod
    def validate_dev_mode_plugin(local_path) -> tuple[str, list]:
        """Validate a local plugin folder for Dev Mode (Plugin Platform v2, WS1).

        Mirrors the structural checks the ``settings.py`` dev-load block performs,
        but as an importable, testable helper for tests and the future
        ``plugin_doctor --path``. It NEVER imports/executes the plugin code,
        touches disk, or applies migrations — it only inspects the folder layout.

        The folder name is the slug; the plugin is loaded under runserver as
        ``plugins.<slug>`` (its ``apps.py`` declares ``name="plugins.<slug>"``),
        so the dev form is byte-identical to the eventual shipped form.

        Returns ``(slug, warnings)``. ``warnings`` flags v2 rule violations that
        the activation doctor (Stage 4) will later enforce but that do not block
        live dev iteration — currently: shipping ``models.py`` / ``migrations/``
        (plugins persist via owned DataStores, not their own DDL).

        Raises ``PluginInstallError`` on a hard structural failure (bad path,
        invalid slug, or a missing ``__init__.py`` / ``apps.py``).
        """
        folder = Path(local_path).expanduser().resolve()
        if not folder.is_dir():
            raise PluginInstallError(f"Dev plugin path is not a directory: {folder}")

        slug = folder.name
        if not is_valid_plugin_slug(slug):
            raise PluginInstallError(
                f"Invalid dev plugin slug '{slug}' (from the folder name). Use "
                "lowercase letters, digits, underscores; must start with a letter."
            )
        if not (folder / "__init__.py").exists():
            raise PluginInstallError(
                f"Dev plugin '{slug}' is missing __init__.py (it must be a Python package)."
            )
        if not (folder / "apps.py").exists():
            raise PluginInstallError(
                f"Dev plugin '{slug}' is missing apps.py (it must define a PluginAppConfig)."
            )

        warnings = []
        if (folder / "models.py").exists():
            warnings.append(
                "Plugin ships models.py — plugins persist via owned DataStores, "
                "not their own models. The activation doctor will reject this."
            )
        if (folder / "migrations").is_dir():
            warnings.append(
                "Plugin ships a migrations/ package — plugins apply no DDL. "
                "The activation doctor will reject this."
            )
        return slug, warnings

    # --------------------------------------------------------------- lifecycle

    @staticmethod
    def activate(plugin: Plugin) -> tuple[bool, str]:
        """Validate in an isolated subprocess; on success flip to ACTIVE.

        Returns (ok, output). On failure the plugin keeps its current installed
        state and the error is stored on the row — the live site is untouched.
        """
        # Tier-1 doctor (static lint) runs FIRST, in this process, so a
        # rule-breaker is refused BEFORE the preflight subprocess could apply any
        # plugin migration. It only reads files + AST-parses (no plugin import),
        # so it is safe here. The boot path never runs it (contract: an
        # already-active plugin stays active across upgrade regardless of new rules).
        from core.services.plugin_doctor import run_doctor

        report = run_doctor(Path(settings.PLUGINS_DIR) / plugin.slug)
        if not report.ok:
            plugin.error_message = report.format()[:4000]
            if plugin.status == Plugin.Status.ACTIVE:
                plugin.status = Plugin.Status.ERRORED
            plugin.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning("Plugin %r refused by doctor (%d fail)", plugin.slug, report.fail_count)
            return False, "Plugin doctor blocked activation:\n" + report.failures_text()

        ok, output = PluginService._run_preflight(plugin.slug)
        if ok:
            plugin.status = Plugin.Status.ACTIVE
            plugin.activated_at = timezone.now()
            plugin.error_message = ""
            plugin.save(update_fields=["status", "activated_at", "error_message", "updated_at"])
            logger.info("Plugin %r activated", plugin.slug)
        else:
            plugin.error_message = output[:4000] or "Preflight failed."
            # Keep it out of the active set; surface that it failed.
            if plugin.status == Plugin.Status.ACTIVE:
                plugin.status = Plugin.Status.ERRORED
            plugin.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning("Plugin %r failed activation preflight", plugin.slug)
        # On success, surface any advisory doctor warnings (non-blocking).
        if ok and report.warn_count:
            output = report.warnings_text()
        return ok, output

    @staticmethod
    def deactivate(plugin: Plugin) -> None:
        plugin.status = Plugin.Status.DISABLED
        plugin.save(update_fields=["status", "updated_at"])
        logger.info("Plugin %r deactivated", plugin.slug)

    @staticmethod
    def delete(plugin: Plugin, *, remove_data: bool = False) -> list:
        """Remove the plugin's files + DB row. Returns a list of warning strings.

        With remove_data, first drops the plugin's own DB tables by unapplying its
        migrations in an isolated subprocess (best effort — a broken plugin can't
        be migrated, in which case files/row are still removed and a warning is
        returned).
        """
        warnings = []
        if remove_data:
            # Plugin Platform v2: owned resources (owner_plugin=slug) are the
            # plugin's real persistence — delete them. Best-effort + field-gated so
            # it never errors on a pre-v2 schema. Owned Scripts cascade to their
            # Runs/Schedules/SecretGrants; owned Secrets/DataStores cascade to
            # their grants/entries. User (owner-NULL) rows are never touched.
            removed = PluginService._cleanup_owned_resources(plugin.slug)
            if removed:
                logger.info("Plugin %r owned-data removed: %s", plugin.slug, removed)

            # Legacy path: a v1 plugin that shipped its own models/migrations still
            # gets its tables dropped in isolation. A v2 plugin ships none, so this
            # is a no-op for it.
            ok, output = PluginService._run_uninstall_data(plugin.slug)
            if not ok:
                warnings.append(
                    "Could not drop plugin data (the plugin may be broken). "
                    "Files and registry entry were still removed."
                )
                logger.warning("Plugin %r data removal failed: %s", plugin.slug, output)

        folder = Path(settings.PLUGINS_DIR) / plugin.slug
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        slug = plugin.slug
        plugin.delete()
        logger.info("Plugin %r deleted (remove_data=%s)", slug, remove_data)
        return warnings

    @staticmethod
    def owned_resource_counts(slug: str) -> dict:
        """Count Script/Secret/DataStore rows owned by ``slug`` (for the delete
        preview). Field-gated + best-effort, so it's safe on a pre-v2 schema."""
        from core.models import DataStore, Script, Secret

        counts = {}
        for model, label in ((Script, "scripts"), (Secret, "secrets"), (DataStore, "datastores")):
            try:
                model._meta.get_field("owner_plugin")
                counts[label] = model.objects.filter(owner_plugin=slug).count()
            except Exception:
                counts[label] = 0
        counts["total"] = sum(counts.values())
        return counts

    @staticmethod
    def _cleanup_owned_resources(slug: str) -> dict:
        """Delete Script/Secret/DataStore rows owned by ``slug``. Returns counts.

        Field-gated (a pre-v2 schema has no ``owner_plugin`` column) and wrapped so
        a cleanup failure can never block plugin removal — the files + registry row
        are still removed by the caller.
        """
        from core.models import DataStore, Script, Secret

        counts = {}
        for model, label in ((Script, "scripts"), (Secret, "secrets"), (DataStore, "datastores")):
            try:
                model._meta.get_field("owner_plugin")  # raises if column absent
                deleted, _ = model.objects.filter(owner_plugin=slug).delete()
                if deleted:
                    counts[label] = deleted
            except Exception as exc:
                logger.warning(
                    "Owned-%s cleanup skipped for plugin %r: %s", label, slug, exc
                )
        return counts

    # ------------------------------------------------------------- restart info

    @staticmethod
    def loaded_slugs() -> set:
        """Plugin slugs actually imported into THIS running process."""
        return {
            app.rsplit(".", 1)[-1]
            for app in getattr(settings, "INSTALLED_PLUGINS", [])
        }

    @staticmethod
    def active_slugs() -> set:
        return set(
            Plugin.objects.filter(status=Plugin.Status.ACTIVE).values_list("slug", flat=True)
        )

    @staticmethod
    def pending_restart() -> bool:
        """True when the active set in the DB differs from what's loaded now."""
        return PluginService.loaded_slugs() != PluginService.active_slugs()

    @staticmethod
    def trigger_restart() -> None:
        """Spawn a detached, plugin-free process that restarts the container."""
        env = os.environ.copy()
        env["PYRUNNER_DISABLE_PLUGINS"] = "1"  # the trigger never loads plugin code
        kwargs = dict(
            cwd=str(settings.BASE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.name == "posix":
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"),
             "plugin_apply_restart", "--delay", "2"],
            **kwargs,
        )
        logger.info("Controlled restart triggered")

    # ------------------------------------------------------------- subprocesses

    @staticmethod
    def _isolated_env(slug: str) -> dict:
        env = os.environ.copy()
        env.pop("PYRUNNER_DISABLE_PLUGINS", None)  # the child MUST load this plugin
        env["PYRUNNER_PREFLIGHT_SLUG"] = slug  # ...and ONLY this one (unguarded)
        env["PYRUNNER_PLUGIN_PREFLIGHT"] = "1"  # make a broken ready() re-raise
        return env

    @staticmethod
    def _run_preflight(slug: str) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"),
                 "plugin_preflight", slug],
                env=PluginService._isolated_env(slug),
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT,
                cwd=str(settings.BASE_DIR),
            )
        except subprocess.TimeoutExpired:
            return False, f"Preflight timed out after {PREFLIGHT_TIMEOUT}s."
        return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()

    @staticmethod
    def _run_uninstall_data(slug: str) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"),
                 "plugin_uninstall", slug],
                env=PluginService._isolated_env(slug),
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT,
                cwd=str(settings.BASE_DIR),
            )
        except subprocess.TimeoutExpired:
            return False, f"Data removal timed out after {PREFLIGHT_TIMEOUT}s."
        return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()
