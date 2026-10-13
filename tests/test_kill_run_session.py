"""Session-scoped kill of dispatched subprocess trees (kb-03965).

``kill_run_session`` exists because the worker launches every agent and gate
subprocess as ``sudo -u dispatch -H <cmd>``; sudo cannot relay SIGKILL to its
child, so killing the sudo wrapper's pid orphaned the agent and every command
it had spawned (one such tree ran 19 hours on one host).  The fix is to
launch the wrapper as a NEW SESSION LEADER (``start_new_session=True``) and,
on timeout/cancel, signal every process in that session by session id.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def _spawn_with_pidfile(pidfile: Path) -> tuple[subprocess.Popen[bytes], int]:
    """Spawn the production-shaped tree: a session-leading ``sh -c`` whose
    grandchild ``sleep 300`` runs in its OWN process group (``set -m``), the
    way talos >= 0.15.0-11 places each of its commands.

    ``setsid -f`` would NOT do: it detaches the grandchild into a NEW session,
    so a session kill could never reach it — which would make the test prove
    nothing.  ``set -m`` keeps the grandchild inside the sh's session while
    moving it to its own process group, which is exactly the production
    hazard: a process-GROUP kill would miss it, a session kill must not.
    """
    proc = subprocess.Popen(
        ["sh", "-c", f"set -m; sleep 300 & echo $! > {pidfile}; wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    gc_pid = -1
    while time.monotonic() < deadline:
        try:
            gc_pid = int(pidfile.read_text().strip())
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    assert gc_pid > 0, "grandchild pidfile never appeared"
    return proc, gc_pid


def _pid_running(pid: int) -> bool:
    """True while <pid> exists and is not a mere unreaped zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # Fields after the comm "(...)" block: state is the first one.
    tail = stat[stat.rindex(")") + 1 :].split()
    return len(tail) > 0 and tail[0] != "Z"


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


class TestKillRunSessionRealProcesses:
    """AC-6: single-user mode (AGENT_SUBPROCESS_USER unset), real processes."""

    def test_session_kill_reaches_grandchild_in_own_process_group(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        proc, gc_pid = _spawn_with_pidfile(tmp_path / "gc.pid")
        try:
            dispatch.kill_run_session(proc, signal.SIGKILL)
            assert _wait_gone(gc_pid), f"grandchild {gc_pid} survived a session SIGKILL"
            proc.wait()
        finally:
            if _pid_running(gc_pid):  # pragma: no cover - failure path cleanup
                with contextlib.suppress(ProcessLookupError):
                    os.kill(gc_pid, signal.SIGKILL)

    def test_proc_kill_alone_leaves_grandchild_alive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Documents the bug: proc.kill() hits only the wrapper's pid."""
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        proc, gc_pid = _spawn_with_pidfile(tmp_path / "gc.pid")
        try:
            proc.kill()
            proc.wait()
            # Give the reaper a moment, then the grandchild must still run:
            # sudo-less sh is dead, its sleep 300 keeps going as an orphan.
            time.sleep(0.5)
            assert _pid_running(gc_pid), "grandchild died with a bare proc.kill()"
            assert not _pid_running(proc.pid)
        finally:
            if _pid_running(gc_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(gc_pid, signal.SIGKILL)
                _wait_gone(gc_pid)


class TestKillRunSessionSudoPath:
    """AC-7: sudo mode invokes pkill via sudo -n -u <user> -s <sid>."""

    def test_sudo_argv_and_exit_1_swallowed(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        proc = MagicMock()
        proc.pid = 4242
        with patch.object(dispatch.subprocess, "run") as run:
            dispatch.kill_run_session(proc, signal.SIGKILL)
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv == [
            "sudo",
            "-n",
            "-u",
            "dispatch",
            "pkill",
            "-KILL",
            "-s",
            "4242",
        ]
        assert run.call_args.kwargs.get("timeout") == 10
        proc.kill.assert_called_once()
        proc.terminate.assert_not_called()

        # pkill exit 1 ("no processes matched") is not an error — no raise,
        # and the wrapper itself is still signalled.
        proc2 = MagicMock()
        proc2.pid = 5151
        with patch.object(
            dispatch.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "pkill"),
        ) as run2:
            dispatch.kill_run_session(proc2, signal.SIGKILL)
        assert run2.call_args.args[0][-4:] == ["pkill", "-KILL", "-s", "5151"]
        proc2.kill.assert_called_once()

    def test_sigterm_maps_to_terminate_and_other_failures_swallowed(
        self, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        proc = MagicMock()
        proc.pid = 777
        with patch.object(
            dispatch.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(2, "pkill"),
        ):
            dispatch.kill_run_session(proc, signal.SIGTERM)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
