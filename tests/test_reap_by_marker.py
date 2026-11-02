"""Reaping a run's processes by its inherited run marker (AGENT_GTD_RUN_KEY)."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import signal
import subprocess
import sys
import textwrap
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agent_gtd_dispatch import config, dispatch, run_tmp

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

READY_SLEEP = "print('ready', flush=True); import time; time.sleep(60)"

# Parent forks a child that leaves the session AND the workspace, then exits.
ESCAPE = textwrap.dedent(
    """
    import os, sys, time
    pidfile, elsewhere = sys.argv[1], sys.argv[2]
    if os.fork() == 0:
        os.setsid()
        os.chdir(elsewhere)
        with open(pidfile + '.tmp', 'w') as f:
            f.write(str(os.getpid()))
        os.rename(pidfile + '.tmp', pidfile)
        time.sleep(60)
        os._exit(0)
    os._exit(0)
    """
)


def _unmarked_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != run_tmp.RUN_MARKER_ENV}


@pytest.fixture
def roots(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    ws = tmp_path.resolve() / "workspaces"
    rt = tmp_path.resolve() / "run-tmp"
    ws.mkdir()
    rt.mkdir()
    with (
        patch.object(config, "WORKSPACE_ROOT", ws),
        patch.object(config, "RUN_TMP_ROOT", rt),
        patch.object(config, "AGENT_SUBPROCESS_USER", ""),
    ):
        yield ws, rt


def _spawn(
    argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    cwd.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env if env is not None else _unmarked_env(),
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout is not None
    assert p.stdout.readline().strip() == "ready"
    return p


def _spawn_marked(tmp: Path, cwd: Path) -> subprocess.Popen[str]:
    return _spawn(
        run_tmp.with_tmp_env([sys.executable, "-c", READY_SLEEP], tmp),
        cwd,
        env=_unmarked_env(),
    )


def _alive(pid: int) -> bool:
    return dispatch._proc_is_live(pid)


def _gone_within(pid: int, secs: float = 5.0) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _kill(p: subprocess.Popen[str] | int) -> None:
    pid = p if isinstance(p, int) else p.pid
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    if not isinstance(p, int):
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=5)


def _msgs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Real processes, single-user (AC-9)
# ---------------------------------------------------------------------------


@pytest.mark.real_reap
class TestRealProcesses:
    def test_escaped_child_reaped_by_marker(
        self, roots: tuple[Path, Path], tmp_path: Path, caplog
    ) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        workspace_a = ws / "repos-a"
        workspace_a.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        pidfile = tmp_path / "child.pid"
        child = 0
        proc = None
        try:
            proc = subprocess.Popen(
                run_tmp.with_tmp_env(
                    [sys.executable, "-c", ESCAPE, str(pidfile), str(elsewhere)],
                    tmp_a,
                ),
                cwd=workspace_a,
                env=_unmarked_env(),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pidfile.read_text())
            proc.wait(timeout=5)
            assert _alive(child)
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                out = dispatch.reap_run_session(
                    proc,  # type: ignore[arg-type]
                    label="agent",
                    marker=tmp_a.name,
                )
            assert out == "reaped"
            assert _gone_within(child)
            msgs = _msgs(caplog)
            session = [m for m in msgs if m.startswith("session-reap: outcome=")]
            assert len(session) == 1
            assert "outcome=reaped" in session[0]
            assert (
                "survivors_after_kill=0 marker_reaped=1 marker_scan=ok "
                "escaped_session=1" in session[0]
            )
            assert any(f"reap-marker: kill pid={child} " in m for m in msgs)
        finally:
            if child:
                _kill(child)
            if proc is not None and proc.poll() is None:
                _kill(proc)  # type: ignore[arg-type]
            run_tmp.remove_run_tmp(tmp_a, kind="build")

    def test_sibling_run_untouched_and_own_run_reaped(
        self, roots: tuple[Path, Path], caplog
    ) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        tmp_b = run_tmp.create_run_tmp(secrets.token_hex(6))
        workspace_a = ws / "repos-a"
        procs: list[subprocess.Popen[str]] = []
        try:
            sibling = _spawn_marked(tmp_b, workspace_a)
            procs.append(sibling)
            short = subprocess.Popen(
                run_tmp.with_tmp_env([sys.executable, "-c", "pass"], tmp_a),
                cwd=workspace_a,
                env=_unmarked_env(),
                start_new_session=True,
            )
            short.wait(timeout=10)
            dispatch.reap_run_session(short, label="agent", marker=tmp_a.name)
            assert sibling.poll() is None
            own = _spawn_marked(tmp_a, workspace_a)
            procs.append(own)
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                reaped = dispatch.reap_workspace_processes(
                    workspace_a, term_grace_secs=2, own_marker=tmp_a.name
                )
            assert {r.pid for r in reaped} == {own.pid}
            own.wait(timeout=5)
            assert sibling.poll() is None
            msgs = _msgs(caplog)
            assert any(
                f"skipped pid={sibling.pid} " in m and "reason=other_live_run" in m
                for m in msgs
            )
            assert any(
                f"reaping pid={own.pid} " in m and "reason=own_run" in m for m in msgs
            )
            assert any(
                "reap-backstop: scanned" in m
                and "matched=2 skipped_other_run=1 scan=ok" in m
                for m in msgs
            )
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")
            run_tmp.remove_run_tmp(tmp_b, kind="build")

    def test_dead_run_marker_reaped(self, roots: tuple[Path, Path], caplog) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        tmp_c = run_tmp.create_run_tmp(secrets.token_hex(6))
        workspace_a = ws / "repos-a"
        procs: list[subprocess.Popen[str]] = []
        try:
            stale = _spawn_marked(tmp_c, workspace_a)
            procs.append(stale)
            run_tmp.remove_run_tmp(tmp_c, kind="build")
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                reaped = dispatch.reap_workspace_processes(
                    workspace_a, term_grace_secs=2, own_marker=tmp_a.name
                )
            assert [r.pid for r in reaped] == [stale.pid]
            stale.wait(timeout=5)
            assert any(
                f"reaping pid={stale.pid} " in m
                and f"marker={tmp_c.name}" in m
                and "reason=dead_run" in m
                for m in _msgs(caplog)
            )
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")
            run_tmp.remove_run_tmp(tmp_c, kind="build")

    def test_marker_match_is_exact(self, roots: tuple[Path, Path]) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        procs: list[subprocess.Popen[str]] = []
        try:
            env = {**_unmarked_env(), run_tmp.RUN_MARKER_ENV: tmp_a.name + "x"}
            near = _spawn([sys.executable, "-c", READY_SLEEP], ws / "repos-a", env)
            procs.append(near)
            result = dispatch.reap_marker_processes(tmp_a.name, term_grace_secs=1)
            assert result.reaped == []
            assert result.survivors == 0
            assert result.scan_ok
            assert near.poll() is None
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")

    def test_marker_scan_script_directly(self, roots: tuple[Path, Path]) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        procs: list[subprocess.Popen[str]] = []
        try:
            marked = _spawn_marked(tmp_a, ws / "repos-a")
            procs.append(marked)
            unmarked = _spawn([sys.executable, "-c", READY_SLEEP], ws / "repos-a")
            procs.append(unmarked)
            out = subprocess.run(
                [sys.executable, "-c", dispatch._MARKER_SCAN_SCRIPT, tmp_a.name],
                capture_output=True,
                text=True,
                check=True,
                env=_unmarked_env(),
            )
            assert out.stderr == ""
            pids = {int(line.split("\t", 1)[0]) for line in out.stdout.splitlines()}
            assert marked.pid in pids
            assert unmarked.pid not in pids
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")

    def test_unmarked_process_reaped_by_cwd(
        self, roots: tuple[Path, Path], caplog
    ) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        procs: list[subprocess.Popen[str]] = []
        try:
            unmarked = _spawn([sys.executable, "-c", READY_SLEEP], ws / "repos-a")
            procs.append(unmarked)
            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
                reaped = dispatch.reap_workspace_processes(
                    ws / "repos-a", term_grace_secs=2, own_marker=tmp_a.name
                )
            assert [r.pid for r in reaped] == [unmarked.pid]
            unmarked.wait(timeout=5)
            assert any(
                f"reaping pid={unmarked.pid} " in m
                and "marker=none" in m
                and "reason=no_marker" in m
                for m in _msgs(caplog)
            )
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")

    def test_cwd_scan_script_reports_marker(self, roots: tuple[Path, Path]) -> None:
        ws, _rt = roots
        tmp_a = run_tmp.create_run_tmp(secrets.token_hex(6))
        procs: list[subprocess.Popen[str]] = []
        try:
            marked = _spawn_marked(tmp_a, ws / "repos-a")
            procs.append(marked)
            out = subprocess.run(
                [sys.executable, "-c", dispatch._SCAN_SCRIPT, str(ws / "repos-a")],
                capture_output=True,
                text=True,
                check=True,
            )
            assert out.stderr == ""
            rows = {
                int(p): mk
                for p, mk, _c in (
                    line.split("\t", 2) for line in out.stdout.splitlines()
                )
            }
            assert rows[marked.pid] == tmp_a.name
        finally:
            for p in procs:
                _kill(p)
            run_tmp.remove_run_tmp(tmp_a, kind="build")


# ---------------------------------------------------------------------------
# Mocked: two-user wiring, log levels, invariant, scan errors (AC-10)
# ---------------------------------------------------------------------------


def _completed(rc: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)


class TestScanMarkerPidsTwoUser:
    def test_launch_shape(self) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(dispatch.subprocess, "run", return_value=_completed(0)) as run,
        ):
            res = dispatch.scan_marker_pids("rt-abc")
            expected = dispatch._sudo_wrap(
                ["python3", "-c", dispatch._MARKER_SCAN_SCRIPT, "rt-abc"]
            )
        assert run.call_args.args[0] == expected
        assert run.call_args.kwargs["capture_output"] is True
        assert run.call_args.kwargs["timeout"] == 15
        assert res == dispatch.MarkerScan(True, {}, "")

    def test_sudo_refusal_is_scan_error(self) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=_completed(1, b"", b"sudo: a password is required"),
            ),
        ):
            res = dispatch.scan_marker_pids("rt-abc")
        assert res.ok is False
        assert res.pids == {}
        assert res.error == "rc=1 stderr=sudo: a password is required"

    def test_launch_exception_is_scan_error(self) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["x"], 15),
            ),
        ):
            res = dispatch.scan_marker_pids("rt-abc")
        assert res.ok is False
        assert res.pids == {}
        assert res.error.startswith("TimeoutExpired: ")

    def test_stderr_with_matches_keeps_pids(self) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=_completed(0, b"123\tsleep\nbogus\n", b"warn"),
            ),
            patch.object(dispatch, "_proc_is_live", return_value=True),
        ):
            res = dispatch.scan_marker_pids("rt-abc")
        assert res.ok is False
        assert res.pids == {123: "sleep"}

    def test_in_process_exception_is_scan_error(self) -> None:
        with patch.object(
            dispatch, "_scan_marker_in_process", side_effect=RuntimeError("boom")
        ):
            res = dispatch.scan_marker_pids("rt-abc")
        assert res == dispatch.MarkerScan(False, {}, "RuntimeError: boom")


@pytest.mark.real_reap
class TestReapMarkerProcessesMocked:
    def test_sudo_refusal_logs_warning_scan_error(self, caplog) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=_completed(1, b"", b"sudo: a password is required"),
            ),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            res = dispatch.reap_marker_processes("rt-abc")
        assert res == dispatch.MarkerReap([], -1, False, 0)
        summary = [r for r in caplog.records if "reap-marker: marker=" in r.message]
        assert len(summary) == 1
        assert summary[0].levelno == logging.WARNING
        assert "scan=error rc=1" in summary[0].message

    def test_stderr_with_match_still_signals(self) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=_completed(0, b"123\tsleep\n", b"warn"),
            ),
            patch.object(dispatch, "_proc_is_live", return_value=True),
            patch.object(dispatch, "_signal_pid") as sig,
            patch.object(dispatch, "_REAP_POST_KILL_WAIT_SECONDS", 0.0),
        ):
            res = dispatch.reap_marker_processes("rt-abc", term_grace_secs=0)
        assert sig.call_args_list[0].args == (123, signal.SIGTERM)
        assert (123, signal.SIGKILL) in [c.args for c in sig.call_args_list]
        assert [r.pid for r in res.reaped] == [123]
        assert res.reaped[0].killed_with == "SIGKILL"
        assert res.survivors == -1
        assert res.scan_ok is False

    def _levels(self, scans, caplog, **kw) -> logging.LogRecord:
        with (
            patch.object(dispatch, "scan_marker_pids", side_effect=scans),
            patch.object(dispatch, "_signal_pid"),
            patch.object(dispatch, "_proc_is_live", return_value=False),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            dispatch.reap_marker_processes("rt-abc", term_grace_secs=0, **kw)
        summary = [r for r in caplog.records if "reap-marker: marker=" in r.message]
        assert len(summary) == 1
        return summary[0]

    def test_summary_info_when_nothing_matched(self, caplog) -> None:
        ok = dispatch.MarkerScan(True, {}, "")
        rec = self._levels([ok, ok], caplog)
        assert rec.levelno == logging.INFO
        assert rec.message.startswith(
            "reap-marker: marker=rt-abc matched=0 sigkilled=0 survivors=0 scan=ok"
        )

    def test_summary_warning_when_matched_and_none_survived(self, caplog) -> None:
        first = dispatch.MarkerScan(True, {999999: "x"}, "")
        second = dispatch.MarkerScan(True, {}, "")
        rec = self._levels([first, second], caplog)
        assert rec.levelno == logging.WARNING
        assert "matched=1 sigkilled=0 survivors=0 scan=ok" in rec.message

    def test_summary_error_when_survivors(self, caplog) -> None:
        first = dispatch.MarkerScan(True, {999999: "x"}, "")
        rec = self._levels([first, first], caplog)
        assert rec.levelno == logging.ERROR
        assert "survivors=1" in rec.message

    def test_escaped_session_counts_foreign_sids(self) -> None:
        first = dispatch.MarkerScan(True, {11: "a", 12: "b"}, "")
        second = dispatch.MarkerScan(True, {}, "")
        with (
            patch.object(dispatch, "scan_marker_pids", side_effect=[first, second]),
            patch.object(dispatch, "_signal_pid"),
            patch.object(dispatch, "_proc_is_live", return_value=False),
            patch.object(dispatch, "_proc_sid", side_effect=[500, 77]),
        ):
            res = dispatch.reap_marker_processes(
                "rt-abc", term_grace_secs=0, session_id=500
            )
        assert res.escaped_session == 1

    def test_log_lines_capped(self, caplog) -> None:
        first = dispatch.MarkerScan(True, {1000 + i: "x" for i in range(25)}, "")
        second = dispatch.MarkerScan(True, {}, "")
        with (
            patch.object(dispatch, "scan_marker_pids", side_effect=[first, second]),
            patch.object(dispatch, "_signal_pid"),
            patch.object(dispatch, "_proc_is_live", return_value=False),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            dispatch.reap_marker_processes("rt-abc", term_grace_secs=0, context="c=1")
        msgs = _msgs(caplog)
        assert sum(m.startswith("reap-marker: kill pid=") for m in msgs) == 20
        assert "reap-marker: ... 5 more marker=rt-abc c=1" in msgs

    def test_worker_child_is_invariant_violation(self, caplog) -> None:
        first = dispatch.MarkerScan(True, {999999: "sleep 60"}, "")
        second = dispatch.MarkerScan(True, {}, "")
        with (
            patch.object(dispatch, "scan_marker_pids", side_effect=[first, second]),
            patch.object(dispatch, "_signal_pid") as sig,
            patch.object(dispatch, "_proc_is_live", return_value=False),
            patch.object(dispatch, "_proc_ppid", return_value=os.getpid()),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            res = dispatch.reap_marker_processes("rt-abc", term_grace_secs=0)
        errs = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "reap-marker: invariant-violation" in r.message
        ]
        assert len(errs) == 1
        assert "reason=worker_child" in errs[0].message
        assert "pid=999999" in errs[0].message
        sig.assert_any_call(999999, signal.SIGTERM)
        assert [r.pid for r in res.reaped] == [999999]

    def test_never_raises(self) -> None:
        with patch.object(dispatch, "scan_marker_pids", side_effect=RuntimeError):
            res = dispatch.reap_marker_processes("rt-abc")
        assert res == dispatch.MarkerReap([], -1, False, 0)

    def test_reap_run_session_marker_scan_error(self, caplog) -> None:
        proc = MagicMock(pid=4242, returncode=0)
        bad = dispatch.MarkerScan(False, {}, "rc=1 stderr=x")
        with (
            patch.object(dispatch, "_session_id_reused", return_value=False),
            patch.object(dispatch, "_run_pkill_quietly", return_value=1),
            patch.object(dispatch, "scan_marker_pids", return_value=bad),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            out = dispatch.reap_run_session(proc, label="agent", marker="rt-abc")
        assert out == "clean"
        lines = [r for r in caplog.records if "session-reap: outcome=" in r.message]
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "survivors_after_kill=-1 marker_reaped=0 marker_scan=error" in (
            lines[0].message
        )

    def test_reap_run_session_marker_runs_on_pid_reuse(self, caplog) -> None:
        proc = MagicMock(pid=4242, returncode=0)
        reap = MagicMock(
            return_value=dispatch.MarkerReap(
                [dispatch.ReapedProcess(9, "x", "SIGTERM")], 0, True, 1
            )
        )
        with (
            patch.object(dispatch, "_session_id_reused", return_value=True),
            patch.object(dispatch, "reap_marker_processes", reap),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            out = dispatch.reap_run_session(
                proc, label="gate", marker="rt-abc", context="ctx=1"
            )
        assert out == "skipped_pid_reuse"
        reap.assert_called_once_with("rt-abc", session_id=4242, context="ctx=1")
        line = next(r.message for r in caplog.records if "session-reap:" in r.message)
        assert line.endswith("marker_reaped=1 marker_scan=ok escaped_session=1 ctx=1")


class TestReapWorkspaceScanErrors:
    def test_two_user_scan_refusal_is_visible(
        self, roots: tuple[Path, Path], caplog
    ) -> None:
        ws, _rt = roots
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(dispatch, "_agent_uid", return_value=12345),
            patch.object(dispatch, "_scan_workspace_pids", return_value={}),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="sudo: a password is required"
                ),
            ),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"),
        ):
            assert dispatch.reap_workspace_processes(ws / "repos-a") == []
        msgs = _msgs(caplog)
        assert any(
            m.startswith("reap-backstop: scan-error") and "rc=1" in m for m in msgs
        )
        scanned = [r for r in caplog.records if "reap-backstop: scanned" in r.message]
        assert len(scanned) == 1
        assert scanned[0].levelno == logging.WARNING
        assert "matched=0 skipped_other_run=0 scan=error" in scanned[0].message

    def test_launch_exception_is_scan_error(self, roots: tuple[Path, Path]) -> None:
        ws, _rt = roots
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(dispatch.subprocess, "run", side_effect=OSError("nope")),
        ):
            assert dispatch._scan_as_agent_user(ws) == (False, {})

    def test_clean_empty_scan_logs_info(self, roots: tuple[Path, Path], caplog) -> None:
        ws, _rt = roots
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            assert dispatch.reap_workspace_processes(ws / "repos-a") == []
        scanned = [r for r in caplog.records if "reap-backstop: scanned" in r.message]
        assert len(scanned) == 1
        assert scanned[0].levelno == logging.INFO
        assert "matched=0 skipped_other_run=0 scan=ok" in scanned[0].message

    def test_three_field_parse_keeps_tabbed_cmdline(
        self, roots: tuple[Path, Path]
    ) -> None:
        ws, _rt = roots
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(
                dispatch.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="55\trt-a\tpython -c a\tb\n66\t\tsleep\n", stderr=""
                ),
            ),
        ):
            ok, found = dispatch._scan_as_agent_user(ws)
        assert ok is True
        assert found == {55: ("rt-a", "python -c a\tb"), 66: ("", "sleep")}

    def test_proc_run_marker_unreadable(self) -> None:
        assert dispatch._proc_run_marker(2**31 - 1) == ""
