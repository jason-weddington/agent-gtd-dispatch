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
    gate_failure_output: str | None = None,
    land_conflict: dict[str, list[str]] | None = None,
    refresh_result: dict[str, str] | None = None,
    calls: list[str] | None = None,
    project: dict[str, Any] | None = None,
    run_agent_mock: list[AsyncMock] | None = None,
    prompt_mock: list[MagicMock] | None = None,
) -> tuple[main.ReviewVerdict, AsyncMock, MagicMock, MagicMock, MagicMock]:
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
    refresh = MagicMock(return_value=refresh_result or {})
    review_run_inserted = {"value": False}
    real_insert_run = db.insert_run
    if calls is not None:

        def _on_refresh(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            assert not review_run_inserted["value"], (
                "the REVIEW run row already exists when the ref refresh runs"
            )
            calls.append("refresh")
            return dict(refresh_result or {})

        refresh.side_effect = _on_refresh

    with ExitStack() as stack:
        mock_gtd = stack.enter_context(patch("agent_gtd_dispatch.main.gtd_client"))
        mock_gtd.get_item = AsyncMock(
            return_value={"id": "item-t", "title": "X", "commit_type": "fix"}
        )
        mock_gtd.list_runs_for_item = AsyncMock(return_value=[])
        mock_gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
        agent_mock = AsyncMock(side_effect=run_agent_effect)
        if run_agent_mock is not None:
            run_agent_mock.append(agent_mock)
        prompt_spy = MagicMock(return_value="prompt")
        if prompt_mock is not None:
            prompt_mock.append(prompt_spy)
        for name, value in {
            "build_review_prompt": prompt_spy,
            "compose_squash_subject": MagicMock(return_value=None),
            "clear_review_verdict": MagicMock(),
            "run_agent": agent_mock,
            "read_review_verdict": MagicMock(return_value=verdict),
            "reap_marker_processes": marker_reap,
            "reap_workspace_processes": ws_reap,
            "refresh_feature_branch_refs": refresh,
        }.items():
            stack.enter_context(patch.object(dispatch, name, value))
        stack.enter_context(patch.object(main.db, "update_run", update_run))
        if calls is not None:

            async def _insert_run_spy(run: Any) -> None:
                await real_insert_run(run)
                review_run_inserted["value"] = True

            stack.enter_context(patch.object(main.db, "insert_run", _insert_run_spy))
        result = await main._launch_reviewer(
            "ro-1",
            "item-t",
            project=project if project is not None else {"name": "P"},
            workspace=workspace,  # type: ignore[arg-type]
            reviewer_engine=CLAUDE,
            token=None,
            attribution=None,
            gate_failure_output=gate_failure_output,
            land_conflict=land_conflict,
        )
    return result, update_run, marker_reap, ws_reap, refresh


def _terminal_call(update_run: AsyncMock) -> Any:
    return next(c for c in update_run.call_args_list if "status" in c.kwargs)


class TestReviewerTimeout:
    async def test_timeout_is_its_own_outcome(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            verdict, update_run, _mr, _wr, _refresh = await _review(
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
        verdict, update_run, _mr, _wr, _refresh = await _review(
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
            verdict, update_run, _mr, _wr, _refresh = await _review(
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
        _verdict, update_run, marker_reap, ws_reap, _refresh = await _review(
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
        _verdict, update_run, _mr, _wr, _refresh = await _review(
            tmp_path,
            run_agent_effect=[MagicMock(returncode=0)],
            verdict=(blob, "ok"),
            marker_reaped=reaped,
        )
        last = update_run.call_args_list[-1]
        assert "status" not in last.kwargs
        assert last.kwargs["error"].startswith("reaped 1 leaked process(es) by marker")


class TestSignalKilledReviewer:
    """AC-17: a signal-killed reviewer is reported as killed, not as "agent said"."""

    @staticmethod
    async def _review_signal(tmp_path, returncode: int, caplog=None):
        async def _run_agent(*_args: Any, **_kwargs: Any) -> Any:
            # A killed `--print` run leaves only this benign startup line.
            (tmp_path / "transcript.txt").write_text(
                "[claude-code:unrecognized_model]\n"
            )
            return MagicMock(returncode=returncode)

        ctx = (
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main")
            if caplog is not None
            else ExitStack()
        )
        with ctx:
            verdict, update_run, _mr, _wr, _rf = await _review(
                tmp_path,
                run_agent_effect=_run_agent,
            )
        return verdict, _terminal_call(update_run)

    async def test_negative_returncode_reports_the_signal(
        self, tmp_path, caplog
    ) -> None:
        verdict, call = await self._review_signal(tmp_path, -9, caplog)
        assert call.kwargs["status"] == RunStatus.failed
        error = call.kwargs["error"]
        assert error.startswith("review_no_verdict:")
        assert (
            "verdict artifact absent (reviewer killed by signal 9 (SIGKILL))" in error
        )
        assert "agent said" not in error
        assert "unrecognized_model" not in error
        assert verdict.rationale == "reviewer returned no usable verdict"
        assert verdict.verdict == "halt"
        assert any(
            "signal=9" in r.getMessage()
            and "signal_name=SIGKILL" in r.getMessage()
            and "detail_source=signal" in r.getMessage()
            for r in caplog.records
        )

    async def test_shell_style_143_reports_sigterm(self, tmp_path) -> None:
        _verdict, call = await self._review_signal(tmp_path, 143)
        error = call.kwargs["error"]
        assert (
            "verdict artifact absent (reviewer killed by signal 15 (SIGTERM))" in error
        )
        assert "agent said" not in error

    async def test_negative_15_reports_the_same_signal(self, tmp_path) -> None:
        _verdict, call = await self._review_signal(tmp_path, -15)
        assert (
            "verdict artifact absent (reviewer killed by signal 15 (SIGTERM))"
            in call.kwargs["error"]
        )

    async def test_plain_exit_still_quotes_the_agent(self, tmp_path) -> None:
        _verdict, call = await self._review_signal(tmp_path, 1)
        error = call.kwargs["error"]
        assert "verdict artifact absent (reviewer exit=1)" in error
        assert "; agent said:" in error
        assert "unrecognized_model" in error


class TestReviewBranchRefreshCallSite:
    """AC-11: the worker refreshes feature refs before a NORMAL review launch."""

    async def test_normal_launch_refreshes_refs_before_run_insert(
        self, tmp_path
    ) -> None:
        from agent_gtd_dispatch_protocol.branches import make_branch_name

        calls: list[str] = []

        async def _run_agent(*_args: Any, **_kwargs: Any) -> Any:
            calls.append("run_agent")
            return SimpleNamespace(returncode=0)

        _verdict, _update_run, _mr, _wr, refresh = await _review(
            tmp_path, run_agent_effect=_run_agent, calls=calls
        )
        # _on_refresh asserts the REVIEW run row does not exist yet.
        expected_branch = make_branch_name("item-t", "X")
        expected_workspace = SimpleNamespace(
            root=tmp_path,
            repo_paths={"repo": tmp_path},
            default_branches={"repo": "main"},
            workspace_mode=False,
        )
        refresh.assert_called_once_with(
            expected_workspace, expected_branch, config.LAND_GIT_TIMEOUT_SECONDS
        )
        assert calls[:2] == ["refresh", "run_agent"]

    async def test_fix_mode_relaunch_skips_refresh(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            _verdict, _u, _mr, _wr, refresh = await _review(
                tmp_path,
                run_agent_effect=TIMEOUT,
                gate_failure_output="boom",
            )
        assert refresh.call_count == 0
        assert any(
            "review-branch-refresh skipped reason=fix_mode" in r.getMessage()
            for r in caplog.records
        )

    async def test_land_conflict_relaunch_skips_refresh(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            _verdict, _u, _mr, _wr, refresh = await _review(
                tmp_path,
                run_agent_effect=TIMEOUT,
                land_conflict={"repo": ["a.py"]},
            )
        assert refresh.call_count == 0
        assert any(
            "review-branch-refresh skipped reason=land_conflict" in r.getMessage()
            for r in caplog.records
        )

    async def test_none_refreshed_warns(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"):
            _verdict, _u, _mr, _wr, _refresh = await _review(
                tmp_path,
                run_agent_effect=TIMEOUT,
                refresh_result={"repo": "fetch_failed"},
            )
        assert any(
            "review-branch-refresh none-refreshed" in r.getMessage()
            for r in caplog.records
        )


def _timeout_for(seconds: int) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(cmd=["claude"], timeout=seconds)


class TestReviewTimeoutOverride:
    async def test_override_used_when_set(self, tmp_path) -> None:
        agent: list[AsyncMock] = []
        verdict, update_run, _mr, _wr, _rf = await _review(
            tmp_path,
            run_agent_effect=_timeout_for(7200),
            project={"name": "P", "review_timeout_seconds": 7200},
            run_agent_mock=agent,
        )
        assert agent[0].call_args.kwargs["timeout_seconds"] == 7200
        assert verdict.verdict == "halt"
        assert verdict.rationale == "reviewer timed out after 120m"
        assert verdict.detail.startswith(
            "reviewer timed out after 120m (timeout=7200s)"
        )
        error = _terminal_call(update_run).kwargs["error"]
        assert error.startswith(
            "review_timeout: reviewer timed out after 120m (timeout=7200s)"
        )

    @pytest.mark.parametrize(
        "project",
        [{"name": "P"}, {"name": "P", "review_timeout_seconds": None}],
    )
    async def test_env_default_when_unset(self, tmp_path, project) -> None:
        agent: list[AsyncMock] = []
        verdict, _ur, _mr, _wr, _rf = await _review(
            tmp_path,
            run_agent_effect=TIMEOUT,
            project=project,
            run_agent_mock=agent,
        )
        assert agent[0].call_args.kwargs["timeout_seconds"] == (
            config.REVIEW_TIMEOUT_SECONDS
        )
        assert verdict.rationale == "reviewer timed out after 45m"

    async def test_prompt_and_run_agent_share_override(self, tmp_path, caplog) -> None:
        agent: list[AsyncMock] = []
        prompt: list[MagicMock] = []
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await _review(
                tmp_path,
                run_agent_effect=_timeout_for(7200),
                project={"name": "P", "review_timeout_seconds": 7200},
                run_agent_mock=agent,
                prompt_mock=prompt,
            )
        assert prompt[0].call_args.kwargs["review_timeout_seconds"] == 7200
        assert agent[0].call_args.kwargs["timeout_seconds"] == 7200
        assert "review_timeout_s=7200" in caplog.text
        assert "prompt_bash_timeout_ms=7200000" in caplog.text

    @pytest.mark.parametrize(
        "project",
        [
            {"name": "P"},
            {"name": "P", "review_timeout_seconds": None},
            {"name": "P", "review_timeout_seconds": 5},
        ],
    )
    async def test_prompt_and_run_agent_default(self, tmp_path, project) -> None:
        agent: list[AsyncMock] = []
        prompt: list[MagicMock] = []
        await _review(
            tmp_path,
            run_agent_effect=TIMEOUT,
            project=project,
            run_agent_mock=agent,
            prompt_mock=prompt,
        )
        want = config.REVIEW_TIMEOUT_SECONDS
        assert prompt[0].call_args.kwargs["review_timeout_seconds"] == want
        assert agent[0].call_args.kwargs["timeout_seconds"] == want

    async def test_env_patched_default_vs_override(self, tmp_path) -> None:
        with patch.dict(os.environ, {"DISPATCH_REVIEW_TIMEOUT_SECONDS": "3600"}):
            config.load()
            agent: list[AsyncMock] = []
            await _review(
                tmp_path,
                run_agent_effect=TIMEOUT,
                run_agent_mock=agent,
            )
            assert agent[0].call_args.kwargs["timeout_seconds"] == 3600
            agent2: list[AsyncMock] = []
            await _review(
                tmp_path,
                run_agent_effect=TIMEOUT,
                project={"name": "P", "review_timeout_seconds": 900},
                run_agent_mock=agent2,
            )
            assert agent2[0].call_args.kwargs["timeout_seconds"] == 900

    async def test_late_verdict_warning_uses_override(self, tmp_path, caplog) -> None:
        blob = {"verdict": "halt", "rationale": "found a bug", "commit_type": "fix"}
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await _review(
                tmp_path,
                run_agent_effect=_timeout_for(7200),
                verdict=(blob, "ok"),
                project={"name": "P", "review_timeout_seconds": 7200},
            )
        assert any(
            "timed out after 120m but wrote verdict" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        ("value", "source", "reason"),
        [
            (7200, "project", None),
            (599, "invalid_project_value", "range"),
            (14401, "invalid_project_value", "range"),
            (True, "invalid_project_value", "type"),
            (None, "env_default", None),
        ],
    )
    async def test_observability(self, tmp_path, caplog, value, source, reason) -> None:
        project: dict[str, Any] = {"name": "P"}
        if value is not None:
            project["review_timeout_seconds"] = value
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await _review(tmp_path, run_agent_effect=TIMEOUT, project=project)
        msgs = [r.getMessage() for r in caplog.records]
        spawn = next(m for m in msgs if "reviewer spawn" in m)
        assert f"review_timeout_source={source}" in spawn
        expected_s = value if source == "project" else config.REVIEW_TIMEOUT_SECONDS
        assert f"review_timeout_s={expected_s} " in spawn + " "
        warns = [m for m in msgs if "review-timeout-override-invalid" in m]
        if reason is None:
            assert warns == []
        else:
            assert len(warns) == 1
            assert f"reason={reason}" in warns[0]
            assert f"fallback_s={config.REVIEW_TIMEOUT_SECONDS}" in warns[0]


class TestResolveReviewTimeout:
    @pytest.mark.parametrize("value", [600, 14400])
    def test_valid(self, value) -> None:
        assert config.resolve_review_timeout_seconds(value) == (value, "project")

    @pytest.mark.parametrize("value", [599, 14401, 0, -1, True, 7200.0, "7200", []])
    def test_invalid_falls_back(self, value) -> None:
        assert config.resolve_review_timeout_seconds(value) == (
            config.REVIEW_TIMEOUT_SECONDS,
            "invalid_project_value",
        )

    def test_none(self) -> None:
        assert config.resolve_review_timeout_seconds(None) == (
            config.REVIEW_TIMEOUT_SECONDS,
            "env_default",
        )

    def test_bounds_match_board_twin(self) -> None:
        # Twin of MIN/MAX_REVIEW_TIMEOUT_SECONDS in agent_gtd/dispatch_constants.py.
        assert config.MIN_REVIEW_TIMEOUT_OVERRIDE_SECONDS == 600
        assert config.MAX_REVIEW_TIMEOUT_OVERRIDE_SECONDS == 14400
