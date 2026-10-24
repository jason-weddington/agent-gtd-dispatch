"""Shared standard-rules section in every prompt, and the stray-steering host checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agent_gtd_dispatch_protocol.models import DispatchMode

from agent_gtd_dispatch import dispatch

HEADING = "## Standard rules for headless runs"
ROOT = Path(__file__).parent.parent
ITEM = {"id": "item-1", "title": "T", "description": "D"}
PROJECT = {"id": "p1", "name": "proj", "git_origin": "git@x:r.git"}
REPO_DIRS = ["a", "b"]


def _prompts(engine_name: str | None = None) -> dict[str, str]:
    kw = {"engine_name": engine_name}
    return {
        "build": dispatch._build_build_prompt(
            ITEM, PROJECT, "feat/x", 10, workspace=Path("."), **kw
        ),
        "plan": dispatch._build_plan_prompt(ITEM, PROJECT, 10, **kw),
        "manage": dispatch._build_manage_prompt("r1", PROJECT, 10, **kw),
        "manage_ws": dispatch._build_manage_workspace_main_prompt(
            "r1", PROJECT, 10, REPO_DIRS, **kw
        ),
        "review": dispatch.build_review_prompt(
            ITEM, PROJECT, "r1", "feat/x", 10, Path("."), **kw
        ),
    }


class TestSharedSection:
    @pytest.mark.parametrize("mode", ["build", "plan", "manage", "manage_ws", "review"])
    def test_heading_exactly_once(self, mode: str) -> None:
        assert _prompts()[mode].count(HEADING) == 1

    def test_system_prompt_router_includes_it(self) -> None:
        for m in (DispatchMode.BUILD, DispatchMode.PLAN, DispatchMode.MANAGE):
            p = dispatch.build_system_prompt(
                ITEM, PROJECT, "feat/x", 10, mode=m, rollout_id="r1"
            )
            assert p.count(HEADING) == 1

    def test_one_phrase_per_rule(self) -> None:
        s = dispatch._standard_headless_rules()
        assert s.startswith(HEADING)
        for phrase in (
            "Never end your turn while anything you started is still running",
            "cannot read service credentials",
            "cannot install software or system packages",
            "commits on the remote branch",
            "Never push to or commit on the default branch",
        ):
            assert phrase in s

    def test_engine_agnostic_copy(self) -> None:
        for engine in (None, "talos-sonnet"):
            s = dispatch._standard_headless_rules(engine)
            for banned in ("Claude", "claude-code", "talos", "Opus", "Sonnet"):
                assert banned not in s

    def test_no_commit_rule_only_for_talos(self) -> None:
        marker = "Never run `git commit`"
        assert marker in dispatch._standard_headless_rules("talos-sonnet")
        assert marker not in dispatch._standard_headless_rules("claude-code")
        assert marker not in dispatch._standard_headless_rules(None)
        assert marker in _prompts("talos-opus")["build"]
        assert marker not in _prompts("claude-code")["build"]


class TestStrayUserSteeringChecks:
    @pytest.mark.parametrize("name", ["deploy.sh", "setup-dispatch-host.sh"])
    def test_script_checks(self, name: str) -> None:
        path = ROOT / name
        assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
        text = path.read_text()
        assert "stray user-level steering" in text
        for needle in (".claude/CLAUDE.md", ".claude/CLAUDE.local.md", ".claude/rules"):
            assert needle in text
        assert not [
            ln
            for ln in text.splitlines()
            if "_stray" in ln
            and "settings.json" in ln
            and not ln.lstrip().startswith("#")
        ]

    def test_deploy_refuses_before_install(self) -> None:
        text = (ROOT / "deploy.sh").read_text()
        assert text.index("stray user-level steering") < text.index(
            "uv tool install --force --refresh"
        )
        assert "[ERR]  stray user-level steering" in text
