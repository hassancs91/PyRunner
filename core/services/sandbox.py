"""
Host sandbox capability probe (FOUNDATIONS Seam 2, sandbox Stage 2).

PyRun ships to many self-hosted environments, so the filesystem/network
sandbox can't be a maintainer pre-build gate — its availability is *detected at
runtime per instance*. This module is the single probe used by all three call
sites that need it:

- ``manage.py sandbox_check`` (CLI),
- the dashboard **"Test sandbox on this host"** button (Settings → Isolation),
- worker-side runtime detection before a sandboxed run (the real enforcement;
  the UI gate is only convenience).

The probe answers one question — which protection *tier* can this host actually
deliver — and degrades honestly:

    full          posix + an nsjail/bwrap binary + a minimal unprivileged-userns
                  sandbox actually runs here.
    rlimits_only  posix (so ``setrlimit`` works) but no usable fs/net sandbox
                  (no binary, or userns/seccomp blocks it).
    none          not even rlimits (non-POSIX host, e.g. the Windows dev box).

Tiers degrade *independently*: losing the fs/net sandbox never loses rlimits —
the executor falls back a tier, never to "no protection". The probe is cheap,
side-effect-free, and re-runnable (capability can change on redeploy).
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Preference order: nsjail is batteries-included (mount + seccomp + netns +
# rlimits + time limits in one config); bubblewrap is the simpler, more-universal
# fallback. Whichever is on PATH is auto-detected.
#
# NOTE: execution (executor_backends/sandboxed._BACKEND_TOOLS) prefers bubblewrap
# first — its "validated wrapper". The two orders diverge ONLY on a host with BOTH
# binaries installed (uncommon; the official image ships bubblewrap only), where the
# probe would validate nsjail while a run uses bwrap. Left as-is deliberately: the
# probe answers "can this host sandbox at all?" and surfaces the tool it validated in
# its result detail; forcing the orders to match is a design call on this locked,
# real-stack-verified subsystem, not a mechanical cleanup.
SANDBOX_TOOLS = ("nsjail", "bwrap")

# Capability tiers — must match GlobalSettings.SandboxCapability values.
CAP_FULL = "full"
CAP_RLIMITS_ONLY = "rlimits_only"
CAP_NONE = "none"

# How long the minimal probe child may take before we treat the host as unable
# to sandbox (a hung unshare shouldn't wedge the probe).
_PROBE_TIMEOUT_SECONDS = 10


@dataclass
class SandboxProbeResult:
    """Outcome of a single host probe.

    ``capability`` is the tier above. ``tool`` is the sandbox binary that was
    tried (or None). ``detail`` is a short human-readable explanation (the
    failing step / syscall) suitable for surfacing in the dashboard and CLI.
    """

    capability: str
    tool: Optional[str] = None
    detail: str = ""

    @property
    def is_full(self) -> bool:
        return self.capability == CAP_FULL


def find_sandbox_tool() -> Optional[str]:
    """Return the first available sandbox binary on PATH, or None."""
    for tool in SANDBOX_TOOLS:
        if shutil.which(tool):
            return tool
    return None


def _minimal_sandbox_command(tool: str, tool_path: str) -> list:
    """Build a do-nothing sandboxed command that only succeeds if the host can
    create an unprivileged user namespace and the minimal mounts the real
    sandbox needs. Runs ``true`` inside the jail — no script, no side effects.
    """
    if tool == "bwrap":
        # Unshare a user + mount namespace, bind the host fs read-only, give a
        # /proc and /dev, then run true. Fails loudly if userns is blocked.
        return [
            tool_path,
            "--unshare-user",
            "--unshare-pid",
            "--ro-bind", "/", "/",
            "--proc", "/proc",
            "--dev", "/dev",
            "--die-with-parent",
            "true",
        ]
    # nsjail: standalone "once" mode, clone a new userns, no networking changes,
    # run true. -Mo = run once; -q = quiet; -t 0 = no time limit override here.
    return [
        tool_path,
        "-Mo",
        "-q",
        "--chroot", "/",
        "--rlimit_as", "max",
        "--",
        "true",
    ]


def _run_minimal_sandbox(tool: str) -> SandboxProbeResult:
    """Try to actually run the minimal sandbox; classify success/failure."""
    tool_path = shutil.which(tool)
    if not tool_path:  # pragma: no cover - guarded by caller
        return SandboxProbeResult(CAP_RLIMITS_ONLY, tool=None,
                                  detail=f"{tool} not found on PATH")
    cmd = _minimal_sandbox_command(tool, tool_path)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT_SECONDS,
            text=True,
        )
    except FileNotFoundError as e:
        return SandboxProbeResult(CAP_RLIMITS_ONLY, tool=tool,
                                  detail=f"could not execute {tool}: {e}")
    except subprocess.TimeoutExpired:
        return SandboxProbeResult(
            CAP_RLIMITS_ONLY, tool=tool,
            detail=f"{tool} probe timed out after {_PROBE_TIMEOUT_SECONDS}s "
                   "(unprivileged user namespaces likely blocked)")
    if proc.returncode == 0:
        return SandboxProbeResult(CAP_FULL, tool=tool,
                                  detail=f"{tool}: minimal user-namespace sandbox OK")
    # Non-zero: surface the tool's own error (usually the userns/clone failure)
    # so the operator knows WHY the host can't sandbox.
    err = (proc.stderr or proc.stdout or "").strip()
    err = err.splitlines()[-1] if err else f"exit code {proc.returncode}"
    return SandboxProbeResult(
        CAP_RLIMITS_ONLY, tool=tool,
        detail=f"{tool} could not create a sandbox: {err}")


def probe_sandbox() -> SandboxProbeResult:
    """Detect the protection tier this host can deliver right now.

    Side-effect-free and safe to call from the web process or a worker job. The
    web-process result is accurate for the standard single-container deploy
    (web + worker share the kernel/seccomp); for split deployments the
    authoritative probe is a worker job, but this is a faithful approximation.
    """
    if os.name != "posix":
        # setrlimit and unprivileged userns are POSIX features. The Windows dev
        # box has neither — it always runs scripts plain.
        return SandboxProbeResult(
            CAP_NONE, tool=None,
            detail="non-POSIX host: resource limits and sandbox are unavailable "
                   "(scripts run unrestricted — fine for a dev box)")

    tool = find_sandbox_tool()
    if not tool:
        return SandboxProbeResult(
            CAP_RLIMITS_ONLY, tool=None,
            detail="no nsjail/bwrap binary found — resource limits apply, but "
                   "the filesystem/network sandbox is unavailable")

    return _run_minimal_sandbox(tool)


def run_and_store_probe(settings=None) -> SandboxProbeResult:
    """Probe the host and cache the result on ``GlobalSettings``.

    Stores ``sandbox_capability`` + ``sandbox_checked_at`` so the dashboard can
    surface the active tier and grey out options the host can't deliver. The
    executor still re-detects at run time — this cache is convenience, not the
    enforcement boundary.
    """
    from django.utils import timezone

    from core.models import GlobalSettings

    result = probe_sandbox()
    gs = settings or GlobalSettings.get_settings()
    gs.sandbox_capability = result.capability
    gs.sandbox_checked_at = timezone.now()
    gs.save(update_fields=["sandbox_capability", "sandbox_checked_at", "updated_at"])
    return result
