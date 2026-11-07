"""Gate install in the reviewer's clone (item 1f92702f)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import dispatch, gates, main
from agent_gtd_dispatch.engines import get_engine
from tests.test_reviewer_timeout import _env, _review  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

FAILURE = gates.GateFailure(
    repo_label="repo",
    step="pre-commit install",
    reason="exit code 1",
    output_tail="",
    duration_ms=1,
)
GATED = {"name": "P", "gate_command": "make check"}


def _ws(root: Path, *, workspace_mode: bool) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        repo_paths={"a": root / "a", "b": root / "b"},
        default_branches={},
        workspace_mode=workspace_mode,
    )


class TestRunGateInstall:
    async def test_no_steps(self, tmp_path) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=[]),
            patch.object(gates, "run_gate_steps") as run,
        ):
            res = await main._run_gate_install(
                _ws(tmp_path, workspace_mode=True),  # type: ignore[arg-type]
                GATED,
                run_id="r",
                tmp_dir=None,
                executor=None,
            )
        assert res.outcome == "no-steps"
        run.assert_not_called()

    async def test_failure_detail(self, tmp_path) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", return_value=FAILURE),
        ):
            res = await main._run_gate_install(
                _ws(tmp_path, workspace_mode=True),  # type: ignore[arg-type]
                GATED,
                run_id="r",
                tmp_dir=None,
                executor=None,
            )
        assert res.outcome == "failed"
        assert res.failure is FAILURE
        assert res.detail == "repo: pre-commit install: exit code 1"

    async def test_installed(self, tmp_path) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", return_value=None),
        ):
            res = await main._run_gate_install(
                _ws(tmp_path, workspace_mode=True),  # type: ignore[arg-type]
                GATED,
                run_id="r",
                tmp_dir=None,
                executor=None,
            )
        assert res.outcome == "installed"

    async def test_workspace_mode_repos(self, tmp_path) -> None:
        with patch.object(gates, "detect_gate_steps", return_value=[]) as detect:
            await main._run_gate_install(
                _ws(tmp_path, workspace_mode=True),  # type: ignore[arg-type]
                GATED,
                run_id="r",
                tmp_dir=None,
                executor=None,
            )
        assert detect.call_args.args[0] == [
            ("a", tmp_path / "a"),
            ("b", tmp_path / "b"),
        ]

    async def test_mono_repo(self, tmp_path) -> None:
        project = {**GATED, "git_origin": "git@host:org/my-repo.git"}
        with patch.object(gates, "detect_gate_steps", return_value=[]) as detect:
            await main._run_gate_install(
                _ws(tmp_path, workspace_mode=False),  # type: ignore[arg-type]
                project,
                run_id="r",
                tmp_dir=None,
                executor=None,
            )
        assert detect.call_args.args[0] == [
            (dispatch.repo_name_from_origin("git@host:org/my-repo.git"), tmp_path)
        ]

    async def test_land_step_halts_on_failure(self, tmp_path) -> None:
        ws = _ws(tmp_path, workspace_mode=True)
        ws.default_branches = {"a": "main", "b": "main"}
        lander = main._Lander(
            "ro-1",
            "item-t",
            MagicMock(),
            project=GATED,
            workspace=ws,  # type: ignore[arg-type]
            pre_review_origin={},
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="1",
            token=None,
            attribution=None,
        )
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", return_value=FAILURE),
            pytest.raises(main._LandHalt) as exc,
        ):
            await lander._install_gate()
        assert exc.value.outcome == "gate_install_failed"
        assert exc.value.head == (
            "land gate install failed for item item-t: "
            "repo: pre-commit install: exit code 1"
        )


async def _launch(tmp_path, *, project=None, **kw):
    calls: list[str] = []
    prompt: list[MagicMock] = []
    agent: list[AsyncMock] = []
    out = await _review(
        tmp_path,
        run_agent_effect=lambda *a, **k: (
            calls.append("agent") or MagicMock(returncode=0)
        ),
        project=project if project is not None else GATED,
        calls=calls,
        prompt_mock=prompt,
        run_agent_mock=agent,
        **kw,
    )
    return out, calls, prompt[0], agent[0]


class TestLaunchPath:
    async def test_installed_ordering(self, tmp_path, caplog) -> None:
        calls: list[str] = []

        def _detect(*_a, **_k):
            calls.append("install")
            return ["s"]

        prompt: list[MagicMock] = []
        agent: list[AsyncMock] = []
        spy = MagicMock(return_value="prompt")

        def _prompt(*a, **k):
            calls.append("prompt")
            return spy(*a, **k)

        with (
            patch.object(gates, "detect_gate_steps", side_effect=_detect) as detect,
            patch.object(gates, "run_gate_steps", return_value=None),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await _review(
                tmp_path,
                run_agent_effect=lambda *a, **k: (
                    calls.append("agent") or MagicMock(returncode=0)
                ),
                project=GATED,
                calls=calls,
                prompt_mock=prompt,
                run_agent_mock=agent,
            )
        assert calls.index("refresh") < calls.index("install")
        assert calls.index("install") < calls.index("agent")
        assert prompt[0].call_args.kwargs["gate_installed"] is True
        assert detect.call_args.kwargs["run_id"] == "review-install-item-t"
        assert "review_gate_install=installed" in caplog.text
        assert "review_gate_install_ms=" in caplog.text
        del _prompt

    async def test_no_steps(self, tmp_path, caplog) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=[]),
            patch.object(gates, "run_gate_steps") as run,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _o, _c, prompt, agent = await _launch(tmp_path)
        run.assert_not_called()
        assert prompt.call_args.kwargs["gate_installed"] is True
        assert "review_gate_install=no-steps" in caplog.text
        agent.assert_awaited_once()

    async def test_failure_still_launches(self, tmp_path, caplog) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", return_value=FAILURE),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _o, _c, prompt, agent = await _launch(tmp_path)
        agent.assert_awaited_once()
        assert prompt.call_args.kwargs["gate_installed"] is False
        assert (
            "rollout-loop: review-gate-install-failed rollout_id=ro-1 item_id=item-t"
        ) in caplog.text
        assert "detail=repo: pre-commit install: exit code 1" in caplog.text
        assert "review_gate_install=failed" in caplog.text

    async def test_no_gate_skips(self, tmp_path, caplog) -> None:
        with (
            patch.object(gates, "detect_gate_steps") as detect,
            patch.object(gates, "run_gate_steps") as run,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await _launch(tmp_path, project={"name": "P", "gate_command": ""})
        detect.assert_not_called()
        run.assert_not_called()
        assert "review_gate_install=skipped-no-gate" in caplog.text

    async def test_fix_and_conflict_modes_skip(self, tmp_path, caplog) -> None:
        with (
            patch.object(gates, "detect_gate_steps") as detect,
            patch.object(gates, "run_gate_steps") as run,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await _launch(tmp_path, gate_failure_output="boom")
            await _launch(tmp_path, land_conflict={"repo": ["x.py"]})
        detect.assert_not_called()
        run.assert_not_called()
        assert "review_gate_install=skipped-fix-mode" in caplog.text
        assert "review_gate_install=skipped-land-conflict" in caplog.text

    async def test_exception_still_launches(self, tmp_path, caplog) -> None:
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _o, _c, prompt, agent = await _launch(tmp_path)
        agent.assert_awaited_once()
        assert prompt.call_args.kwargs["gate_installed"] is False
        assert "detail=exception: boom" in caplog.text

    async def test_cancellation_propagates(self, tmp_path) -> None:
        agent: list[AsyncMock] = []
        with (
            patch.object(gates, "detect_gate_steps", return_value=["s"]),
            patch.object(gates, "run_gate_steps", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await _review(
                tmp_path,
                run_agent_effect=MagicMock(returncode=0),
                project=GATED,
                run_agent_mock=agent,
            )
        agent[0].assert_not_awaited()
