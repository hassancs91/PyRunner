"""
PyRun plugin system — registry, the plugin contract, and the safe AppConfig.

This module is the public *contract* a plugin author codes against. It is kept
deliberately light: importing it must never require the Django app registry to
be ready, and it must never import any plugin code. Deciding *what* is active and
*importing it guarded* lives in ``pyrunner/settings.py`` and ``pyrunner/urls.py``
so that a broken plugin can never take down core.

Safety note: plugins register themselves from their ``AppConfig.ready()``, which
is wrapped so a registration error is swallowed on the live server (the plugin
simply doesn't appear) but *re-raised* inside the isolated ``plugin_preflight``
subprocess (env ``PYRUNNER_PLUGIN_PREFLIGHT=1``) so the plugin can be quarantined
before the real server ever loads it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# A plugin slug must be a valid Python identifier segment so ``import
# plugins.<slug>`` resolves, and must be safe to interpolate into a URL path.
PLUGIN_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Set in the isolated preflight subprocess so that a registration failure in
# ready() is raised (failing the subprocess) instead of being swallowed as it is
# on the live server.
PREFLIGHT_FLAG_ENV = "PYRUNNER_PLUGIN_PREFLIGHT"


def is_valid_plugin_slug(slug) -> bool:
    """Return True if ``slug`` is a safe, importable plugin slug."""
    return bool(slug and isinstance(slug, str) and PLUGIN_SLUG_RE.match(slug))


@dataclass
class NavItem:
    """A console-sidebar entry contributed by a plugin."""

    label: str
    url_name: str  # e.g. "my_flows:index"
    icon_svg: str = ""  # optional inline SVG <path/>; the UI falls back to a default
    superuser_only: bool = False


@dataclass
class PyRunnerPlugin:
    """Descriptor a plugin exposes (usually constructed in its ``apps.py``)."""

    slug: str
    name: str
    version: str = "0.0.0"
    nav_items: list = field(default_factory=list)


_REGISTRY: dict[str, PyRunnerPlugin] = {}


def register(plugin: PyRunnerPlugin) -> None:
    """Add a plugin to the in-process registry (called from ready())."""
    if not is_valid_plugin_slug(plugin.slug):
        raise ValueError(f"Invalid plugin slug: {plugin.slug!r}")
    _REGISTRY[plugin.slug] = plugin


def unregister(slug: str) -> None:
    _REGISTRY.pop(slug, None)


def get_plugin(slug: str) -> PyRunnerPlugin | None:
    return _REGISTRY.get(slug)


def all_plugins() -> list:
    return list(_REGISTRY.values())


def run_in_environment(environment, **kwargs):
    """Run plugin compute in a PyRun environment's venv (isolated subprocess).

    Thin re-export of ``core.executor.run_in_environment`` so plugin authors have
    a single import surface (``from core.plugins import run_in_environment``).
    Imported lazily to keep this contract module light. See the executor for the
    full signature: ``code=`` / ``path=``, ``args=``, ``timeout=`` →
    ``(exit_code, stdout, stderr)``.
    """
    from core.executor import run_in_environment as _impl

    return _impl(environment, **kwargs)


def nav_for(user) -> list:
    """Nav items visible to ``user`` (superuser-only items filtered for others)."""
    items = []
    for plugin in _REGISTRY.values():
        for item in plugin.nav_items:
            if item.superuser_only and not (user and getattr(user, "is_superuser", False)):
                continue
            items.append(item)
    return items


class PluginAppConfig(AppConfig):
    """Base ``AppConfig`` for plugins.

    A plugin's ``apps.py`` subclasses this and sets ``plugin`` to a
    ``PyRunnerPlugin`` instance. ``ready()`` registers it.

    Registration is guarded so that on the live server a broken plugin never
    crashes app population — *except* inside the preflight subprocess, where we
    deliberately let the failure surface so the plugin can be quarantined before
    the real server ever loads it.

    AppConfig discovery note: a plugin's apps.py naturally does
    ``from core.plugins import PluginAppConfig``, which makes BOTH this base and
    the plugin's own subclass visible in that module. Django's auto-discovery
    picks the single AppConfig candidate, so we exclude this base from being a
    candidate (``default = False``) while marking every subclass as one
    (``__init_subclass__`` sets ``default = True``). Without this, Django finds
    two candidates, gives up, and silently falls back to a plain AppConfig — so
    the plugin's ready()/descriptor would never run.
    """

    plugin: PyRunnerPlugin | None = None

    # Exclude THIS base from Django's AppConfig auto-discovery; subclasses are
    # re-included below so a plugin's own config is the sole candidate.
    default = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.default = True

    def ready(self):
        if self.plugin is None:
            return
        try:
            register(self.plugin)
        except Exception:
            logger.exception("Plugin %r failed to register", self.label)
            if os.environ.get(PREFLIGHT_FLAG_ENV):
                raise
