"""Reviewer prompt: skip the merge bar after a passed post-run gate."""

from pathlib import Path

from agent_gtd_dispatch import dispatch
from agent_gtd_dispatch.main import _gate_result_summary

ITEM = {"id": "item-a", "title": "t", "acceptance_criteria": ["works"]}
PROJECT = {"name": "p", "gate_command": "make check"}


def _render(
    gate_result: str,
    *,
    gate_command: str = "make check",
    gate_installed: bool = False,
    review_timeout_seconds: int | None = None,
    gate_failure_output: str | None = None,
) -> str:
    return dispatch.build_review_prompt(
        ITEM,
        {"name": "p", "gate_command": gate_command},
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
        gate_installed=gate_installed,
        review_timeout_seconds=review_timeout_seconds,
        gate_failure_output=gate_failure_output,
    )


def _flat(prompt: str) -> str:
    return " ".join(prompt.split())


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


EXEMPT = "The one exception is the stored `gate_command`"


def test_exemption_present_with_gate() -> None:
    flat = _flat(_render("x"))
    assert (
        "than 5 minutes. The one exception is the stored `gate_command` and the "
        "squash commit: both are exempt from this 5-minute limit." in flat
    )
    assert "{gate_exemption}" not in flat


def test_exemption_absent_without_gate() -> None:
    flat = _flat(_render("x", gate_command=""))
    assert EXEMPT not in flat
    assert "No long-running verification" in flat
    assert "more than 5 minutes" in flat
    assert "{gate_exemption}" not in flat


def test_run_the_gate_paragraph_uses_review_timeout() -> None:
    assert "parameter set to 2700000" in _flat(
        _render("x", review_timeout_seconds=2700)
    )
    assert "parameter set to 3600000" in _flat(
        _render("x", review_timeout_seconds=3600)
    )
    flat = _flat(_render("x", gate_command="", review_timeout_seconds=2700))
    assert "Run the gate as ONE foreground Bash call" not in flat


def test_fix_mode_step_three_is_foreground_with_timeout() -> None:
    flat = _flat(_render("x", gate_failure_output="boom", review_timeout_seconds=2700))
    step3 = flat.split("3. Re-run the gate yourself")[1]
    assert "parameter set to 2700000" in step3
    assert "exempt from the 5-minute limit" in step3
    assert EXEMPT not in flat


def test_gate_installed_sentences() -> None:
    yes = "has already run the gate install step"
    no = "could not run the gate install step in your clone"
    assert yes in _flat(_render("x", gate_installed=True))
    assert no not in _flat(_render("x", gate_installed=True))
    assert no in _flat(_render("x", gate_installed=False))
    assert yes not in _flat(_render("x", gate_installed=False))
    for flag in (True, False):
        flat = _flat(_render("x", gate_command="", gate_installed=flag))
        assert yes not in flat
        assert no not in flat
    fix = _flat(_render("x", gate_failure_output="boom", gate_installed=True))
    assert yes not in fix


def test_engine_agnostic_copy_in_step_four() -> None:
    for flag in (True, False):
        step4 = _flat(_render("x", gate_installed=flag)).split("## Step 4")[1]
        assert "Claude" not in step4.split("## Step 5")[0]
    fix = _flat(_render("x", gate_failure_output="boom"))
    assert "Claude" not in fix
