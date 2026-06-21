"""
RunBackend interface + the data passed across the seam.

Deliberately dependency-light: no imports from ``core.models``/services/executor,
so a backend module can be reasoned about (and unit-tested) in isolation.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class RunSpec:
    """Everything core prepares for one execution; backend-agnostic."""

    cmd: list
    env: dict
    cwd: str
    timeout: Optional[int] = None
    # Optional resource caps (posix only):
    # {"memory_bytes", "cpu_seconds", "nproc", "fsize_bytes"}.
    # None = off (the default), reproducing today's behavior.
    limits: Optional[dict] = None


@dataclass
class RunResult:
    """What a backend reports back; core maps it to the Run row.

    ``stdout``/``stderr`` are returned RAW (unmasked, untruncated) — masking and
    truncation stay in core so the resolved-secret set lives in exactly one place.
    On a timeout, core forces ``exit_code = -1`` regardless of what is reported.
    """

    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    timed_out: bool = False


@dataclass
class RunHandle:
    """An opaque handle to a started execution.

    ``pid`` is what core records on the Run for force-stop (local backend).
    ``native`` carries the backend's private object (the ``Popen`` for local; a
    container id for a future backend) and is never read by core.
    """

    pid: Optional[int]
    native: Any = None


class RunBackend(ABC):
    """Spawn a prepared command, wait for it (enforcing the timeout + killing the
    tree internally), and kill it on demand. Stateless w.r.t. the Run lifecycle."""

    @abstractmethod
    def start(self, spec: RunSpec) -> RunHandle:
        """Launch the command and return a handle (records ``pid`` for local)."""

    @abstractmethod
    def wait(self, handle: RunHandle, timeout: Optional[int]) -> RunResult:
        """Wait up to ``timeout`` seconds; on timeout, kill the tree, drain
        output, and return ``timed_out=True``."""

    @abstractmethod
    def kill(self, handle: RunHandle) -> None:
        """Force-kill the execution's process tree (for external force-stop)."""
