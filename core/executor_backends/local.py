"""
LocalSubprocessBackend — today's executor spawn/wait/timeout-kill, verbatim.

This is the default backend. With ``PYRUNNER_RUN_BACKEND`` unset it reproduces
the pre-seam behavior byte-for-byte: same win/posix process-group/session
isolation flags, same ``communicate(timeout=...)``, same kill-the-tree-then-drain
on timeout. The script is launched in its own process group/session so the web
process can later kill the whole job tree (force stop / timeout) without
touching the django-q worker that spawned it.
"""

import logging
import os
import signal
import subprocess

from core.executor_backends.base import RunBackend, RunHandle, RunResult, RunSpec

logger = logging.getLogger(__name__)


def kill_process_tree(pid: int) -> None:
    """
    Kill a script job's entire process tree.

    This targets *only* the child process the executor spawned (the user's
    Python script and anything it spawned) — never the long-lived django-q
    worker. The child is launched in its own process group/session (see
    ``LocalSubprocessBackend.start``) so the kill is isolated from the worker.

    Args:
        pid: PID of the spawned script subprocess (the process-group leader).
    """
    if not pid:
        return
    try:
        if os.name == "nt":
            # /T kills the whole tree (children too), /F forces it.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            # Child was started with start_new_session=True, so it leads its own
            # process group; killing the group takes out the script + children.
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        # Process already gone (or PID reused/invalid) — nothing to do.
        logger.warning(f"Could not kill process tree {pid}: {e}")


def _make_rlimit_preexec(limits: dict):
    """Build a posix ``preexec_fn`` that applies resource caps in the child.

    Runs in the forked child just before ``exec`` (posix only). Each cap is
    applied independently and only when set, so an unconfigured limit leaves the
    inherited (unlimited) value untouched — the sandbox's rlimits floor.
    """

    def _apply():
        import resource

        mem = limits.get("memory_bytes")
        if mem:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = limits.get("cpu_seconds")
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        nproc = limits.get("nproc")
        if nproc:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        fsize = limits.get("fsize_bytes")
        if fsize:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    return _apply


class LocalSubprocessBackend(RunBackend):
    """Run the command as a local subprocess in the PyRunner container."""

    def start(self, spec: RunSpec) -> RunHandle:
        # Launch the script in its own process group/session so the web process
        # can later kill the whole job tree without touching the django-q worker.
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": spec.cwd,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": spec.env,
        }
        if os.name == "nt":
            # CREATE_NO_WINDOW: no console popup. CREATE_NEW_PROCESS_GROUP:
            # isolate the child so signals/kills don't reach the worker.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Child becomes the leader of a new session/process group.
            popen_kwargs["start_new_session"] = True
            # Optional resource caps, applied in the child before exec (posix
            # only). Off unless configured, so the default is unchanged.
            if spec.limits:
                popen_kwargs["preexec_fn"] = _make_rlimit_preexec(spec.limits)

        proc = subprocess.Popen(spec.cmd, **popen_kwargs)
        return RunHandle(pid=proc.pid, native=proc)

    def wait(self, handle: RunHandle, timeout) -> RunResult:
        proc = handle.native
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return RunResult(
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            # Kill the whole job tree (not just the immediate child), then drain
            # whatever output was buffered before the kill.
            kill_process_tree(proc.pid)
            stdout, stderr = proc.communicate()
            return RunResult(
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

    def kill(self, handle: RunHandle) -> None:
        kill_process_tree(handle.pid)
