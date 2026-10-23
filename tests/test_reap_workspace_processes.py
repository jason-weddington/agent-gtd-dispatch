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
        ws = tmp_path

        async def rescue(*a: object, **k: object) -> None:
            calls.append("rescue")

        def reap(w: Path) -> list[ReapedProcess]:
            calls.append("reap")
            return []

        with (
            patch.object(main, "_rescue_before_teardown", rescue),
            patch.object(main.dispatch, "reap_workspace_processes", reap),
            patch.object(
                main.dispatch, "cleanup_workspace", lambda w: calls.append("cleanup")
            ),
            patch.object(main.dispatch, "_executor", None),
        ):
            await main._rescue_before_teardown(run, [], attribution=None)
            await main._reap_leaked_processes(run, ws)
            if should_cleanup:
                main.dispatch.cleanup_workspace(ws)
        assert calls == ["rescue", "reap"] + (["cleanup"] if should_cleanup else [])

    async def test_records_reaped(self, tmp_path: Path) -> None:
        run = MagicMock(id="r1")
        reaped = [ReapedProcess(5, "kb-service serve", "SIGTERM")]
        db = MagicMock()
        db.get_run = AsyncMock(return_value=MagicMock(error=None))
        db.update_run = AsyncMock()
        with (
            patch.object(main, "db", db),
            patch.object(main.dispatch, "reap_workspace_processes", lambda w: reaped),
            patch.object(main.dispatch, "_executor", None),
        ):
            await main._reap_leaked_processes(run, tmp_path)
        err = db.update_run.call_args.kwargs["error"]
        assert "reaped 1 leaked process(es)" in err and "kb-service serve" in err


def test_module_exports() -> None:
    assert dispatch.REAP_TERM_GRACE_SECS > 0
