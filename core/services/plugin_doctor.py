"""
Plugin "doctor" — a pre-activation rules check (Plugin Platform v2, Stage 4).

Tier-1 STATIC LINT: inspects a plugin folder WITHOUT importing or executing any
plugin code (file checks + ``ast.parse`` only), so it is safe to run on untrusted
files. It enforces the structural conventions that keep a plugin from shadowing or
breaking core, and the v2 rule that plugins ship NO models/migrations (so no
plugin DDL can ever reach a core table).

Severity is data-driven: a ``fail`` blocks activation; a ``warn`` is advisory. The
dynamic Tier-2 checks (import + migrate + URL-resolve + the light-import
assertion) stay in ``plugin_preflight`` and run in an isolated subprocess.

Used by:
  * ``PluginService.activate`` — runs this BEFORE the preflight subprocess, so a
    rule-breaker is refused before any plugin migration could be applied;
  * ``manage.py plugin_doctor <slug | --path ./folder>`` — for developers, works
    on a local folder with no upload.
"""

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PASS, WARN, FAIL = "pass", "warn", "fail"

# A slug that shadows one of these would collide with a real app / URL root.
RESERVED_SLUGS = {"core", "theme", "landing", "plugins", "admin", "static", "api"}
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Stage 6 (marketplace-prep metadata) validation.
# Semver MAJOR.MINOR.PATCH with optional pre-release / build metadata.
SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
PUBLISHER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Icons are served via <img src>; bitmap + svg are safe in that context (svg is
# never inlined). Keep the set small and explicit.
ALLOWED_ICON_EXTS = {".png", ".svg", ".webp", ".jpg", ".jpeg"}
SUPPORTED_MANIFEST_VERSIONS = {1}
# Optional string fields that, when present, must be strings.
_STR_META_FIELDS = (
    "name", "summary", "description", "author", "author_url", "license",
    "homepage", "repository", "documentation", "max_pyrunner",
)
# Fields a marketplace listing wants; missing ones are advisory (never block).
_RECOMMENDED_FIELDS = ("author", "license", "summary", "icon")

# core.models at module top in apps.py breaks the light-import boot guard (it runs
# before the app registry is ready). core.plugins (incl .api) is import-light.
_CORE_INTERNAL_PREFIXES = ("core.models", "core.tasks", "core.services", "core.executor")

# Plugin API (docs/PLAN_plugin_api.md): GET-only in v1 — the manifest shape is
# POST-ready, but the doctor pins declared methods until writes are designed.
_API_METHODS_ALLOWED = {"GET"}
# Network clients in an api.py are the slow-handler smell: a sync handler can't
# be preemptively killed, so API handlers must stay SDK/DB-read-bound.
_API_NETWORK_IMPORT_ROOTS = {"requests", "httpx", "urllib"}
# The external-API surface (@resource/APIRequest/APIError) shipped in SDK 2.3.
_API_RESOURCES_MIN_SDK = (2, 3)


@dataclass
class Finding:
    rule: str
    severity: str  # PASS / WARN / FAIL
    message: str


@dataclass
class DoctorReport:
    slug: str
    findings: list = field(default_factory=list)

    def add(self, rule, severity, message):
        self.findings.append(Finding(rule, severity, message))

    @property
    def ok(self) -> bool:
        """True when nothing blocks activation (no FAIL findings)."""
        return not any(f.severity == FAIL for f in self.findings)

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == WARN)

    def format(self) -> str:
        glyph = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
        lines = [f"Plugin doctor — {self.slug}"]
        for f in self.findings:
            lines.append(f"  [{glyph[f.severity]}] {f.rule}: {f.message}")
        lines.append(f"  => {self.fail_count} fail, {self.warn_count} warn")
        return "\n".join(lines)

    def failures_text(self) -> str:
        """Just the blocking findings, for the activation error message."""
        fails = [f for f in self.findings if f.severity == FAIL]
        return "\n".join(f"• {f.rule}: {f.message}" for f in fails)

    def warnings_text(self) -> str:
        """Just the advisory findings, shown but non-blocking on a successful activation."""
        warns = [f for f in self.findings if f.severity == WARN]
        return "\n".join(f"• {f.rule}: {f.message}" for f in warns)


def run_doctor(path) -> DoctorReport:
    """Run all Tier-1 static checks on a plugin folder; return a DoctorReport."""
    folder = Path(path).resolve()
    report = DoctorReport(slug=folder.name)

    if not folder.is_dir():
        report.add("structure", FAIL, f"{folder} is not a directory")
        return report

    _check_slug(folder, report)
    _check_manifest(folder, report)
    _check_metadata(folder, report)
    _check_package_files(folder, report)
    _check_no_ddl(folder, report)
    _check_apps(folder, report)
    _check_urls(folder, report)
    _check_asset_shadow(folder, report)
    _check_sdk_usage(folder, report)
    _check_api_resources(folder, report)
    _check_public_pages(folder, report)
    return report


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def _check_slug(folder: Path, report: DoctorReport):
    slug = folder.name
    if not SLUG_RE.match(slug):
        report.add("slug", FAIL, f"'{slug}' is not a valid slug (lowercase letter, then letters/digits/underscores).")
    elif slug in RESERVED_SLUGS:
        report.add("slug", FAIL, f"'{slug}' is a reserved name and would shadow a core app.")
    else:
        report.add("slug", PASS, f"slug '{slug}' is valid and not reserved.")


def _check_manifest(folder: Path, report: DoctorReport):
    path = folder / "plugin.json"
    if not path.exists():
        report.add("manifest", FAIL, "plugin.json is missing.")
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        report.add("manifest", FAIL, f"plugin.json is not valid JSON: {exc}")
        return
    if not isinstance(manifest, dict):
        report.add("manifest", FAIL, "plugin.json must be a JSON object.")
    elif manifest.get("slug") != folder.name:
        report.add("manifest", FAIL, f"plugin.json slug ({manifest.get('slug')!r}) must match the folder name ({folder.name!r}).")
    else:
        report.add("manifest", PASS, "plugin.json present and slug matches the folder.")


def _check_metadata(folder: Path, report: DoctorReport):
    """Validate the marketplace-prep manifest fields (Stage 6).

    FAIL on *malformed* values (bad semver, icon escaping ``<slug>/`` or an
    unsupported type/extension, unknown ``manifest_version``, wrong ``provisions``
    shape, wrong field types). WARN on *missing* recommended fields
    (``author``/``license``/``summary``/``icon``). All fields are optional, so a
    legacy manifest with only slug/name/version activates cleanly (warnings only).
    """
    path = folder / "plugin.json"
    if not path.exists():
        return  # already FAILed in _check_manifest
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return  # already FAILed in _check_manifest
    if not isinstance(manifest, dict):
        return

    failures = []  # collect malformed-value messages

    # --- manifest_version: when present, must be a supported format version.
    if "manifest_version" in manifest:
        mv = manifest["manifest_version"]
        if not isinstance(mv, int) or isinstance(mv, bool) or mv not in SUPPORTED_MANIFEST_VERSIONS:
            failures.append(
                f"manifest_version {mv!r} is not supported (supported: "
                f"{sorted(SUPPORTED_MANIFEST_VERSIONS)})."
            )

    # --- version: when present, must be semver (update-detection depends on it).
    version = manifest.get("version")
    if version is not None and (not isinstance(version, str) or not SEMVER_RE.match(version)):
        failures.append(f"version {version!r} is not valid semver (MAJOR.MINOR.PATCH).")

    # --- string fields must be strings when present.
    for field_name in _STR_META_FIELDS:
        if field_name in manifest and not isinstance(manifest[field_name], str):
            failures.append(f"'{field_name}' must be a string.")

    # --- publisher: marketplace namespace; format-validated when present.
    publisher = manifest.get("publisher")
    if publisher is not None and (
        not isinstance(publisher, str) or not PUBLISHER_RE.match(publisher)
    ):
        failures.append(
            f"publisher {publisher!r} must be lowercase letters/digits/_/- "
            "(it forms the marketplace id publisher/slug)."
        )

    # --- categories / keywords: lists of strings when present.
    for field_name in ("categories", "keywords"):
        if field_name in manifest:
            value = manifest[field_name]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                failures.append(f"'{field_name}' must be a list of strings.")

    # --- icon: bundled file, must resolve UNDER the plugin folder + allowed ext.
    icon = manifest.get("icon")
    if icon is not None:
        if not isinstance(icon, str) or not icon.strip():
            failures.append("'icon' must be a non-empty relative path string.")
        else:
            norm = icon.replace("\\", "/")
            parts = [p for p in norm.split("/") if p not in ("", ".")]
            ext = ("." + norm.rsplit(".", 1)[-1].lower()) if "." in norm else ""
            if norm.startswith("/") or any(p == ".." for p in parts):
                failures.append(f"'icon' path {icon!r} must stay inside the plugin folder.")
            elif ext not in ALLOWED_ICON_EXTS:
                failures.append(
                    f"'icon' must be one of {sorted(ALLOWED_ICON_EXTS)} (got {ext or 'no extension'})."
                )
            elif not (folder / norm).is_file():
                # Declared but not shipped — advisory (a cosmetic miss, not a security/break).
                report.add("metadata-icon", WARN, f"'icon' {icon!r} is declared but the file is missing.")

    # --- provisions: declared resource counts + secret_keys (the trust surface).
    provisions = manifest.get("provisions")
    if provisions is not None:
        if not isinstance(provisions, dict):
            failures.append("'provisions' must be an object.")
        else:
            for count_key in ("scripts", "secrets", "datastores", "databases", "libraries", "schedules"):
                if count_key in provisions:
                    n = provisions[count_key]
                    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                        failures.append(f"provisions.{count_key} must be a non-negative integer.")
            if "secret_keys" in provisions:
                sk = provisions["secret_keys"]
                if not isinstance(sk, list) or not all(isinstance(v, str) for v in sk):
                    failures.append("provisions.secret_keys must be a list of strings.")

    if failures:
        report.add("metadata", FAIL, " ".join(failures))

    # --- advisory: recommended marketplace fields that are missing.
    missing = [f for f in _RECOMMENDED_FIELDS if not manifest.get(f)]
    if missing:
        report.add(
            "metadata", WARN,
            f"missing recommended field(s): {', '.join(missing)} "
            "(required when publishing to a marketplace).",
        )
    elif not failures:
        report.add("metadata", PASS, "manifest metadata is well-formed.")


def _check_package_files(folder: Path, report: DoctorReport):
    missing = [f for f in ("__init__.py", "apps.py") if not (folder / f).exists()]
    if missing:
        report.add("package", FAIL, f"missing required file(s): {', '.join(missing)}.")
    else:
        report.add("package", PASS, "__init__.py and apps.py present.")


def _check_no_ddl(folder: Path, report: DoctorReport):
    offenders = []
    if (folder / "models.py").exists():
        offenders.append("models.py")
    if (folder / "migrations").is_dir():
        offenders.append("migrations/")
    if offenders:
        report.add(
            "no-ddl", FAIL,
            f"plugin ships {', '.join(offenders)} — plugins persist via owned DataStores, "
            "never their own models/migrations (no plugin DDL ever reaches the DB).",
        )
    else:
        report.add("no-ddl", PASS, "no models.py / migrations/ (DataStores are the persistence layer).")


def _check_apps(folder: Path, report: DoctorReport):
    path = folder / "apps.py"
    if not path.exists():
        return  # already FAILed in _check_package_files
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        report.add("apps", FAIL, f"apps.py has a syntax error: {exc}")
        return

    slug = folder.name

    # Exactly one PluginAppConfig subclass, with name/label matching the slug.
    configs = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(_base_name(b) == "PluginAppConfig" for b in node.bases)
    ]
    if len(configs) != 1:
        report.add("apps", FAIL, f"apps.py must define exactly one PluginAppConfig subclass (found {len(configs)}).")
    else:
        attrs = _class_str_attrs(configs[0])
        if attrs.get("name") != f"plugins.{slug}":
            report.add("apps", FAIL, f"AppConfig.name must be 'plugins.{slug}' (found {attrs.get('name')!r}).")
        elif attrs.get("label") != slug:
            report.add("apps", FAIL, f"AppConfig.label must be '{slug}' (found {attrs.get('label')!r}).")
        else:
            report.add("apps", PASS, "apps.py defines one PluginAppConfig with matching name/label.")

    # Module-top imports: core internals are fatal; other third-party imports warn.
    for node in tree.body:
        for module, is_core_internal, is_allowed in _iter_top_imports(node):
            if is_core_internal:
                report.add("apps-imports", FAIL,
                           f"apps.py imports '{module}' at module top — this breaks the light-import "
                           "boot guard. Import core lazily inside functions, or use core.plugins.api.")
            elif not is_allowed:
                report.add("apps-imports", WARN,
                           f"apps.py imports '{module}' at module top — heavy/third-party imports belong "
                           "inside functions (run third-party code in an Environment), not in apps.py.")


def _check_urls(folder: Path, report: DoctorReport):
    path = folder / "urls.py"
    if not path.exists():
        report.add("urls", PASS, "no urls.py (plugin has no routes).")
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        report.add("urls", FAIL, f"urls.py has a syntax error: {exc}")
        return
    app_name = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "app_name":
                    if isinstance(node.value, ast.Constant):
                        app_name = node.value.value
    if app_name is None:
        report.add("urls", FAIL, f"urls.py must set app_name = '{folder.name}'.")
    elif app_name != folder.name:
        report.add("urls", FAIL, f"urls.py app_name must be '{folder.name}' (found {app_name!r}).")
    else:
        report.add("urls", PASS, f"urls.py app_name == '{folder.name}'.")


def _check_asset_shadow(folder: Path, report: DoctorReport):
    """Templates/static must live under <slug>/ so they can't shadow a core template."""
    slug = folder.name
    offenders = []
    for sub in ("templates", "static"):
        root = folder / sub
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file():
                first = f.relative_to(root).parts[0]
                if first != slug:
                    offenders.append(f"{sub}/{f.relative_to(root).as_posix()}")
    if offenders:
        sample = ", ".join(offenders[:5]) + (" …" if len(offenders) > 5 else "")
        report.add("asset-shadow", FAIL,
                   f"templates/static must be namespaced under '{slug}/' to avoid shadowing core "
                   f"assets. Move: {sample}")
    else:
        report.add("asset-shadow", PASS, "templates/static (if any) are namespaced under the slug.")


def _check_sdk_usage(folder: Path, report: DoctorReport):
    """Advisory: prefer core.plugins.api over importing core internals directly."""
    hits = []
    for py in folder.rglob("*.py"):
        if py.name == "apps.py":
            continue  # handled (fatally) by _check_apps
        if py.name == "tests.py" or py.name.startswith("test_"):
            continue  # tests legitimately import core.models to assert behavior
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(p) for p in _CORE_INTERNAL_PREFIXES):
                    hits.append(py.name)
                    break
    if hits:
        report.add("sdk-usage", WARN,
                   "imports core internals directly (" + ", ".join(sorted(set(hits)))
                   + ") — prefer the stable SDK core.plugins.api where possible.")
    else:
        report.add("sdk-usage", PASS, "no direct core-internal imports outside apps.py.")


def _check_api_resources(folder: Path, report: DoctorReport):
    """Plugin API — manifest ⇄ api.py drift (docs/PLAN_plugin_api.md, Stage 3).

    Declared ∧ registered = servable, so drift is the failure mode: a declared
    resource with no ``@resource`` handler can never serve (FAIL pre-ship); a
    marked handler that isn't declared is dead code the dispatcher will 404
    (WARN — the manifest stays truthful, same discipline as ``provisions``).
    Static AST only, like every Tier-1 check — api.py is never imported.
    """
    manifest = _manifest_dict(folder)
    failures = []

    declared_entries = []
    provides = (manifest or {}).get("provides")
    if provides is not None and not isinstance(provides, dict):
        failures.append("'provides' must be an object.")
        provides = None
    if isinstance(provides, dict) and provides.get("api_resources") is not None:
        raw = provides["api_resources"]
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            failures.append("provides.api_resources must be a list of objects.")
        else:
            declared_entries = raw

    api_path = folder / "api.py"
    api_tree = None
    marked = set()
    if api_path.exists():
        try:
            api_tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
        except (SyntaxError, OSError) as exc:
            report.add("api-resources", FAIL, f"api.py cannot be parsed: {exc}")
            return
        marked = _marked_api_resources(api_tree)

    if not declared_entries and not marked and not failures:
        report.add("api-resources", PASS, "no external API resources declared.")
        return

    declared_names = set()
    for entry in declared_entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            failures.append("every api_resources entry needs a non-empty string 'name'.")
            continue
        if name in declared_names:
            failures.append(f"resource {name!r} is declared more than once.")
        declared_names.add(name)
        methods = entry.get("methods", ["GET"])
        if (
            not isinstance(methods, list)
            or not methods
            or not set(methods) <= _API_METHODS_ALLOWED
        ):
            failures.append(
                f"resource {name!r}: methods must be [\"GET\"] "
                "(the plugin API is read-only in v1)."
            )

    if declared_names and not api_path.exists():
        failures.append("provides.api_resources is declared but api.py is missing.")

    unregistered = declared_names - marked
    if unregistered and api_path.exists():
        failures.append(
            "declared resource(s) with no @resource handler in api.py: "
            + ", ".join(sorted(unregistered))
            + "."
        )

    if declared_names:
        api_version = (manifest or {}).get("api")
        if not _version_at_least(api_version, _API_RESOURCES_MIN_SDK):
            failures.append(
                f'manifest "api" must be >= "2.3" to declare api_resources '
                f"(found {api_version!r})."
            )

    if failures:
        report.add("api-resources", FAIL, " ".join(failures))
    else:
        report.add(
            "api-resources", PASS,
            f"{len(declared_names)} declared API resource(s), each with a handler.",
        )

    undeclared = marked - declared_names
    if undeclared:
        report.add(
            "api-resources", WARN,
            "@resource handler(s) not declared in plugin.json "
            "(the dispatcher will 404 them): " + ", ".join(sorted(undeclared)) + ".",
        )

    if api_tree is not None:
        network = _import_roots(api_tree) & _API_NETWORK_IMPORT_ROOTS
        if network:
            report.add(
                "api-imports", WARN,
                "api.py imports " + ", ".join(sorted(network))
                + " — network calls in an API handler are the slow-handler smell; "
                "handlers should be SDK/DB-read-bound.",
            )


def _check_public_pages(folder: Path, report: DoctorReport):
    """Public pages — manifest ⇄ api.py drift, same discipline as api_resources.

    A declared page with no ``@page`` handler can never render (FAIL); a marked
    handler that isn't declared is dead code the public view will 404 (WARN).
    """
    manifest = _manifest_dict(folder)
    failures = []

    declared_entries = []
    provides = (manifest or {}).get("provides")
    if isinstance(provides, dict) and provides.get("public_pages") is not None:
        raw = provides["public_pages"]
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            failures.append("provides.public_pages must be a list of objects.")
        else:
            declared_entries = raw

    api_path = folder / "api.py"
    marked = set()
    if api_path.exists():
        try:
            tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
        except (SyntaxError, OSError):
            return  # already FAILed in _check_api_resources
        marked = _marked_decorator_names(tree, "page")

    if not declared_entries and not marked and not failures:
        report.add("public-pages", PASS, "no public pages declared.")
        return

    declared_names = set()
    for entry in declared_entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            failures.append("every public_pages entry needs a non-empty string 'name'.")
            continue
        if name in declared_names:
            failures.append(f"page {name!r} is declared more than once.")
        declared_names.add(name)

    if declared_names and not api_path.exists():
        failures.append("provides.public_pages is declared but api.py is missing.")

    unregistered = declared_names - marked
    if unregistered and api_path.exists():
        failures.append(
            "declared page(s) with no @page handler in api.py: "
            + ", ".join(sorted(unregistered))
            + "."
        )

    if declared_names:
        api_version = (manifest or {}).get("api")
        if not _version_at_least(api_version, _API_RESOURCES_MIN_SDK):
            failures.append(
                f'manifest "api" must be >= "2.3" to declare public_pages '
                f"(found {api_version!r})."
            )

    if failures:
        report.add("public-pages", FAIL, " ".join(failures))
    else:
        report.add(
            "public-pages", PASS,
            f"{len(declared_names)} declared public page(s), each with a handler.",
        )

    undeclared = marked - declared_names
    if undeclared:
        report.add(
            "public-pages", WARN,
            "@page handler(s) not declared in plugin.json "
            "(the public view will 404 them): " + ", ".join(sorted(undeclared)) + ".",
        )


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #

def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _manifest_dict(folder: Path):
    """plugin.json as a dict, or None (missing/invalid is FAILed elsewhere)."""
    path = folder / "plugin.json"
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _version_at_least(value, minimum: tuple) -> bool:
    """True when a manifest version string like "2.3" is >= ``minimum``."""
    if not isinstance(value, str):
        return False
    try:
        parts = tuple(int(p) for p in value.split("."))
    except ValueError:
        return False
    return parts >= minimum


def _marked_decorator_names(tree, decorator: str) -> set:
    """Names bound with @<decorator>("name") on functions in a module AST."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and _base_name(dec.func) == decorator
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ):
                names.add(dec.args[0].value)
    return names


def _marked_api_resources(tree) -> set:
    """Resource names bound with @resource("name") in an api.py AST."""
    return _marked_decorator_names(tree, "resource")


def _import_roots(tree) -> set:
    """Top-level root packages imported anywhere in a module's AST."""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _class_str_attrs(classdef) -> dict:
    """Collect simple ``name = "literal"`` class attributes as a dict."""
    attrs = {}
    for node in classdef.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    attrs[target.id] = node.value.value
    return attrs


def _iter_top_imports(node):
    """Yield (module, is_core_internal, is_allowed) for a module-top import node."""
    stdlib = getattr(sys, "stdlib_module_names", set())

    def classify(module):
        if module is None:
            return (module, False, True)  # relative import — fine
        is_core_internal = any(module.startswith(p) for p in _CORE_INTERNAL_PREFIXES)
        root = module.split(".")[0]
        allowed = (
            module == "core.plugins"
            or module.startswith("core.plugins.")
            or root == "django"
            or root in stdlib
        )
        return (module, is_core_internal, allowed)

    if isinstance(node, ast.Import):
        for alias in node.names:
            yield classify(alias.name)
    elif isinstance(node, ast.ImportFrom):
        # `from . import x` (level>0) is relative → module may be None.
        module = node.module if node.level == 0 else None
        yield classify(module)
