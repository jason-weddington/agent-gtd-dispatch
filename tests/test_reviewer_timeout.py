"""A reviewer that hits its timeout is recorded as a timeout, not a no-verdict."""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import ExitStack
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, db, dispatch, main
from agent_gtd_dispatch.engines import CLAUDE
from agent_gtd_dispatch.models import RunStatus

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


TIMEOUT = subprocess.TimeoutExpired(cmd=["claude"], timeout=2700)


async def _review(
    tmp_path: Path,
    *,
    run_agent_effect: Any,
    verdict: tuple[dict[str, Any] | None, str] = (None, "absent"),
    marker_reaped: list[dispatch.ReapedProcess] | None = None,
) -> tuple[main.ReviewVerdict, AsyncMock, MagicMock, MagicMock]:
    await db.init_db()
    (tmp_path / "transcript.txt").write_text("I was still running the gate\n")
    workspace = SimpleNamespace(
        root=tmp_path,
        repo_paths={"repo": tmp_path},
        default_branches={"repo": "main"},
        workspace_mode=False,
    )
    update_run = AsyncMock(wraps=db.update_run)
    marker_reap = MagicMock(
        return_value=dispatch.MarkerReap(
            reaped=marker_reaped or [],
            survivors=0,
            scan_ok=True,
            escaped_session=0,
        )
    )
    ws_reap = MagicMock(return_value=[])
    with ExitStack() as stack:
        mock_gtd = stack.enter_context(patch("agent_gtd_dispatch.main.gtd_client"))
        mock_gtd.get_item = AsyncMock(
            return_value={"id": "item-t", "title": "X", "commit_type": "fix"}
        )
        mock_gtd.list_runs_for_item = AsyncMock(return_value=[])
        mock_gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
        for name, value in {
            "build_review_prompt": MagicMock(return_value="prompt"),
            "compose_squash_subject": MagicMock(return_value=None),
            "clear_review_verdict": MagicMock(),
            "run_agent": AsyncMock(side_effect=run_agent_effect),
            "read_review_verdict": MagicMock(return_value=verdict),
            "reap_marker_processes": marker_reap,
            "reap_workspace_processes": ws_reap,
        }.items():
            stack.enter_context(patch.object(dispatch, name, value))
        stack.enter_context(patch.object(main.db, "update_run", update_run))
        result = await main._launch_reviewer(
            "ro-1",
            "item-t",
            project={"name": "P"},
            workspace=workspace,  # type: ignore[arg-type]
            reviewer_engine=CLAUDE,
            token=None,
            attribution=None,
        )
    return result, update_run, marker_reap, ws_reap


def _terminal_call(update_run: AsyncMock) -> Any:
    return next(c for c in update_run.call_args_list if "status" in c.kwargs)


class TestReviewerTimeout:
    async def test_timeout_is_its_own_outcome(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            verdict, update_run, _mr, _wr = await _review(
                tmp_path, run_agent_effect=TIMEOUT
            )
        assert verdict.verdict == "halt"
        assert verdict.rationale == "reviewer timed out after 45m"
        call = _terminal_call(update_run)
        assert call.kwargs["status"] == RunStatus.timed_out
        error = call.kwargs["error"]
        assert error.startswith("review_timeout: reviewer timed out after 45m")
        assert "(timeout=2700s)" in error
        assert "; agent said:" in error
        assert "TimeoutExpired" not in error
        assert "'claude'" not in error
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("timed out after 45m" in m for m in warnings)
        assert not any("produced no usable verdict" in m for m in warnings)
        assert not any("TimeoutExpired" in r.getMessage() for r in caplog.records)
        assert all(r.exc_info is None for r in caplog.records)

    async def test_generic_failure_unchanged(self, tmp_path) -> None:
        verdict, update_run, _mr, _wr = await _review(
            tmp_path, run_agent_effect=RuntimeError("boom")
        )
        assert verdict.verdict == "halt"
        assert verdict.rationale == "reviewer returned no usable verdict"
        call = _terminal_call(update_run)
        assert call.kwargs["status"] == RunStatus.failed
        assert call.kwargs["error"].startswith("review_no_verdict:")

    async def test_timeout_with_verdict_acts_on_it(self, tmp_path, caplog) -> None:
        blob = {"verdict": "halt", "rationale": "found a bug", "commit_type": "fix"}
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            verdict, update_run, _mr, _wr = await _review(
                tmp_path, run_agent_effect=TIMEOUT, verdict=(blob, "ok")
            )
        assert verdict.verdict == "halt"
        assert verdict.rationale == "found a bug"
        assert _terminal_call(update_run).kwargs["status"] == RunStatus.succeeded
        assert any(
            "timed out after 45m but wrote verdict=halt" in r.getMessage()
            for r in caplog.records
        )

    async def test_reap_note_lands_in_error(self, tmp_path) -> None:
        reaped = [dispatch.ReapedProcess(4321, "bash scripts/gate.sh full", "SIGKILL")]
        _verdict, update_run, marker_reap, ws_reap = await _review(
            tmp_path, run_agent_effect=TIMEOUT, marker_reaped=reaped
        )
        error = _terminal_call(update_run).kwargs["error"]
        assert "review_timeout:" in error
        assert "reaped 1 leaked process(es) by marker" in error
        assert "pid 4321" in error
        marker = marker_reap.call_args.args[0]
        assert marker.startswith("rt-")
        assert "label=reviewer" in marker_reap.call_args.kwargs["context"]
        own = {c.kwargs.get("own_marker") for c in ws_reap.call_args_list}
        assert own == {marker}

    async def test_reap_note_appended_on_verdict_path(self, tmp_path) -> None:
        blob = {"verdict": "merge", "rationale": "ok", "commit_type": "fix"}
        reaped = [dispatch.ReapedProcess(4321, "vite", "SIGTERM")]
        _verdict, update_run, _mr, _wr = await _review(
            tmp_path,
            run_agent_effect=[MagicMock(returncode=0)],
            verdict=(blob, "ok"),
            marker_reaped=reaped,
        )
        last = update_run.call_args_list[-1]
        assert "status" not in last.kwargs
        assert last.kwargs["error"].startswith("reaped 1 leaked process(es) by marker")
