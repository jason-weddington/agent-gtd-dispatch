"""Reviewer prompt: skip the merge bar after a passed post-run gate."""

from pathlib import Path

from agent_gtd_dispatch import dispatch
from agent_gtd_dispatch.main import _gate_result_summary

ITEM = {"id": "item-a", "title": "t", "acceptance_criteria": ["works"]}
PROJECT = {"name": "p", "gate_command": "make check"}


def _render(gate_result: str) -> str:
    return dispatch.build_review_prompt(
        ITEM,
        PROJECT,
        "rollout-abc",
        "feat/item-a-x",
        40,
        Path("/tmp/ws"),  # noqa: S108 — prompt text only
        repo_dirs=["repo-a"],
        default_branches={"repo-a": "main"},
        workspace_mode=False,
        run_status="success",
        gate_result=gate_result,
        merge_notes=[],
        redispatches_used=0,
        redispatch_cap=1,
    )


def test_passed_run_row_renders_the_pass_wording_in_both_places() -> None:
    summary = _gate_result_summary(
        {"status": "success", "error_msg": ""},
        gate_command="make check",
        build_engine="claude-code",
    )
    assert dispatch.GATE_PASSED_WORDING in summary
    prompt = _render(summary)
    # Once in the envelope's gate-result line, once in Step 4's condition.
    assert prompt.count(dispatch.GATE_PASSED_WORDING) == 2
    assert "do NOT run the merge bar yourself unless you make an inline change" in (
        " ".join(prompt.split())
    )


def test_unknown_or_failed_gate_is_not_reported_as_passed() -> None:
    cases = [
        _gate_result_summary({"status": "success", "error_msg": ""}),
        _gate_result_summary(
            {"status": "success", "error_msg": ""},
            gate_command="make check",
            build_engine="talos-sonnet",
        ),
        _gate_result_summary(
            {"status": "failed", "error_msg": "post-run gate failed: ruff"},
            gate_command="make check",
        ),
    ]
    for summary in cases:
        assert dispatch.GATE_PASSED_WORDING not in summary


def test_long_verification_rule_present() -> None:
    flat = " ".join(_render("x").split())
    assert "No long-running verification" in flat
    assert "cargo mutants" in flat
    assert "more than 5 minutes" in flat
    assert "return `halt` naming that criterion" in flat
    assert "Claude" not in flat.split("## Step 4")[1].split("## Step 5")[0]


def test_dispatch_side_succeeded_literal_is_not_a_board_pass() -> None:
    """Board run rows say "success"; the dispatch-side "succeeded" never reaches
    the reviewer envelope, so it must not be read as a pass."""
    summary = _gate_result_summary(
        {"status": "succeeded", "error_msg": ""},
        gate_command="make gate",
        build_engine="claude-code",
    )
    assert dispatch.GATE_PASSED_WORDING not in summary
