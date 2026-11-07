"""The review prompt carries the build host's gate failure summary via the board."""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, db, dispatch, main
from agent_gtd_dispatch.dispatch import (
    GATE_SUMMARY_UNAVAILABLE,
    backtick_fence,
    extract_gate_summary,
    gate_summary_marker,
)
from agent_gtd_dispatch.engines import CLAUDE

HEADING = "\nPost-run gate failure summary (from the build host)\n"
LOG = "agent_gtd_dispatch.main"


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


def _comment(run_id: str, block: str, fence: str = "````") -> dict[str, Any]:
    return {
        "content_markdown": (
            f"Post-run gate failed (run `{run_id}`)\n\n"
            f"{gate_summary_marker(run_id)}\nGate output (failure lines and tail):\n\n"
            f"{fence}\n{block}\n{fence}"
        )
    }


def _row(error_msg: str, remote: str = "run-xyz") -> dict[str, Any]:
    return {"status": "failed", "error_msg": error_msg, "remote_run_id": remote}


def _prompt(summary: str | None) -> str:
    return dispatch.build_review_prompt(
        {"id": "i", "title": "T"},
        {"name": "P"},
        "ro-1",
        "feat/x",
        10,
        config.WORKSPACE_ROOT,
        post_run_gate_summary=summary,
    )


async def _summary(
    row: dict[str, Any],
    comments: Any,
    *,
    normal_launch: bool = True,
) -> tuple[str | None, str, AsyncMock]:
    lc = (
        AsyncMock(side_effect=comments)
        if isinstance(comments, Exception)
        else AsyncMock(return_value=comments)
    )
    get_run = AsyncMock()
    with (
        patch("agent_gtd_dispatch.main.gtd_client") as gtd,
        patch.object(main.db, "get_run", get_run),
    ):
        gtd.list_comments = lc
        s, src = await main._gate_summary_for_review(
            "item-t", row, token=None, normal_launch=normal_launch, rollout_id="ro-1"
        )
    get_run.assert_not_awaited()
    return s, src, lc


class TestHelpers:
    def test_marker_and_fence(self) -> None:
        assert gate_summary_marker("r1") == "<!-- gate-summary run=r1 -->"
        assert backtick_fence("x", 4) == "````"
        assert backtick_fence("a\n`````\nb", 4) == "``````"
        assert backtick_fence("a\n````\nb", 3) == "`````"

    def test_truncation_100_lines(self) -> None:
        block = "\n".join(f"line{i}" for i in range(1, 101))
        r = extract_gate_summary([_comment("r", block)], "r")
        assert r.reason == "found"
        assert r.omitted_lines == 40
        assert r.text is not None
        lines = r.text.split("\n")
        assert len(lines) == 61
        assert lines[19] == "line20"
        assert lines[20] == (
            "... (40 more lines omitted; full output is in the build's item comment)"
        )
        assert lines[21] == "line61"
        assert lines[-1] == "line100"

    def test_60_lines_whole(self) -> None:
        block = "\n".join(f"l{i}" for i in range(60))
        r = extract_gate_summary([_comment("r", block)], "r")
        assert r.text is not None
        assert len(r.text.split("\n")) == 60
        assert r.omitted_lines == 0

    def test_tail_failure_kept(self) -> None:
        block = "\n".join([*(f"l{i}" for i in range(99)), "FAILED x"])
        r = extract_gate_summary([_comment("r", block)], "r")
        assert r.text is not None
        assert "FAILED x" in r.text

    def test_newest_wins(self) -> None:
        r = extract_gate_summary([_comment("r", "old"), _comment("r", "new")], "r")
        assert r.text == "new"

    def test_full_log_rewritten(self) -> None:
        r = extract_gate_summary(
            [_comment("r", "Full gate log: /x/y.log\nFAILED a")], "r"
        )
        assert r.text is not None
        assert r.text.split("\n")[0] == (
            "Full gate log (on the build host; not readable from the reviewer's"
            " host): /x/y.log"
        )

    @pytest.mark.parametrize(
        ("comments", "rid", "reason"),
        [
            ([], "", "no_remote_run_id"),
            ([{"content_markdown": "hi"}], "r", "no_marker"),
            ([_comment("run-xy", "a")], "run-xyz", "marker_other_run"),
            ([_comment("r", "(no output)")], "r", "empty_block"),
            (
                [{"content_markdown": gate_summary_marker("r") + "\n````\nabc"}],
                "r",
                "fence_unterminated",
            ),
        ],
    )
    def test_reasons(
        self, comments: list[dict[str, Any]], rid: str, reason: str
    ) -> None:
        r = extract_gate_summary(comments, rid)
        assert r.reason == reason
        assert r.text is None

    def test_prompt_fence_five_backticks(self) -> None:
        p = _prompt("a\n````\nb")
        assert "\n        `````text\n" in p or "\n`````text\n" in p
        assert HEADING in p

    def test_prompt_none_has_no_summary(self) -> None:
        p = _prompt(None)
        assert "Post-run gate failure summary" not in p
        assert GATE_SUMMARY_UNAVAILABLE not in p

    def test_prompt_sentinel(self) -> None:
        p = _prompt(GATE_SUMMARY_UNAVAILABLE)
        assert "\n" + GATE_SUMMARY_UNAVAILABLE + "\n" in p
        assert "Post-run gate failure summary" not in p


class TestGateSummaryForReview:
    async def test_found(self, caplog) -> None:
        c = [_comment("run-xyz", "FAILED e2e/artifacts.spec.ts:39 e2e-coverage")]
        with caplog.at_level(logging.INFO, logger=LOG):
            s, src, _ = await _summary(_row("post-run gate failed: exit 1"), c)
        assert src == "comment"
        assert s is not None
        assert "e2e-coverage" in s
        assert "review-gate-summary " in caplog.text
        assert "reason=found" in caplog.text
        assert "failure_lines=1" in caplog.text
        p = _prompt(s)
        assert HEADING in p
        assert "\n        Post-run gate failure summary" not in p

    async def test_timeout_message_applicable(self) -> None:
        c = [_comment("run-xyz", "FAILED t")]
        s, src, _ = await _summary(_row("post-run gate timed out after 600s"), c)
        assert src == "comment"
        assert s == "FAILED t"

    async def test_no_comment(self) -> None:
        s, src, _ = await _summary(_row("post-run gate failed: exit 1"), [])
        assert (s, src) == (GATE_SUMMARY_UNAVAILABLE, "unavailable")
        p = _prompt(s)
        assert "\n" + GATE_SUMMARY_UNAVAILABLE + "\n" in p
        assert HEADING not in p

    async def test_marker_mismatch(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=LOG):
            s, src, _ = await _summary(
                _row("post-run gate failed: exit 1"), [_comment("run-xy", "a")]
            )
        assert src == "unavailable"
        assert s == GATE_SUMMARY_UNAVAILABLE
        assert "marker_run_ids=run-xy" in caplog.text
        assert "reason=marker_other_run" in caplog.text

    async def test_list_error(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=LOG):
            s, src, _ = await _summary(
                _row("post-run gate failed: exit 1"), RuntimeError("boom")
            )
        assert (s, src) == (GATE_SUMMARY_UNAVAILABLE, "unavailable")
        assert "reason=list_error" in caplog.text

    async def test_no_remote_run_id(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=LOG):
            s, _src, _ = await _summary(_row("post-run gate failed: exit 1", ""), [])
        assert s == GATE_SUMMARY_UNAVAILABLE
        assert "review-gate-summary-no-remote-run-id" in caplog.text

    @pytest.mark.parametrize(
        ("msg", "normal", "reason"),
        [
            ("", True, "not_applicable_passed"),
            ("agent exited 1", True, "not_applicable_passed"),
            ("post-run gate failed: exit 1", False, "not_applicable"),
        ],
    )
    async def test_not_applicable(self, caplog, msg, normal, reason) -> None:
        with caplog.at_level(logging.INFO, logger=LOG):
            s, src, lc = await _summary(_row(msg), [], normal_launch=normal)
        assert (s, src) == (None, "not-applicable")
        lc.assert_not_awaited()
        assert reason in caplog.text
        p = _prompt(s)
        assert GATE_SUMMARY_UNAVAILABLE not in p
        assert "Post-run gate failure summary" not in p

    async def test_empty_and_unterminated(self, caplog) -> None:
        row = _row("post-run gate failed: exit 1")
        with caplog.at_level(logging.INFO, logger=LOG):
            s, _, _ = await _summary(row, [_comment("run-xyz", "(no output)")])
            assert s == GATE_SUMMARY_UNAVAILABLE
            assert "reason=empty_block" in caplog.text
            bad = {"content_markdown": gate_summary_marker("run-xyz") + "\n````\nx"}
            s, _, _ = await _summary(row, [bad])
            assert s == GATE_SUMMARY_UNAVAILABLE
            assert "reason=fence_unterminated" in caplog.text

    async def test_no_failure_lines_warning(self, caplog) -> None:
        block = "\n".join(f"l{i}" for i in range(100))
        with caplog.at_level(logging.INFO, logger=LOG):
            await _summary(
                _row("post-run gate failed: exit 1"), [_comment("run-xyz", block)]
            )
        assert "omitted=40" in caplog.text
        assert "review-gate-summary-no-failure-lines" in caplog.text


async def test_launch_reviewer_threads_summary(tmp_path) -> None:
    await db.init_db()
    (tmp_path / "transcript.txt").write_text("x\n")
    workspace = SimpleNamespace(
        root=tmp_path,
        repo_paths={"repo": tmp_path},
        default_branches={"repo": "main"},
        workspace_mode=False,
    )
    spy = MagicMock(wraps=dispatch.build_review_prompt)
    comments = [_comment("run-xyz", "FAILED e2e/artifacts.spec.ts:39 e2e-coverage")]
    with ExitStack() as stack:
        gtd = stack.enter_context(patch("agent_gtd_dispatch.main.gtd_client"))
        gtd.get_item = AsyncMock(
            return_value={"id": "item-t", "title": "X", "commit_type": "fix"}
        )
        gtd.list_runs_for_item = AsyncMock(
            return_value=[
                {
                    "rollout_id": "ro-1",
                    "status": "failed",
                    "error_msg": "post-run gate failed: exit 1",
                    "remote_run_id": "run-xyz",
                }
            ]
        )
        gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
        gtd.list_comments = AsyncMock(return_value=comments)
        for name, value in {
            "build_review_prompt": spy,
            "compose_squash_subject": MagicMock(return_value=None),
            "clear_review_verdict": MagicMock(),
            "run_agent": AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1)
            ),
            "read_review_verdict": MagicMock(return_value=(None, "absent")),
            "reap_marker_processes": MagicMock(
                return_value=dispatch.MarkerReap(
                    reaped=[], survivors=0, scan_ok=True, escaped_session=0
                )
            ),
            "reap_workspace_processes": MagicMock(return_value=[]),
            "refresh_feature_branch_refs": MagicMock(return_value={}),
        }.items():
            stack.enter_context(patch.object(dispatch, name, value))
        await main._launch_reviewer(
            "ro-1",
            "item-t",
            project={"name": "P"},
            workspace=workspace,  # type: ignore[arg-type]
            reviewer_engine=CLAUDE,
            token=None,
            attribution=None,
        )
    kwargs = spy.call_args.kwargs
    assert "e2e-coverage" in kwargs["post_run_gate_summary"]
    rendered = _prompt(kwargs["post_run_gate_summary"])
    assert HEADING in rendered
