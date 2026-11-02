"""portable_procs: the psutil process API used off procfs (macOS), on real processes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import psutil
import pytest

from agent_gtd_dispatch import dispatch, portable_procs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

READY_SLEEP = "print('ready', flush=True); import time; time.sleep(60)"


def _env(marker: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AGENT_GTD_RUN_KEY"}
    if marker is not None:
        env["AGENT_GTD_RUN_KEY"] = marker
    return env


@pytest.fixture
def spawn() -> Iterator[object]:
    procs: list[subprocess.Popen[str]] = []

    def _spawn(
        cwd: Path | None = None,
        marker: str | None = None,
        argv: list[str] | None = None,
    ) -> subprocess.Popen[str]:
        p = subprocess.Popen(
            argv or [sys.executable, "-c", READY_SLEEP],
            cwd=cwd,
            env=_env(marker),
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(p)
        if argv is None:
            assert p.stdout is not None
            assert p.stdout.readline().strip() == "ready"
        return p

    yield _spawn
    for p in procs:
        with contextlib.suppress(OSError):
            p.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=5)


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


_LOOKUP_ERRORS = [
    psutil.NoSuchProcess(1),
    psutil.AccessDenied(1),
    psutil.ZombieProcess(1),
    OSError("boom"),
]


class TestPointLookups:
    def test_cmdline_cap_matches_dispatch(self) -> None:
        assert portable_procs.CMDLINE_MAX_CHARS == dispatch._REAP_CMDLINE_MAX_CHARS

    def test_cmdline_real(self, spawn) -> None:
        p = spawn()
        assert portable_procs.cmdline(p.pid).startswith(sys.executable)
        assert "\n" not in portable_procs.cmdline(p.pid)

    def test_cmdline_newlines_and_cap(self, spawn) -> None:
        p = spawn(argv=["sleep", "60"])
        with patch_method("cmdline", lambda self: ["a\nb", "x" * 500]):
            text = portable_procs.cmdline(p.pid)
        assert text.startswith("a b x")
        assert len(text) == 200

    def test_is_live_ppid_uid_marker(self, spawn) -> None:
        p = spawn(marker="rt-abc")
        assert portable_procs.is_live(p.pid) is True
        assert portable_procs.ppid(p.pid) == os.getpid()
        assert portable_procs.real_uid(p.pid) == os.getuid()
        assert portable_procs.run_marker(p.pid) == "rt-abc"
        q = spawn()
        assert portable_procs.run_marker(q.pid) == ""

    def test_zombie_is_not_live(self) -> None:
        p = subprocess.Popen(["true"])
        deadline = time.monotonic() + 5
        while psutil.Process(p.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        try:
            assert portable_procs.is_live(p.pid) is False
        finally:
            p.wait()

    def test_dead_pid_fallbacks(self) -> None:
        pid = _dead_pid()
        assert portable_procs.cmdline(pid) == ""
        assert portable_procs.is_live(pid) is False
        assert portable_procs.ppid(pid) == -1
        assert portable_procs.real_uid(pid) is None
        assert portable_procs.run_marker(pid) == ""

    @pytest.mark.parametrize("exc", _LOOKUP_ERRORS, ids=lambda e: type(e).__name__)
    def test_each_error_gives_fallback(self, exc: BaseException) -> None:
        def _raise(self, *a, **k):
            raise exc

        pid = os.getpid()
        with patch_method("cmdline", _raise):
            assert portable_procs.cmdline(pid) == ""
        with patch_method("status", _raise):
            assert portable_procs.is_live(pid) is False
        with patch_method("ppid", _raise):
            assert portable_procs.ppid(pid) == -1
        with patch_method("uids", _raise):
            assert portable_procs.real_uid(pid) is None
        with patch_method("environ", _raise):
            assert portable_procs.run_marker(pid) == ""

    def test_missing_psutil_gives_fallback(self, monkeypatch) -> None:
        def _no(*_a):
            raise ImportError("no psutil")

        monkeypatch.setattr(portable_procs, "_psutil", _no)
        pid = os.getpid()
        assert portable_procs.cmdline(pid) == ""
        assert portable_procs.is_live(pid) is False
        assert portable_procs.ppid(pid) == -1
        assert portable_procs.real_uid(pid) is None
        assert portable_procs.run_marker(pid) == ""
        assert portable_procs.member_live(psutil.Process()) is False

    def test_ancestor_pids(self) -> None:
        pids = portable_procs.ancestor_pids()
        assert os.getpid() in pids and os.getppid() in pids
        assert 1 not in pids
        if dispatch.platform_support.USE_PROCFS:
            # The /proc implementation agrees with the portable one.
            assert pids == dispatch._ancestor_pids()

    def test_ancestor_pids_stops_on_repeat(self, monkeypatch) -> None:
        monkeypatch.setattr(portable_procs, "ppid", lambda pid: os.getppid())
        assert portable_procs.ancestor_pids() == {os.getpid(), os.getppid()}


@contextlib.contextmanager
def patch_method(name: str, fn) -> Iterator[None]:
    saved = getattr(psutil.Process, name)
    setattr(psutil.Process, name, fn)
    try:
        yield
    finally:
        setattr(psutil.Process, name, saved)


class TestScanWorkspace:
    def test_path_component_match(self, spawn, tmp_path: Path) -> None:
        ws = tmp_path / "repos-a"
        sib = tmp_path / "repos-ab"
        (ws / "sub").mkdir(parents=True)
        sib.mkdir()
        inside = spawn(cwd=ws / "sub")
        sibling = spawn(cwd=sib)
        res = portable_procs.scan_workspace_pids(ws)
        assert inside.pid in res.pids
        assert sibling.pid not in res.pids
        assert res.pids[inside.pid].startswith(sys.executable)
        assert res.examined >= 2
        assert res.unreadable == 0

    def test_symlinked_workspace(self, spawn, tmp_path: Path) -> None:
        real = tmp_path / "real-ws"
        real.mkdir()
        link = tmp_path / "link-ws"
        link.symlink_to(real)
        p = spawn(cwd=real)
        assert p.pid in portable_procs.scan_workspace_pids(link).pids

    def test_unreadable_counted(self, spawn, tmp_path: Path) -> None:
        spawn(cwd=tmp_path)

        def _deny(self):
            raise psutil.AccessDenied(self.pid)

        with patch_method("exe", _deny), patch_method("cwd", _deny):
            res = portable_procs.scan_workspace_pids(tmp_path)
        assert res.pids == {}
        assert res.unreadable == res.examined > 0

    def test_vanished_and_unreadable_uids_skipped(self, spawn, tmp_path: Path) -> None:
        p = spawn(cwd=tmp_path)
        real_cwd = psutil.Process.cwd
        real_uids = psutil.Process.uids

        def _gone(self):
            if self.pid == p.pid:
                raise psutil.NoSuchProcess(self.pid)
            return real_cwd(self)

        with patch_method("exe", lambda self: ""), patch_method("cwd", _gone):
            assert p.pid not in portable_procs.scan_workspace_pids(tmp_path).pids

        def _uids(self):
            if self.pid == p.pid:
                raise psutil.AccessDenied(self.pid)
            return real_uids(self)

        with patch_method("uids", _uids):
            assert p.pid not in portable_procs.scan_workspace_pids(tmp_path).pids

    def test_whole_scan_failure_propagates(self, monkeypatch, tmp_path) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(psutil, "process_iter", _boom)
        with pytest.raises(RuntimeError, match="boom"):
            portable_procs.scan_workspace_pids(tmp_path)
        with pytest.raises(RuntimeError, match="boom"):
            portable_procs.scan_marker("rt-x", os.getuid())


class TestScanMarker:
    def test_exact_match_only(self, spawn) -> None:
        exact = spawn(marker="rt-abc")
        longer = spawn(marker="rt-abcd")
        shorter = spawn(marker="rt-ab")
        res = portable_procs.scan_marker("rt-abc", os.getuid())
        assert exact.pid in res.pids
        assert longer.pid not in res.pids
        assert shorter.pid not in res.pids
        assert res.examined >= 3

    def test_other_uid_not_matched(self, spawn) -> None:
        p = spawn(marker="rt-uid")
        res = portable_procs.scan_marker("rt-uid", os.getuid() + 1)
        assert p.pid not in res.pids

    def test_unreadable_environ_not_matched(self, spawn) -> None:
        p = spawn(marker="rt-hidden")
        real = psutil.Process.environ

        def _env(self):
            if self.pid == p.pid:
                raise psutil.AccessDenied(self.pid)
            return real(self)

        # Other same-uid processes on a busy host may be unreadable already
        # (non-dumpable), so assert the child's contribution, not a total.
        assert p.pid in portable_procs.scan_marker("rt-hidden", os.getuid()).pids
        with patch_method("environ", _env):
            res = portable_procs.scan_marker("rt-hidden", os.getuid())
        assert p.pid not in res.pids
        assert res.unreadable >= 1

    def test_vanished_process_not_counted(self, spawn) -> None:
        p = spawn(marker="rt-gone")
        real = psutil.Process.environ

        def _env(self):
            if self.pid == p.pid:
                raise psutil.NoSuchProcess(self.pid)
            return real(self)

        with patch_method("environ", _env):
            res = portable_procs.scan_marker("rt-gone", os.getuid())
        assert p.pid not in res.pids


class TestReadHelper:
    @pytest.mark.parametrize(
        ("exc", "state"),
        [
            (psutil.ZombieProcess(1), "unreadable"),
            (psutil.NoSuchProcess(1), "gone"),
            (psutil.AccessDenied(1), "unreadable"),
            (OSError("x"), "unreadable"),
            (psutil.TimeoutExpired(1), "unreadable"),
        ],
    )
    def test_states(self, exc: BaseException, state: str) -> None:
        def _fn():
            raise exc

        assert portable_procs._read(psutil, _fn) == (state, None)

    def test_ok(self) -> None:
        assert portable_procs._read(psutil, lambda: 3) == ("ok", 3)


class TestSessionSnapshots:
    def _tree(self) -> subprocess.Popen[bytes]:
        p = subprocess.Popen(
            ["sh", "-c", "sleep 60 & sleep 60 & wait"], start_new_session=True
        )
        deadline = time.monotonic() + 5
        while len(psutil.Process(p.pid).children()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return p

    def test_capture_signal_drop(self) -> None:
        p = self._tree()
        try:
            snap = portable_procs.session_snapshot(
                p.pid, owner=id(p), leader_reaped=False
            )
            assert p.pid in snap.members
            assert snap.descendants == 2
            assert len(portable_procs.live_members(snap)) == 3
            again = portable_procs.session_snapshot(
                p.pid, owner=id(p), leader_reaped=False
            )
            assert again is snap
            assert portable_procs.signal_members(snap, signal.SIGKILL) == 3
            p.wait(timeout=5)
            deadline = time.monotonic() + 5
            while portable_procs.live_members(snap):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            # A refresh skips dead members and finds nothing new.
            portable_procs.session_snapshot(p.pid, owner=id(p), leader_reaped=False)
            assert portable_procs.live_members(snap) == []
        finally:
            portable_procs.drop_session(p.pid)
            with contextlib.suppress(OSError):
                p.kill()
            p.wait()
        assert p.pid not in portable_procs._SESSIONS
        portable_procs.drop_session(p.pid)

    def test_refresh_finds_new_descendants(self, tmp_path: Path) -> None:
        go = tmp_path / "go"
        p = subprocess.Popen(
            [
                "sh",
                "-c",
                f"while [ ! -e {go} ]; do sleep 0.05; done; sleep 60 & wait",
            ],
            start_new_session=True,
        )
        try:
            snap = portable_procs.session_snapshot(
                p.pid, owner=id(p), leader_reaped=False
            )
            before = set(snap.members)
            go.write_text("")
            deadline = time.monotonic() + 5
            while True:
                snap = portable_procs.session_snapshot(
                    p.pid, owner=id(p), leader_reaped=False
                )
                names = {m.name() for m in portable_procs.live_members(snap)}
                if "sleep" in names and set(snap.members) - before:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.05)
            portable_procs.signal_members(snap, signal.SIGKILL)
        finally:
            portable_procs.drop_session(p.pid)
            with contextlib.suppress(OSError):
                p.kill()
            p.wait()

    def test_zombie_child_not_a_member(self) -> None:
        # `true` exits but the exec'd sleep never reaps it: a zombie child.
        p = subprocess.Popen(["sh", "-c", "true & exec sleep 60"])
        try:
            deadline = time.monotonic() + 5
            while not any(
                c.status() == psutil.STATUS_ZOMBIE
                for c in psutil.Process(p.pid).children()
            ):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            snap = portable_procs.session_snapshot(
                p.pid, owner=id(p), leader_reaped=False
            )
            assert list(snap.members) == [p.pid]
            assert snap.descendants == 0
        finally:
            portable_procs.drop_session(p.pid)
            p.kill()
            p.wait()

    def test_owner_mismatch_recaptures(self) -> None:
        p = self._tree()
        try:
            a = portable_procs.session_snapshot(p.pid, owner=1, leader_reaped=False)
            b = portable_procs.session_snapshot(p.pid, owner=2, leader_reaped=False)
            assert a is not b
            portable_procs.signal_members(b, signal.SIGKILL)
        finally:
            portable_procs.drop_session(p.pid)
            p.wait()

    def test_leader_reaped_is_empty(self) -> None:
        pid = _dead_pid()
        snap = portable_procs.session_snapshot(pid, owner=0, leader_reaped=True)
        assert snap.members == {} and snap.descendants == 0
        portable_procs.drop_session(pid)

    def test_leader_gone_is_empty(self) -> None:
        pid = _dead_pid()
        snap = portable_procs.session_snapshot(pid, owner=0, leader_reaped=False)
        assert snap.members == {}
        portable_procs.drop_session(pid)

    def test_leader_children_vanish(self) -> None:
        p = self._tree()

        def _gone(self, recursive=False):
            raise psutil.NoSuchProcess(self.pid)

        try:
            with patch_method("children", _gone):
                snap = portable_procs.session_snapshot(
                    p.pid, owner=id(p), leader_reaped=False
                )
            assert list(snap.members) == [p.pid]
        finally:
            portable_procs.drop_session(p.pid)
            for c in psutil.Process(p.pid).children():
                c.kill()
            p.kill()
            p.wait()

    def test_access_denied_on_leader_propagates(self, monkeypatch) -> None:
        def _deny(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", _deny)
        with pytest.raises(psutil.AccessDenied):
            portable_procs.session_snapshot(os.getpid(), owner=0, leader_reaped=False)
        portable_procs.drop_session(os.getpid())

    def test_foreign_uid_dropped(self) -> None:
        p = self._tree()
        real = psutil.Process.uids

        def _uids(self):
            u = real(self)
            if self.pid != p.pid:
                return type(u)(u.real + 1, u.effective, u.saved)
            return u

        def _gone_uids(self):
            raise psutil.NoSuchProcess(self.pid)

        try:
            with patch_method("uids", _uids):
                snap = portable_procs.session_snapshot(
                    p.pid, owner=id(p), leader_reaped=False
                )
            assert snap.dropped_foreign_uid == 2
            assert snap.descendants == 0
            portable_procs.drop_session(p.pid)
            with patch_method("uids", _gone_uids):
                snap = portable_procs.session_snapshot(
                    p.pid, owner=id(p), leader_reaped=False
                )
            assert snap.dropped_foreign_uid == 0 and snap.descendants == 0
        finally:
            portable_procs.drop_session(p.pid)
            for c in psutil.Process(p.pid).children():
                c.kill()
            p.kill()
            p.wait()

    def test_signal_errors_swallowed(self, caplog) -> None:
        snap = portable_procs.SessionSnapshot(owner=0, leader_pid=-5)

        class _P:
            def __init__(self, pid: int, exc: BaseException) -> None:
                self.pid = pid
                self.exc = exc

            def is_running(self) -> bool:
                return True

            def status(self) -> str:
                return psutil.STATUS_SLEEPING

            def send_signal(self, sig: int) -> None:
                raise self.exc

        snap.members = {
            1: _P(1, ProcessLookupError()),
            2: _P(2, PermissionError()),
            3: _P(3, psutil.NoSuchProcess(3)),
        }
        caplog.set_level("INFO")
        assert portable_procs.signal_members(snap, signal.SIGTERM) == 0
        assert caplog.text.count("session-signal:") == 3

    def test_member_live_false_for_dead(self) -> None:
        p = subprocess.Popen(["true"])
        proc = psutil.Process(p.pid)
        p.wait()
        assert portable_procs.member_live(proc) is False
