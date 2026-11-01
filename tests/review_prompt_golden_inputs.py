"""Fixed inputs for the review-prompt golden files (item 21106de8, AC-12).

The goldens under ``tests/fixtures/review_prompt_golden/`` were captured from
the code BEFORE the conflict-classification text was factored into a shared
helper; the equality tests re-render with exactly these inputs.
"""

from __future__ import annotations

from pathlib import Path

from agent_gtd_dispatch import dispatch

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "review_prompt_golden"

ITEM: dict[str, object] = {
    "id": "11111111-2222-3333-4444-555555555555",
    "title": "Golden item",
    "description": "A fixed item for the review prompt goldens.",
    "acceptance_criteria": ["AC-1: one", "AC-2: two"],
    "scope_out": ["nothing else"],
    "commit_type": "feat",
}
PROJECT: dict[str, object] = {"name": "golden-repo", "gate_command": "make check"}


def render_contract(workspace_mode: bool, has_gate: bool) -> str:
    return dispatch._review_conflict_contract_block(
        workspace_mode=workspace_mode, has_gate=has_gate
    )


def contract_name(workspace_mode: bool, has_gate: bool) -> str:
    w = "workspace" if workspace_mode else "mono"
    g = "gate" if has_gate else "nogate"
    return f"conflict_contract_{w}_{g}.txt"


def render_prompt(*, fix_mode: bool) -> str:
    return dispatch.build_review_prompt(
        ITEM,
        PROJECT,
        "rollout-golden",
        "feat/11111111-golden",
        40,
        Path("/golden/workspace"),
        repo_dirs=["golden-repo"],
        default_branches={"golden-repo": "main"},
        workspace_mode=False,
        run_status="succeeded",
        gate_result="the post-run gate PASSED",
        merge_notes=[],
        gate_failure_output="boom" if fix_mode else None,
        engine_name="claude-code",
    )


def prompt_name(*, fix_mode: bool) -> str:
    return "review_prompt_fix_mode.txt" if fix_mode else "review_prompt_squash.txt"


def golden_text(rendered: str) -> str:
    """The on-disk form of a rendering: the exact bytes plus one EOF newline.

    The repo's whitespace hook requires a trailing newline on every text file,
    so the file carries one framing newline that the rendering itself lacks.
    """
    return rendered + "\n"


def capture() -> None:  # pragma: no cover - run once against the pre-change code
    for w in (False, True):
        for g in (False, True):
            (GOLDEN_DIR / contract_name(w, g)).write_text(
                golden_text(render_contract(w, g))
            )
    for fix in (False, True):
        (GOLDEN_DIR / prompt_name(fix_mode=fix)).write_text(
            golden_text(render_prompt(fix_mode=fix))
        )


if __name__ == "__main__":  # pragma: no cover
    capture()
