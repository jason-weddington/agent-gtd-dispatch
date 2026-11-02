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


# ---------------------------------------------------------------------------
# reap_run_session — reap a run's session on EVERY exit path
# ---------------------------------------------------------------------------

import logging  # noqa: E402

import pytest  # noqa: E402


def _exited_proc(pid: int = 4242, returncode: int | None = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    return proc


def _reap_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.getMessage().startswith("session-reap: outcome=")
    ]


@pytest.mark.real_reap
class TestReapRunSessionUnit:
    @pytest.fixture(autouse=True)
    def _single_user(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "CANCEL_GRACE_SECONDS", 1)
        monkeypatch.setattr(dispatch, "_REAP_POLL_SECONDS", 0.01)
        monkeypatch.setattr(dispatch, "_REAP_POST_KILL_WAIT_SECONDS", 0.05)
        # Leader reaped and its pid number free: no PID reuse.
        monkeypatch.setattr(
            dispatch.os, "kill", MagicMock(side_effect=ProcessLookupError)
        )

    def test_clean_exit_one_probe_one_info_record(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        caplog.set_level(logging.INFO, logger=dispatch.logger.name)
        with patch.object(dispatch, "_run_pkill_quietly", return_value=1) as pk:
            out = dispatch.reap_run_session(
                _exited_proc(), label="gate", context="run_id=abc"
            )
        assert out == "clean"
        pk.assert_called_once_with(["pkill", "-0", "-s", "4242"])
        recs = _reap_records(caplog)
        assert len(recs) == 1
        assert recs[0].levelno == logging.INFO
        assert "outcome=clean" in recs[0].getMessage()
        assert "run_id=abc" in recs[0].getMessage()

    def test_members_then_gone_reaped_without_sigkill(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        caplog.set_level(logging.INFO, logger=dispatch.logger.name)
        # probe: members; SIGTERM pkill; poll: gone.
        with (
            patch.object(dispatch, "_run_pkill_quietly", side_effect=[0, 0, 1]) as pk,
            patch.object(dispatch.subprocess, "run") as run,
        ):
            out = dispatch.reap_run_session(_exited_proc(), label="agent")
        assert out == "reaped"
        argvs = [c.args[0] for c in pk.call_args_list]
        assert ["pkill", "-TERM", "-s", "4242"] in argvs
        assert ["pkill", "-KILL", "-s", "4242"] not in argvs
        run.assert_not_called()
        (rec,) = _reap_records(caplog)
        assert rec.levelno == logging.WARNING
        assert "escalated_sigkill=0" in rec.getMessage()

    def test_members_persist_escalates_to_sigkill(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        caplog.set_level(logging.INFO, logger=dispatch.logger.name)
        sent: list[list[str]] = []

        def _pk(argv: list[str]) -> int:
            sent.append(argv)
            if "-KILL" in argv:
                state["killed"] = True
                return 0
            if "-0" in argv:
                return 1 if state["killed"] else 0
            return 0

        state = {"killed": False}
        with patch.object(dispatch, "_run_pkill_quietly", side_effect=_pk):
            out = dispatch.reap_run_session(
                _exited_proc(), label="agent", grace_seconds=0.05
            )
        assert out == "reaped"
        assert ["pkill", "-KILL", "-s", "4242"] in sent
        (rec,) = _reap_records(caplog)
        assert "escalated_sigkill=1 survivors_after_kill=0" in rec.getMessage()

    def test_survivors_after_sigkill_logged_at_error(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        caplog.set_level(logging.INFO, logger=dispatch.logger.name)
        with patch.object(dispatch, "_run_pkill_quietly", return_value=0):
            out = dispatch.reap_run_session(
                _exited_proc(), label="agent", grace_seconds=0.02
            )
        assert out == "reaped"
        (rec,) = _reap_records(caplog)
        assert "escalated_sigkill=1 survivors_after_kill=1" in rec.getMessage()
        assert any(
            r.levelno == logging.ERROR and "members survived SIGKILL" in r.getMessage()
            for r in caplog.records
        )

    def test_mid_poll_pid_reuse_stops_without_sigkill(self) -> None:
        from agent_gtd_dispatch import dispatch

        sent: list[list[str]] = []

        def _pk(argv: list[str]) -> int:
            sent.append(argv)
            return 0

        # First check (before the -0 probe) says not reused; then it flips.
        with (
            patch.object(dispatch, "_session_id_reused", side_effect=[False, True]),
            patch.object(dispatch, "_run_pkill_quietly", side_effect=_pk),
        ):
            out = dispatch.reap_run_session(_exited_proc(), label="agent")
        assert out == "reaped"
        assert ["pkill", "-TERM", "-s", "4242"] in sent
        assert not any("-KILL" in a for a in sent)

    @pytest.mark.parametrize("rc", [-2, -1, 2])
    def test_probe_error_sends_no_signal(self, rc: int) -> None:
        from agent_gtd_dispatch import dispatch

        with patch.object(dispatch, "_run_pkill_quietly", return_value=rc) as pk:
            out = dispatch.reap_run_session(_exited_proc(), label="agent")
        assert out == "probe_error"
        pk.assert_called_once()

    def test_never_raises(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        with patch.object(dispatch, "_run_pkill_quietly", side_effect=RuntimeError):
            out = dispatch.reap_run_session(
                _exited_proc(), label="agent", context="run_id=zzz"
            )
        assert out == "probe_error"
        assert any("session-reap failed" in r.getMessage() for r in caplog.records)

    def test_sudo_refused_probe_is_probe_error(self, caplog, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        err = subprocess.CalledProcessError(
            1, ["sudo"], stderr=b"sudo: a password is required"
        )
        with patch.object(dispatch.subprocess, "run", side_effect=err):
            assert dispatch._run_pkill_quietly(["sudo", "pkill"]) == -2
            out = dispatch.reap_run_session(_exited_proc(), label="agent")
        assert out == "probe_error"
        assert any(
            r.levelno == logging.WARNING
            and "sudo refused (rc=1): sudo: a password is required" in r.getMessage()
            for r in caplog.records
        )

    def test_two_user_probe_argv(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        with patch.object(
            dispatch.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["x"], stderr=b""),
        ) as run:
            assert dispatch.reap_run_session(_exited_proc(), label="gate") == "clean"
        assert run.call_args_list[0].args[0] == [
            "sudo",
            "-n",
            "-u",
            "dispatch",
            "pkill",
            "-0",
            "-s",
            "4242",
        ]

    def test_double_reap_is_harmless(self) -> None:
        from agent_gtd_dispatch import dispatch

        with patch.object(
            dispatch.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["x"], stderr=b""),
        ) as run:
            out = dispatch.reap_run_session(_exited_proc(returncode=-15), label="agent")
        assert out == "clean"
        run.assert_called_once()
        assert "-0" in run.call_args.args[0]


@pytest.mark.real_reap
class TestSessionIdReused:
    def test_live_process_at_pid_means_reused(self) -> None:
        from agent_gtd_dispatch import dispatch

        with (
            patch("agent_gtd_dispatch.dispatch.os.kill") as kill,
            patch.object(dispatch.subprocess, "run") as run,
        ):
            assert dispatch._session_id_reused(_exited_proc()) is True
            out = dispatch.reap_run_session(_exited_proc(), label="agent")
        kill.assert_called_with(4242, 0)
        assert out == "skipped_pid_reuse"
        run.assert_not_called()

    def test_permission_error_means_reused(self) -> None:
        from agent_gtd_dispatch import dispatch

        with patch("agent_gtd_dispatch.dispatch.os.kill", side_effect=PermissionError):
            assert dispatch._session_id_reused(_exited_proc()) is True

    def test_no_such_process_means_not_reused(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        with (
            patch(
                "agent_gtd_dispatch.dispatch.os.kill", side_effect=ProcessLookupError
            ),
            patch.object(dispatch, "_run_pkill_quietly", return_value=1) as pk,
        ):
            assert dispatch._session_id_reused(_exited_proc()) is False
            assert dispatch.reap_run_session(_exited_proc(), label="a") == "clean"
        assert pk.call_args.args[0] == ["pkill", "-0", "-s", "4242"]

    def test_unreaped_leader_is_ours(self) -> None:
        from agent_gtd_dispatch import dispatch

        with patch("agent_gtd_dispatch.dispatch.os.kill") as kill:
            assert dispatch._session_id_reused(_exited_proc(returncode=None)) is False
        kill.assert_not_called()


def test_mocked_single_user_gate_runs_no_real_pkill(tmp_path, monkeypatch) -> None:
    from agent_gtd_dispatch import config, dispatch
    from agent_gtd_dispatch.engines import CLAUDE

    monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")

    def _popen(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.poll.return_value = 0
        return proc

    with (
        patch("agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen),
        patch.object(dispatch, "_run_pkill_quietly") as spy,
    ):
        dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
    spy.assert_not_called()


@pytest.mark.real_reap
class TestGateLeakRealProcesses:
    """Background children of a gate are reaped on every exit path."""

    @pytest.mark.parametrize(
        ("script", "rc"),
        [
            ("sleep 300 & echo $! > {pidfile}; exit 0", 0),
            ("sleep 300 & echo $! > {pidfile}; exit 3", 3),
            ("set -m; sleep 300 & echo $! > {pidfile}; exit 0", 0),
            (
                "bash -c 'trap \"\" TERM; exec sleep 300' & echo $! > {pidfile}; exit 0",
                0,
            ),
        ],
    )
    def test_leaked_background_child_is_killed(
        self, tmp_path: Path, monkeypatch, caplog, script: str, rc: int
    ) -> None:
        from agent_gtd_dispatch import config, dispatch
        from agent_gtd_dispatch.engines import CLAUDE

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "CANCEL_GRACE_SECONDS", 1)
        caplog.set_level(logging.INFO, logger=dispatch.logger.name)
        real = dispatch.reap_run_session
        outcomes: list[str] = []

        def _spy(*a, **kw):
            out = real(*a, **kw)
            outcomes.append(out)
            return out

        pidfile = tmp_path / "pid"
        work = tmp_path / "ws"
        work.mkdir()
        try:
            with patch.object(dispatch, "reap_run_session", side_effect=_spy):
                result = dispatch.run_gate_command(
                    work, script.format(pidfile=pidfile), 30, CLAUDE
                )
            pid = int(pidfile.read_text().strip())
            assert _wait_gone(pid), f"leaked child {pid} outlived the gate"
            assert result.returncode == rc
            assert len(outcomes) == 1
            if "trap" in script:
                assert outcomes == ["reaped"]
                (rec,) = _reap_records(caplog)
                assert "escalated_sigkill=1 survivors_after_kill=0" in rec.getMessage()
        finally:
            with contextlib.suppress(Exception):
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)


# ---------------------------------------------------------------------------
# Portable session backend (USE_PROCFS=False): psutil snapshots, never pkill
# ---------------------------------------------------------------------------


def _wait_file(path: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            text = path.read_text().strip()
            if text:
                return text
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def _portable_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.getMessage().startswith("session-reap: portable ")
    ]


@pytest.mark.real_reap
class TestPortableSessionBackend:
    @pytest.fixture(autouse=True)
    def _portable(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, dispatch, platform_support

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)

        def _no_pkill(_argv):
            raise AssertionError("pkill must not run on the portable backend")

        monkeypatch.setattr(dispatch, "_run_pkill_quietly", _no_pkill)

    def test_reaps_every_descendant_and_spares_sibling(
        self, tmp_path: Path, caplog
    ) -> None:
        import psutil

        from agent_gtd_dispatch import dispatch

        sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 60 & sleep 60 & wait"], start_new_session=True
        )
        try:
            deadline = time.monotonic() + 5
            while len(psutil.Process(proc.pid).children()) < 2:
                assert time.monotonic() < deadline
                time.sleep(0.02)
            kids = [c.pid for c in psutil.Process(proc.pid).children()]
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                out = dispatch.reap_run_session(proc, label="agent", grace_seconds=2)  # type: ignore[arg-type]
            assert out == "reaped"
            proc.wait(timeout=5)
            for pid in kids:
                assert _wait_gone(pid)
            assert sibling.poll() is None
            (line,) = _portable_lines(caplog)
            assert line.getMessage() == (
                f"session-reap: portable label=agent sid={proc.pid} leader_reaped=0 "
                "descendants=2 dropped_foreign_uid=0 marker=none"
            )
            assert line.levelno == logging.INFO
            (reap,) = _reap_records(caplog)
            assert reap.getMessage().startswith(
                "session-reap: outcome=reaped label=agent"
            )
            assert " sid_unknown=0 " in reap.getMessage()
            assert proc.pid not in dispatch.portable_procs._SESSIONS
        finally:
            sibling.kill()
            sibling.wait()
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait()

    def test_exited_leader_without_marker_is_clean_and_warned(self, caplog) -> None:
        from agent_gtd_dispatch import dispatch

        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            out = dispatch.reap_run_session(proc, label="gate")  # type: ignore[arg-type]
        assert out == "clean"
        (line,) = _portable_lines(caplog)
        assert "leader_reaped=1 descendants=0" in line.getMessage()
        assert line.getMessage().endswith("marker=none")
        assert line.levelno == logging.WARNING

    def test_sigterm_ignoring_child_escalates_to_sigkill(
        self, tmp_path: Path, caplog
    ) -> None:
        from agent_gtd_dispatch import dispatch

        pidfile = tmp_path / "child.pid"
        proc = subprocess.Popen(
            [
                "sh",
                "-c",
                f"(trap '' TERM; exec sh -c 'echo $$ > {pidfile}; exec sleep 60') & wait",
            ],
            start_new_session=True,
        )
        try:
            child = int(_wait_file(pidfile))
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                out = dispatch.reap_run_session(proc, label="agent", grace_seconds=1)  # type: ignore[arg-type]
            assert out == "reaped"
            assert _wait_gone(child)
            (reap,) = _reap_records(caplog)
            assert "escalated_sigkill=1 survivors_after_kill=0" in reap.getMessage()
        finally:
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait()

    def test_realistic_seam_marker_net(self, tmp_path: Path, caplog) -> None:
        import secrets

        from agent_gtd_dispatch import dispatch

        marker = f"rt-{secrets.token_hex(6)}"
        pidfile = tmp_path / "bg.pid"
        proc = subprocess.Popen(
            ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"],
            env={**os.environ, "AGENT_GTD_RUN_KEY": marker},
            start_new_session=True,
        )
        proc.wait()
        bg = int(_wait_file(pidfile))
        try:
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                out = dispatch.reap_run_session(proc, label="agent", marker=marker)  # type: ignore[arg-type]
            assert out == "reaped"
            (reap,) = _reap_records(caplog)
            assert "marker_reaped=1 marker_scan=ok" in reap.getMessage()
            (line,) = _portable_lines(caplog)
            assert "leader_reaped=1" in line.getMessage()
            assert line.getMessage().endswith("marker=present")
            assert line.levelno == logging.INFO
            assert _wait_gone(bg)
        finally:
            with contextlib.suppress(OSError):
                os.kill(bg, signal.SIGKILL)

    async def test_cancel_path_never_calls_pkill(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config, main

        monkeypatch.setattr(config, "CANCEL_GRACE_SECONDS", 0)
        proc = subprocess.Popen(
            ["sh", "-c", "trap '' TERM; sleep 60 & wait"], start_new_session=True
        )
        try:
            escalated = await main._terminate_run_session(proc)
            assert escalated is True
            proc.wait(timeout=5)
        finally:
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait()

    def test_access_denied_probe_is_probe_error_without_signal(
        self, monkeypatch
    ) -> None:
        import psutil

        from agent_gtd_dispatch import dispatch, portable_procs

        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:

            def _deny(pid):
                raise psutil.AccessDenied(pid)

            monkeypatch.setattr(portable_procs._psutil(), "Process", _deny)
            out = dispatch.reap_run_session(proc, label="agent", grace_seconds=0.2)  # type: ignore[arg-type]
            assert out == "probe_error"
            assert proc.poll() is None
            assert dispatch.run_session_has_members(proc) is True  # type: ignore[arg-type]
        finally:
            proc.kill()
            proc.wait()

    def test_kill_run_session_probe_failure_still_signals_leader(
        self, monkeypatch, caplog
    ) -> None:
        from agent_gtd_dispatch import dispatch, portable_procs

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(portable_procs, "session_snapshot", _boom)
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            dispatch.kill_run_session(proc, signal.SIGKILL)  # type: ignore[arg-type]
            assert proc.wait(timeout=5) == -signal.SIGKILL
            assert any(
                "session-signal: portable probe failed" in r.getMessage()
                for r in caplog.records
            )
        finally:
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait()
