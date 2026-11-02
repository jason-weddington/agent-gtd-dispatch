from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, dispatch, main
from agent_gtd_dispatch.dispatch import ReapedProcess, reap_workspace_processes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SLEEP = "import time; time.sleep(60)"
IGNORE_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "print('ready', flush=True); time.sleep(60)"
)


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    r = tmp_path.resolve() / "workspaces"
    r.mkdir()
    with (
        patch.object(config, "WORKSPACE_ROOT", r),
        patch.object(config, "AGENT_SUBPROCESS_USER", ""),
    ):
        yield r


def _spawn(cwd: Path, code: str = SLEEP) -> subprocess.Popen[str]:
    cwd.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    if "ready" in code:
        assert p.stdout is not None
        p.stdout.readline()
    else:
        time.sleep(0.3)
    return p


def _dead(p: subprocess.Popen[str]) -> bool:
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return False
    return True


class TestReap:
    def test_kills_process_in_workspace(self, root: Path) -> None:
        p = _spawn(root / "repos-a")
        try:
            out = reap_workspace_processes(root / "repos-a", term_grace_secs=2)
            assert [r.pid for r in out] == [p.pid]
            assert isinstance(out[0], ReapedProcess)
            assert out[0].killed_with == "SIGTERM"
            assert _dead(p)
        finally:
            p.kill()

    def test_string_prefix_sibling_untouched(self, root: Path) -> None:
        mine = _spawn(root / "repos-a")
        sibling = _spawn(root / "repos-ab")
        try:
            reap_workspace_processes(root / "repos-a", term_grace_secs=2)
            assert _dead(mine)
            assert sibling.poll() is None
        finally:
            mine.kill()
            sibling.kill()

    def test_outside_process_untouched(self, root: Path, tmp_path: Path) -> None:
        (root / "repos-a").mkdir()
        outside = _spawn(tmp_path / "elsewhere")
        try:
            assert reap_workspace_processes(root / "repos-a", term_grace_secs=1) == []
            assert outside.poll() is None
        finally:
            outside.kill()

    def test_sigterm_ignorer_gets_sigkill(self, root: Path) -> None:
        p = _spawn(root / "repos-a", IGNORE_TERM)
        try:
            out = reap_workspace_processes(root / "repos-a", term_grace_secs=0.5)
            assert [(r.pid, r.killed_with) for r in out] == [(p.pid, "SIGKILL")]
            assert _dead(p)
        finally:
            p.kill()

    def test_workspace_outside_root_returns_empty(
        self, root: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "notroot"
        p = _spawn(other)
        try:
            assert reap_workspace_processes(other, term_grace_secs=1) == []
            assert p.poll() is None
        finally:
            p.kill()


class TestTeardownOrdering:
    @pytest.mark.parametrize("should_cleanup", [True, False])
    async def test_order(self, should_cleanup: bool, tmp_path: Path) -> None:
        calls: list[str] = []
        run = MagicMock(id="r1")
        ws = tmp_path / "ws"
        tmp = tmp_path / "rt"

        async def rescue(*a: object, **k: object) -> None:
            calls.append("rescue")

        def reap(w: Path, **_: object) -> list[ReapedProcess]:
            calls.append(f"reap:{w}")
            return []

        def remove_tmp(p: Path, *, kind: str) -> None:
            calls.append("remove_tmp")

        with (
            patch.object(main, "_rescue_before_teardown", rescue),
            patch.object(main.dispatch, "reap_workspace_processes", reap),
            patch.object(main.run_tmp, "remove_run_tmp", remove_tmp),
            patch.object(
                main.dispatch, "cleanup_workspace", lambda w: calls.append("cleanup")
            ),
            patch.object(main.dispatch, "_executor", None),
        ):
            await main._rescue_before_teardown(run, [], attribution=None)
            await main._reap_leaked_processes(run, ws, tmp)
            main.run_tmp.remove_run_tmp(tmp, kind="build")
            if should_cleanup:
                main.dispatch.cleanup_workspace(ws)
        assert calls == ["rescue", f"reap:{ws}", f"reap:{tmp}", "remove_tmp"] + (
            ["cleanup"] if should_cleanup else []
        )

    async def test_records_reaped(self, tmp_path: Path) -> None:
        run = MagicMock(id="r1")
        reaped = [ReapedProcess(5, "kb-service serve", "SIGTERM")]
        db = MagicMock()
        db.get_run = AsyncMock(return_value=MagicMock(error=None))
        db.update_run = AsyncMock()
        with (
            patch.object(main, "db", db),
            patch.object(
                main.dispatch, "reap_workspace_processes", lambda w, **_: reaped
            ),
            patch.object(main.dispatch, "_executor", None),
        ):
            await main._reap_leaked_processes(run, tmp_path)
        err = db.update_run.call_args.kwargs["error"]
        assert "reaped 1 leaked process(es)" in err and "kb-service serve" in err
        assert "from run dirs:" in err

    async def test_tmp_dir_only(self, tmp_path: Path) -> None:
        run = MagicMock(id="r1")
        seen: list[Path] = []

        def reap(w: Path, **_: object) -> list[ReapedProcess]:
            seen.append(w)
            return []

        with (
            patch.object(main.dispatch, "reap_workspace_processes", reap),
            patch.object(main.dispatch, "_executor", None),
        ):
            await main._reap_leaked_processes(run, tmp_path / "rt")
        assert seen == [tmp_path / "rt"]


@pytest.fixture
def rt_root() -> Path:
    # _isolate_run_tmp_root already points config.RUN_TMP_ROOT at a fresh dir.
    return config.RUN_TMP_ROOT


class TestRunTmpEligibility:
    def test_run_tmp_dir_is_reaped(self, root: Path, rt_root: Path) -> None:
        mine = _spawn(rt_root / "rt-aaaaaaaaaaab" / "sub")
        other = _spawn(rt_root / "rt-aaaaaaaaaaac")
        try:
            out = reap_workspace_processes(
                rt_root / "rt-aaaaaaaaaaab", term_grace_secs=2
            )
            assert [r.pid for r in out] == [mine.pid]
            assert _dead(mine)
            assert other.poll() is None
        finally:
            mine.kill()
            other.kill()

    def test_not_a_run_dir_ignored_and_logged(
        self, root: Path, rt_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        d = rt_root / "not-a-run"
        p = _spawn(d)
        try:
            assert reap_workspace_processes(d, term_grace_secs=1) == []
            assert p.poll() is None
        finally:
            p.kill()
        assert any(
            "reap-backstop: ignored" in r.getMessage()
            and "reason=outside_roots" in r.getMessage()
            for r in caplog.records
        )

    def test_isolation_between_runs(self, root: Path, rt_root: Path) -> None:
        a = _spawn(root / "repos-a")
        ab = _spawn(root / "repos-ab")
        other_tmp = _spawn(rt_root / "rt-bbbbbbbbbbbb")
        try:
            reap_workspace_processes(root / "repos-a", term_grace_secs=2)
            reap_workspace_processes(rt_root / "rt-aaaaaaaaaaaa", term_grace_secs=2)
            assert _dead(a)
            assert ab.poll() is None
            assert other_tmp.poll() is None
        finally:
            for p in (a, ab, other_tmp):
                p.kill()


class TestOwnershipFilter:
    def test_foreign_owner_not_reaped(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import os

        p = _spawn(root / "repos-a")
        try:
            with patch.object(dispatch, "_agent_uid", return_value=os.getuid() + 1):
                assert (
                    reap_workspace_processes(root / "repos-a", term_grace_secs=1) == []
                )
            assert p.poll() is None
        finally:
            p.kill()
        assert any(
            "reason=foreign_owner" in r.getMessage()
            and f"pid={p.pid}" in r.getMessage()
            for r in caplog.records
        )

    def test_agent_owned_kept(self, root: Path) -> None:
        p = _spawn(root / "repos-a")
        try:
            with (
                patch.object(dispatch, "_agent_uid", return_value=12345),
                patch.object(dispatch, "_proc_real_uid", return_value=12345),
            ):
                out = reap_workspace_processes(root / "repos-a", term_grace_secs=2)
            assert [r.pid for r in out] == [p.pid]
            assert _dead(p)
        finally:
            p.kill()

    def test_unknown_agent_user_reaps_nothing(self, root: Path) -> None:
        p = _spawn(root / "repos-a")
        try:
            with patch.object(dispatch, "_agent_uid", return_value=None):
                assert (
                    reap_workspace_processes(root / "repos-a", term_grace_secs=1) == []
                )
            assert p.poll() is None
        finally:
            p.kill()

    def test_agent_user_scan_bypasses_filter(self, root: Path) -> None:
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(dispatch, "_agent_uid", return_value=12345),
            patch.object(dispatch, "_scan_workspace_pids", return_value={}),
            patch.object(
                dispatch, "_scan_as_agent_user", return_value=(True, {777: ("", "x")})
            ),
            patch.object(dispatch, "_proc_is_live", return_value=False),
            patch.object(dispatch, "_proc_real_uid") as uid,
        ):
            assert reap_workspace_processes(root / "repos-a") == []
        uid.assert_not_called()

    def test_proc_real_uid_parses_status(self) -> None:
        status = "Name:\tsleep\nUid:\t1001\t1001\t1001\t1001\nGid:\t1\n"
        with patch.object(dispatch.Path, "read_text", return_value=status):
            assert dispatch._proc_real_uid(42) == 1001

    def test_proc_real_uid_unreadable_or_garbled(self) -> None:
        with patch.object(dispatch.Path, "read_text", side_effect=OSError):
            assert dispatch._proc_real_uid(42) is None
        with patch.object(dispatch.Path, "read_text", return_value="Uid:\tx\n"):
            assert dispatch._proc_real_uid(42) is None
        with patch.object(dispatch.Path, "read_text", return_value="Name: a\n"):
            assert dispatch._proc_real_uid(42) is None

    def test_agent_uid(self) -> None:
        import os

        with patch.object(config, "AGENT_SUBPROCESS_USER", ""):
            assert dispatch._agent_uid() == os.getuid()
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "dispatch"),
            patch.object(dispatch.pwd, "getpwnam", return_value=MagicMock(pw_uid=77)),
        ):
            assert dispatch._agent_uid() == 77
        with (
            patch.object(config, "AGENT_SUBPROCESS_USER", "nosuchuser"),
            patch.object(dispatch.pwd, "getpwnam", side_effect=KeyError),
        ):
            assert dispatch._agent_uid() is None


def test_module_exports() -> None:
    assert dispatch.REAP_TERM_GRACE_SECS > 0
