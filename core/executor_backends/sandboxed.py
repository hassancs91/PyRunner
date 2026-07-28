"""
SandboxedSubprocessBackend — fs/network isolation on top of the local spawn.

This is the optional top layer of FOUNDATIONS Seam 2 (sandbox Stage 2b). It is
``LocalSubprocessBackend`` with the script command wrapped by an auto-detected
process sandboxer (bubblewrap preferred, nsjail supported) so the run gets a
private mount namespace (host filesystem read-only, nothing writable but a
scratch dir) on top of the rlimits floor and the existing process-group /
timeout-kill / pid lifecycle — those are inherited unchanged from the local
backend, since the sandbox is just a command prefix.

Degrades, never collapses
-------------------------
Process sandboxers need unprivileged user namespaces, which many hosts (and the
default Docker seccomp profile) block. So the backend re-detects capability at
run time (the real enforcement; the dashboard cache is only a convenience gate)
and, when the host can't deliver a full sandbox, **falls back to the plain local
spawn (still with rlimits)** rather than failing the run. Protection drops a
tier — nsjail/bwrap+rlimits -> rlimits-only -> plain — it never drops to nothing.

Datastore under sandbox
-----------------------
On SQLite the datastore helper reads ``PYRUNNER_DB_PATH`` directly. Binding that
file back into the sandbox would re-open the cross-tenant raw-read hole the
sandbox exists to close, so when actually sandboxing we **drop
``PYRUNNER_DB_PATH`` from the run env** — the helper then transparently uses the
loopback internal API (``PYRUNNER_INTERNAL_URL`` + token, which the executor
already injects). Network is shared (not unshared) so the loopback API and
ordinary outbound calls keep working; strict egress filtering is intentionally
out of scope (automation scripts need outbound).
"""

import logging
import os
import shutil

from core.executor_backends.base import RunHandle, RunSpec
from core.executor_backends.local import LocalSubprocessBackend
from core.services.sandbox import CAP_FULL, probe_sandbox

logger = logging.getLogger(__name__)

# System directories a Python script legitimately needs to read (interpreter,
# shared libs, CA certs, resolv.conf). Only the ones that exist on the host are
# bound, so this is robust across base images.
_SYSTEM_RO_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

# Backend tool preference: bubblewrap first — it is the simpler, more universal
# sandboxer and the validated wrapper here; nsjail is supported as a fallback.
_BACKEND_TOOLS = ("bwrap", "nsjail")

# Process-level cache of the runtime capability. Capability is a host property,
# stable for the worker's lifetime, so we probe once per process instead of
# spawning a probe child on every run. Reset in tests via reset_runtime_tier().
_runtime_tier = None


def runtime_tier() -> str:
    """Return this process's detected sandbox tier (cached per process)."""
    global _runtime_tier
    if _runtime_tier is None:
        _runtime_tier = probe_sandbox().capability
    return _runtime_tier


def reset_runtime_tier() -> None:
    """Clear the cached tier (test hook; capability can't change in prod mid-life)."""
    global _runtime_tier
    _runtime_tier = None


def _backend_tool() -> "str | None":
    """Absolute path of the first usable sandbox binary for *execution* (bwrap
    preferred), or ``None`` if neither is on PATH."""
    for tool in _BACKEND_TOOLS:
        path = shutil.which(tool)
        if path:
            return path
    return None


def _is_within(path: str, parent: str) -> bool:
    """Whether ``path`` is ``parent`` or lives inside it (plain prefix compare).

    POSIX separators, not ``os.sep``: bwrap/nsjail only exist on Linux, so every
    path reaching this module is a container path — and hardcoding '/' keeps the
    check honest when the suite runs on a Windows dev host.

    Deliberately textual: these paths come from the spec we just built, not from
    the filesystem, so this must not depend on the dirs existing yet.
    """
    if not parent:
        return False
    if path == parent:
        return True
    return path.startswith(parent.rstrip("/") + "/")


def _ro_bind_dirs(spec: RunSpec) -> list:
    """Read-only directories to expose: system dirs + the venv + PYTHONPATH entries.

    Derived from the spec (never from request state): the interpreter is
    ``spec.cmd[0]`` so its venv is two levels up (``venv/bin/python``); the
    script helpers and any other importable trees are already on ``PYTHONPATH``.
    Only existing dirs are returned, so a missing ``/lib64`` etc. is skipped.
    """
    dirs = []

    for d in _SYSTEM_RO_DIRS:
        if os.path.isdir(d):
            dirs.append(d)

    # The environment's venv (…/bin/python -> …/<venv>).
    python_path = spec.cmd[0] if spec.cmd else ""
    venv_dir = os.path.dirname(os.path.dirname(python_path)) if python_path else ""
    if venv_dir and os.path.isdir(venv_dir):
        dirs.append(venv_dir)

    # script_helpers + any other PYTHONPATH trees (so imports resolve), minus the
    # writable workdir and anything inside it (bound rw separately below).
    # "Inside it" matters for Script Libraries: a run's materialized libraries are
    # staged in a per-run dir UNDER the workdir and put on PYTHONPATH, so they
    # arrive in the jail through the workdir's rw bind. Ro-binding them too would
    # be a no-op the later rw bind overmounts — correct only by argv order, which
    # is not a thing to depend on.
    pythonpath = spec.env.get("PYTHONPATH", "") if spec.env else ""
    for entry in pythonpath.split(os.pathsep):
        entry = entry.strip()
        if entry and not _is_within(entry, spec.cwd) and os.path.isdir(entry):
            dirs.append(entry)

    # De-dup, preserve order, and drop any nested under an already-listed dir
    # would be over-engineering — duplicate --ro-bind of the same path is fine,
    # but a plain de-dup keeps the command tidy.
    seen, unique = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def build_bwrap_argv(tool_path: str, spec: RunSpec) -> list:
    """Wrap ``spec.cmd`` in bubblewrap: host fs read-only, writable scratch only."""
    argv = [tool_path]
    for d in _ro_bind_dirs(spec):
        argv += ["--ro-bind", d, d]
    argv += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        # The per-run workdir (holds the temp script + is the only writable tree).
        "--bind", spec.cwd, spec.cwd,
        "--chdir", spec.cwd,
        # Private user/pid/ipc/uts namespaces; network stays shared so the
        # loopback datastore API and outbound calls work (strict egress later).
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--",
    ]
    argv += spec.cmd
    return argv


def build_nsjail_argv(tool_path: str, spec: RunSpec) -> list:
    """Wrap ``spec.cmd`` in nsjail (fallback wrapper; validate on a userns host).

    Mirrors the bwrap profile: standalone "once" mode, host dirs bound read-only,
    a writable workdir, networking left shared. nsjail flag surface differs from
    bwrap, so treat this as the starting point to confirm on a real nsjail host.
    """
    argv = [tool_path, "-Mo", "-q", "--disable_clone_newnet"]
    for d in _ro_bind_dirs(spec):
        argv += ["--bindmount_ro", f"{d}:{d}"]
    argv += [
        "--bindmount", f"{spec.cwd}:{spec.cwd}",
        "--cwd", spec.cwd,
        "--",
    ]
    argv += spec.cmd
    return argv


class SandboxedSubprocessBackend(LocalSubprocessBackend):
    """Local spawn wrapped in bwrap/nsjail when the host can sandbox; otherwise
    a tier-degrading no-op over the local backend."""

    def start(self, spec: RunSpec) -> RunHandle:
        tier = runtime_tier()
        tool_path = _backend_tool()

        if tier != CAP_FULL or not tool_path:
            # Host can't deliver a full sandbox right now — run plain + rlimits.
            # (Never "protected -> nothing"; the rlimits floor still applies.)
            logger.warning(
                "Sandbox unavailable (tier=%s, tool=%s); running with rlimits-only/plain.",
                tier, tool_path or "none",
            )
            return super().start(spec)

        return super().start(self._wrap(spec, tool_path))

    def _wrap(self, spec: RunSpec, tool_path: str) -> RunSpec:
        """Build the sandbox-wrapped RunSpec: command prefixed by the sandboxer,
        and ``PYRUNNER_DB_PATH`` dropped so the datastore helper uses the loopback
        API instead of the raw SQLite file."""
        env = dict(spec.env)
        env.pop("PYRUNNER_DB_PATH", None)  # force the internal-API datastore path

        tool = os.path.basename(tool_path)
        if tool == "nsjail":
            cmd = build_nsjail_argv(tool_path, spec)
        else:
            cmd = build_bwrap_argv(tool_path, spec)

        return RunSpec(
            cmd=cmd,
            env=env,
            cwd=spec.cwd,
            timeout=spec.timeout,
            limits=spec.limits,
        )
