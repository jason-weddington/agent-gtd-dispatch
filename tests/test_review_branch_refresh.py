"""The worker force-refreshes feature-branch refs before every review launch.

Regression for rollout aed487a7: a lead fast-forwarded the item's feature
branch on origin, and the reviewer kept squashing the STALE
``refs/remotes/origin/<branch>`` because a rollout clone is single-branch and
a plain ``git fetch origin <branch>`` only writes FETCH_HEAD.  These tests
drive :func:`agent_gtd_dispatch.dispatch.refresh_feature_branch_refs` against
real git repos (bare origin + single-branch clone), assert the reviewer prompt
itself uses the forced refspec and squashes the LOCAL branch, and pin the
worker call-site contract.  They never touch host state: git repos live in
``tmp_path`` and ``config.AGENT_SUBPROCESS_USER`` is cleared so every git call
runs as the test user.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, dispatch, main
from agent_gtd_dispatch.main import ReviewVerdict

if TYPE_CHECKING:
    from collections.abc import Iterator

BRANCH = "feat/aaaaaaaa-x"

_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *_GIT_ID, *args], cwd=repo, capture_output=True, check=True, text=True
    )
    return proc.stdout.strip()


def _git_ok(repo: Path, *args: str) -> int:
    proc = subprocess.run(
        ["git", *_GIT_ID, *args], cwd=repo, capture_output=True, check=False
    )
    return proc.returncode


def _ref_sha(repo: Path, ref: str) -> str:
    """The sha *ref* resolves to, "" when it does not exist."""
    if _git_ok(repo, "rev-parse", "--verify", "--quiet", ref) != 0:
        return ""
    return _git(repo, "rev-parse", ref)


def _bare_origin(tmp_path: Path, *, feature: bool = True) -> tuple[Path, Path]:
    """A bare origin with ``main`` at commit ``A0`` and, optionally, *BRANCH* at ``A``.

    Returns (origin, seed working clone).
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    if feature:
        _git(seed, "checkout", "-q", "-b", BRANCH)
        (seed / "feature.txt").write_text("a\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "feat: A")
        _git(seed, "push", "-q", "origin", BRANCH)
        _git(seed, "checkout", "-q", "main")
    return origin, seed


def _single_branch_clone(origin: Path, dest: Path) -> Path:
    """Clone the way the rollout workspace does (shallow, default branch only)."""
    subprocess.run(
        ["git", "clone", "--depth=50", f"file://{origin}", str(dest)],
        check=True,
        capture_output=True,
    )
    _git(dest, "config", "user.name", "t")
    _git(dest, "config", "user.email", "t@t")
    return dest


def _make_stale(clone: Path, a_sha: str) -> None:
    """Leave the pre-fix shape: tracking ref at A plus a stale local branch at A."""
    assert dispatch.land_fetch_feature_branch(clone, BRANCH, 30) is True
    assert _ref_sha(clone, f"refs/remotes/origin/{BRANCH}") == a_sha
    _git(clone, "branch", BRANCH, a_sha)


def _ff_origin_feature(origin: Path, seed: Path) -> str:
    """Fast-forward origin's feature branch from A to B; return B."""
    _git(seed, "checkout", "-q", BRANCH)
    (seed / "feature.txt").write_text("b\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "feat: B (lead's conflict resolution)")
    b_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", BRANCH)
    _git(seed, "checkout", "-q", "main")
    return b_sha


def _ws(clone: Path, name: str = "repo") -> dispatch.RolloutWorkspace:
    return dispatch.RolloutWorkspace(
        root=clone,
        repo_paths={name: clone},
        default_branches={name: "main"},
        created=False,
        workspace_mode=False,
    )


def _refresh_line(messages: list[str], *, marker: str) -> str:
    matches = [
        m
        for m in messages
        if m.startswith("rollout-loop: review-branch-refresh repo=") and marker in m
    ]
    assert matches, f"no {marker!r} line in: {messages}"
    return matches[0]


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        config.load()
        yield


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")


class TestRefreshFeatureBranchRefsRegression:
    """AC-9: single-branch clone, tracking ref stale after origin fast-forward."""

    def test_stale_tracking_ref_is_refreshed_and_stale_local_branch_deleted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        origin, seed = _bare_origin(tmp_path / "o1")
        a_sha = _git(seed, "rev-parse", BRANCH)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        assert _git(clone, "config", "remote.origin.fetch") == (
            "+refs/heads/main:refs/remotes/origin/main"
        )
        _make_stale(clone, a_sha)
        b_sha = _ff_origin_feature(origin, seed)

        # Pre-fix shape: a plain fetch writes only FETCH_HEAD and leaves the
        # tracking ref at the stale sha — the exact bug this helper fixes.
        subprocess.run(
            ["git", "fetch", "origin", BRANCH],
            cwd=clone,
            check=True,
            capture_output=True,
        )
        assert _ref_sha(clone, f"refs/remotes/origin/{BRANCH}") == a_sha
        assert _git(clone, "rev-parse", "FETCH_HEAD") == b_sha

        main_sha = _git(clone, "rev-parse", "refs/heads/main")
        origin_main = _git(clone, "rev-parse", "refs/remotes/origin/main")

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            result = dispatch.refresh_feature_branch_refs(_ws(clone), BRANCH, 30)

        assert result == {"repo": "refreshed"}
        assert _ref_sha(clone, f"refs/remotes/origin/{BRANCH}") == b_sha
        assert _ref_sha(clone, f"refs/heads/{BRANCH}") == ""
        # The default branch ref and its commit are untouched.
        assert _git(clone, "rev-parse", "refs/heads/main") == main_sha
        assert _git(clone, "rev-parse", "refs/remotes/origin/main") == origin_main
        messages = [r.getMessage() for r in caplog.records]
        line = _refresh_line(messages, marker="stale_corrected=true")
        assert "result=refreshed" in line
        assert f"tracking_before={a_sha}" in line
        assert f"tracking_after={b_sha}" in line

    def test_second_refresh_is_a_no_op(self, tmp_path: Path, caplog) -> None:
        origin, seed = _bare_origin(tmp_path / "o2")
        a_sha = _git(seed, "rev-parse", BRANCH)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        _make_stale(clone, a_sha)
        _ff_origin_feature(origin, seed)
        ws = _ws(clone)

        dispatch.refresh_feature_branch_refs(ws, BRANCH, 30)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            result = dispatch.refresh_feature_branch_refs(ws, BRANCH, 30)
        assert result == {"repo": "refreshed"}
        line = _refresh_line(
            [r.getMessage() for r in caplog.records], marker="stale_corrected=false"
        )
        assert "result=refreshed" in line


class TestRefreshFeatureBranchRefsEdgeCases:
    """AC-10: absent branch, dead remote, multi-repo, bad dir, protected names."""

    def test_absent_branch_deletes_stale_refs(self, tmp_path: Path, caplog) -> None:
        origin, seed = _bare_origin(tmp_path / "o1")
        a_sha = _git(seed, "rev-parse", BRANCH)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        _make_stale(clone, a_sha)
        _git(origin, "branch", "-D", BRANCH)  # lead deleted the branch on origin

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            result = dispatch.refresh_feature_branch_refs(_ws(clone), BRANCH, 30)

        assert result == {"repo": "absent"}
        assert _ref_sha(clone, f"refs/remotes/origin/{BRANCH}") == ""
        assert _ref_sha(clone, f"refs/heads/{BRANCH}") == ""
        line = _refresh_line(
            [r.getMessage() for r in caplog.records], marker="result=absent"
        )
        # The tracking ref was deleted, so there is nothing after: not "corrected".
        assert "tracking_after=none" in line
        assert "stale_corrected=false" in line

    def test_unreachable_origin_reports_fetch_failed_and_cleans_stale_refs(
        self, tmp_path: Path, caplog
    ) -> None:
        origin, seed = _bare_origin(tmp_path / "o1")
        a_sha = _git(seed, "rev-parse", BRANCH)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        _make_stale(clone, a_sha)
        _git(clone, "remote", "set-url", "origin", str(tmp_path / "no-such-origin"))

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            result = dispatch.refresh_feature_branch_refs(_ws(clone), BRANCH, 30)

        assert result == {"repo": "fetch_failed"}
        assert _ref_sha(clone, f"refs/remotes/origin/{BRANCH}") == ""
        assert _ref_sha(clone, f"refs/heads/{BRANCH}") == ""
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "review-branch-refresh" in r.getMessage()
        ]
        assert warnings
        message = " ".join(r.getMessage() for r in warnings)
        assert "result=fetch_failed" in message

    def test_two_repos_both_reported(self, tmp_path: Path) -> None:
        # Repo one: dead remote, stale refs.  Repo two: refreshed after FF.
        o1, s1 = _bare_origin(tmp_path / "r1")
        a1 = _git(s1, "rev-parse", BRANCH)
        clone1 = _single_branch_clone(o1, tmp_path / "clone1")
        _make_stale(clone1, a1)
        _git(clone1, "remote", "set-url", "origin", str(tmp_path / "dead"))

        o2, s2 = _bare_origin(tmp_path / "r2")
        a2 = _git(s2, "rev-parse", BRANCH)
        clone2 = _single_branch_clone(o2, tmp_path / "clone2")
        _make_stale(clone2, a2)
        _ff_origin_feature(o2, s2)

        ws = dispatch.RolloutWorkspace(
            root=tmp_path,
            repo_paths={"r1": clone1, "r2": clone2},
            default_branches={"r1": "main", "r2": "main"},
            created=False,
            workspace_mode=False,
        )
        result = dispatch.refresh_feature_branch_refs(ws, BRANCH, 30)
        assert result == {"r1": "fetch_failed", "r2": "refreshed"}
        assert _ref_sha(clone1, f"refs/remotes/origin/{BRANCH}") == ""
        assert _ref_sha(clone1, f"refs/heads/{BRANCH}") == ""
        assert _ref_sha(clone2, f"refs/remotes/origin/{BRANCH}") == _git(
            s2, "rev-parse", BRANCH
        )
        assert _ref_sha(clone2, f"refs/heads/{BRANCH}") == ""

    def test_missing_repo_directory_is_fetch_failed(self, tmp_path: Path) -> None:
        origin, seed = _bare_origin(tmp_path / "o1")
        a_sha = _git(seed, "rev-parse", BRANCH)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        _make_stale(clone, a_sha)
        ws = dispatch.RolloutWorkspace(
            root=tmp_path,
            repo_paths={"missing": tmp_path / "no-such-dir", "repo": clone},
            default_branches={"missing": "main", "repo": "main"},
            created=False,
            workspace_mode=False,
        )
        result = dispatch.refresh_feature_branch_refs(ws, BRANCH, 30)
        assert result["missing"] == "fetch_failed"
        assert result["repo"] == "refreshed"

    @pytest.mark.parametrize("protected", ["main", ""])
    def test_protected_branch_names_skip(
        self, tmp_path: Path, caplog, protected
    ) -> None:
        origin, _seed = _bare_origin(tmp_path / "o1", feature=False)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        main_sha = _git(clone, "rev-parse", "refs/heads/main")
        origin_main = _git(clone, "rev-parse", "refs/remotes/origin/main")

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.dispatch"):
            result = dispatch.refresh_feature_branch_refs(_ws(clone), protected, 30)

        assert result == {"repo": "skipped"}
        assert _git(clone, "rev-parse", "refs/heads/main") == main_sha
        assert _git(clone, "rev-parse", "refs/remotes/origin/main") == origin_main
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("rollout-loop: review-branch-refresh skipped")
            and "reason=protected" in m
            for m in messages
        )


class TestReviewPromptForcedRefspec:
    """AC-11 (prompt half): the rendered normal-mode prompt uses the forced refspec."""

    def test_normal_prompt_fetches_forced_refspec_and_squashes_local_branch(
        self,
    ) -> None:
        forced = f"git fetch origin +refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"
        prompt = dispatch.build_review_prompt(
            {
                "id": "bbbbbbbb-2222-3333-4444-555555555555",
                "title": "T",
                "acceptance_criteria": ["works"],
                "commit_type": "feat",
            },
            {"name": "P", "gate_command": "make check"},
            "ro-1",
            BRANCH,
            40,
            Path("/ws"),
            repo_dirs=["r"],
            default_branches={"r": "main"},
            workspace_mode=False,
            run_status="succeeded",
            gate_result="the post-run gate PASSED",
            merge_notes=[],
            engine_name="claude-code",
        )
        assert prompt.count(forced) == 2
        assert f"git checkout -B {BRANCH} origin/{BRANCH}" in prompt
        assert f"git merge --squash {BRANCH}" in prompt
        # No bare branch fetch that would only write FETCH_HEAD.
        assert f"git fetch origin {BRANCH}" not in prompt


class TestLandFeatureTipFallbackTripwire:
    """AC-8: the land's conflict guard logs which ref resolved the feature tip."""

    async def test_fallback_to_local_branch_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A feature branch that exists ONLY locally: the origin ref cannot
        # resolve, so the guard must take the refs/heads fallback and say so.
        origin, _seed = _bare_origin(tmp_path / "o1", feature=False)
        clone = _single_branch_clone(origin, tmp_path / "clone")
        (clone / "extra.txt").write_text("x\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "chore: second main commit")
        tip = _git(clone, "rev-parse", "HEAD")
        _git(clone, "branch", BRANCH, tip)

        verdict = ReviewVerdict(
            verdict="merge",
            rationale="ok",
            merge_note="n",
            detail="",
            branch_name=BRANCH,
            repo_states=(("repo", "committed"),),
        )
        lander = main._Lander(
            "ro-1",
            "item-1",
            verdict,
            project={"name": "P"},
            workspace=_ws(clone),
            pre_review_origin={},
            reviewer_engine=SimpleNamespace(name="claude-code"),
            loop_run_id="run-1",
            token=None,
            attribution=None,
        )
        lander.landing = ["repo"]

        async def _no_conflicts(repo: str, **_kw: Any) -> main._ConflictCheck:
            return main._ConflictCheck([], [], [], [], [])

        lander._check_repo_conflicts = _no_conflicts  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await lander._conflict_guard()

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            r.levelno == logging.WARNING
            and "rollout-loop: land-feature-tip-fallback" in r.getMessage()
            and f"branch={BRANCH}" in r.getMessage()
            for r in caplog.records
        )
        tip_lines = [m for m in messages if "rollout-loop: land-feature-tip repo=" in m]
        assert tip_lines
        assert any(
            f"tip_ref=refs/heads/{BRANCH}" in m and f"tip={tip}" in m for m in tip_lines
        )
