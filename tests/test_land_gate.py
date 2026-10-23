"""The worker-owned land step: fetch, rebase onto current origin, gate, push.

Driven against real git repos with real bare origins (same shape as
``TestFindLandedCommitSubjects``); only the GTD client and, where a test says
so, the reviewer relaunch are faked.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_gtd_dispatch import config, dispatch, gates, main
from agent_gtd_dispatch.engines import get_engine
from agent_gtd_dispatch.main import ReviewVerdict

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")

ROLLOUT = "rollout-land-1"
ITEM = "996b308e-1111-2222-3333-444455556666"
ITEM_B = "aaaabbbb-1111-2222-3333-444455556666"
_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *_GIT_ID, *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    return proc.stdout.strip()


def _origin(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Return (bare origin, seed clone with a base commit pushed)."""
    origin = tmp_path / f"{name}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "base.txt").write_text("base\n")
    (seed / "f.txt").write_text("a\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    return origin, seed


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", str(origin), str(dest)], check=True, capture_output=True
    )
    _git(dest, "config", "user.name", "t")
    _git(dest, "config", "user.email", "t@t")
    return dest


def _commit_unrelated(seed: Path, name: str, *, push: bool = True) -> str:
    _git(seed, "pull", "-q", "--rebase", "origin", "main")
    (seed / name).write_text(name)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", f"chore: unrelated {name}")
    if push:
        _git(seed, "push", "-q", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def _squash(
    clone: Path,
    item_id: str = ITEM,
    *,
    filename: str = "feature.txt",
    content: str = "feature\n",
    subject: str = "fix(996b308e): Add X",
    trailer: bool = True,
    branch: str = "feat/996b308e-add-x",
) -> str:
    (clone / filename).write_text(content)
    _git(clone, "add", "-A")
    msg = subject
    if trailer:
        msg += f"\n\nRollout: {ROLLOUT}\nItem: {item_id}"
    _git(clone, "commit", "-m", msg)
    # The land's conflict guard needs the verdict's feature branch to exist;
    # pointing it at the squash commit makes the squash merge trivially clean.
    _git(clone, "branch", "-f", branch, "HEAD")
    return _git(clone, "rev-parse", "HEAD")


FEATURE = "feat/996b308e-add-x"
_MARKER_LINE = "<<<<<<< HEAD\n"


def _conflict_squash(
    clone: Path,
    files: dict[str, tuple[str, str, str]],
    item_id: str = ITEM,
) -> str:
    """Fabricate a reviewer's squash commit whose merge conflicted.

    *files* maps path -> (branch content, default-branch content, resolved
    content).  The default-branch edit is pushed to origin, so origin is the
    squash commit's parent.
    """
    _git(clone, "checkout", "-q", "-b", FEATURE)
    for name, (ours, _theirs, _resolved) in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(ours)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "feat: branch side")
    _git(clone, "checkout", "-q", "main")
    for name, (_ours, theirs, _resolved) in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(theirs)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "docs: default-branch side")
    _git(clone, "push", "-q", "origin", "main")
    merge = subprocess.run(
        ["git", *_GIT_ID, "merge", "--squash", FEATURE],
        cwd=clone,
        capture_output=True,
        check=False,
    )
    assert merge.returncode != 0
    for name, (_ours, _theirs, resolved) in files.items():
        (clone / name).write_text(resolved)
        _git(clone, "add", name)
    _git(
        clone,
        "commit",
        "-m",
        f"fix(996b308e): Add X\n\nRollout: {ROLLOUT}\nItem: {item_id}",
    )
    tree = subprocess.run(
        ["git", "merge-tree", "--write-tree", "main~1", FEATURE],
        cwd=clone,
        capture_output=True,
        check=False,
    )
    assert tree.returncode == 1
    return _git(clone, "rev-parse", "HEAD")


_README = ("branch line\n", "main line\n", "branch line\nmain line\n")
_APP = ("x = 1\n", "x = 2\n", "x = 3\n")


def _tip(origin: Path) -> str:
    return _git(origin, "rev-parse", "main")


def _mono_ws(clone: Path) -> dispatch.RolloutWorkspace:
    return dispatch.RolloutWorkspace(
        root=clone,
        repo_paths={clone.name: clone},
        default_branches={clone.name: "main"},
        created=False,
        workspace_mode=False,
    )


def _pre(ws: dispatch.RolloutWorkspace) -> dict[str, str]:
    return dispatch.land_origin_shas(dict(ws.repo_paths), dict(ws.default_branches), 30)


def _verdict(**kw) -> ReviewVerdict:
    base = {
        "verdict": "merge",
        "rationale": "looks right",
        "merge_note": "original note",
        "detail": "",
        "commit_type": "fix",
        "commit_type_reason": "groomed on the item",
        "item_commit_type": "fix",
        "branch_name": "feat/996b308e-add-x",
    }
    base.update(kw)
    return ReviewVerdict(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
    monkeypatch.setattr(main, "_land_locks", {})
    gtd = MagicMock()
    gtd.update_rollout_state = AsyncMock()
    gtd.post_comment = AsyncMock()
    gtd.halt_rollout = AsyncMock()
    gtd.complete_in_rollout = AsyncMock(return_value={"newly_ready": []})
    gtd.get_item = AsyncMock(return_value={"id": "item", "title": "t"})
    monkeypatch.setattr(main, "gtd_client", gtd)
    return gtd


@pytest.fixture
def gtd(_env):
    return _env


async def _land(
    ws: dispatch.RolloutWorkspace,
    pre: dict[str, str],
    *,
    gate: str | None = "true",
    outcome: ReviewVerdict | None = None,
    item_id: str = ITEM,
    loop_run_id: str = "loop-1",
):
    project = {"name": "P", "git_origin": "git@h:o/repo.git"}
    if gate is not None:
        project["gate_command"] = gate
    return await main._land_reviewed_merge(
        ROLLOUT,
        item_id,
        outcome or _verdict(),
        project=project,
        workspace=ws,
        pre_review_origin=pre,
        reviewer_engine=get_engine("claude-code-sonnet"),
        loop_run_id=loop_run_id,
        token=None,
        attribution=None,
    )


def _halt_reason(gtd) -> str:
    gtd.halt_rollout.assert_awaited_once()
    return gtd.halt_rollout.await_args.kwargs["reason"]


def _land_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("rollout-loop: land rollout_id=")
    ]


@pytest.fixture
def mono(tmp_path: Path):
    """A monorepo rollout workspace with origin moved by one commit post-review."""
    origin, seed = _origin(tmp_path, "repo")
    clone = _clone(origin, tmp_path / "repo")
    ws = _mono_ws(clone)
    return SimpleNamespace(origin=origin, seed=seed, clone=clone, ws=ws, pre=_pre(ws))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_values(self) -> None:
        assert main.MAX_LAND_PUSH_ATTEMPTS == 3
        assert main.MAX_LAND_FIX_RELAUNCHES == 1
        assert main.LAND_HEARTBEAT_SECONDS == 300

    def test_config_defaults_and_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_API_KEY", "k")
        monkeypatch.setenv("AGENT_GTD_URL", "http://x")
        monkeypatch.setenv("AGENT_GTD_API_KEY", "k")
        monkeypatch.delenv("DISPATCH_LAND_GATE_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("DISPATCH_LAND_GIT_TIMEOUT_SECONDS", raising=False)
        config.load()
        assert config.LAND_GATE_TIMEOUT_SECONDS == 1800
        assert config.LAND_GIT_TIMEOUT_SECONDS == 120
        monkeypatch.setenv("DISPATCH_LAND_GATE_TIMEOUT_SECONDS", "77")
        monkeypatch.setenv("DISPATCH_LAND_GIT_TIMEOUT_SECONDS", "9")
        config.load()
        assert config.LAND_GATE_TIMEOUT_SECONDS == 77
        assert config.LAND_GIT_TIMEOUT_SECONDS == 9
        monkeypatch.delenv("DISPATCH_LAND_GATE_TIMEOUT_SECONDS")
        monkeypatch.delenv("DISPATCH_LAND_GIT_TIMEOUT_SECONDS")
        config.load()


class TestHelpers:
    def test_normalize_remote_url(self) -> None:
        assert dispatch.normalize_remote_url("git@h:o/r.git/") == "git@h:o/r"
        assert dispatch.normalize_remote_url("git@h:o/r.git") == "git@h:o/r"
        assert dispatch.normalize_remote_url("/srv/r/") == "/srv/r"

    def test_land_git_timeout_raises(self, tmp_path: Path) -> None:
        with (
            patch(
                "agent_gtd_dispatch.dispatch.subprocess.run",
                side_effect=subprocess.TimeoutExpired("git", 1),
            ),
            pytest.raises(dispatch.LandGitTimeout, match="git fetch"),
        ):
            dispatch.land_fetch(tmp_path, "main", 1)

    def test_origin_shas_failure_is_empty(self, tmp_path: Path) -> None:
        assert dispatch.land_origin_shas({"r": tmp_path}, {"r": "main"}, 5) == {"r": ""}

    def test_count_between_unknown_is_zero(self, tmp_path: Path) -> None:
        assert dispatch.land_count_between(tmp_path, "", "abc", 5) == 0
        assert dispatch.land_count_between(tmp_path, "a", "b", 5) == 0

    def test_delete_branch_failures_are_reported(self, mono) -> None:
        notes = dispatch.land_delete_feature_branch(mono.clone, "feat/nope", 30)
        assert len(notes) == 2

    def test_conflict_paths_clean_is_empty(self, mono) -> None:
        _squash(mono.clone)
        assert dispatch.land_conflict_paths(mono.clone, "main~1", FEATURE, 30) == []

    def test_conflict_paths_lists_the_conflicting_paths(self, mono) -> None:
        _conflict_squash(mono.clone, {"README.md": _README, "src/app.py": _APP})
        paths = dispatch.land_conflict_paths(mono.clone, "main~1", FEATURE, 30)
        assert sorted(paths) == ["README.md", "src/app.py"]

    def test_conflict_paths_other_exit_raises(self, tmp_path: Path) -> None:
        proc = subprocess.CompletedProcess(["git"], 129, b"", b"usage")
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess.run", return_value=proc),
            pytest.raises(
                dispatch.LandGitError, match="git merge-tree failed: exit 129: usage"
            ),
        ):
            dispatch.land_conflict_paths(tmp_path, "main", "feat/x", 30)

    def test_conflict_paths_unresolvable_side_raises(self, mono) -> None:
        with pytest.raises(
            dispatch.LandGitError, match="git merge-tree failed: exit 1"
        ):
            dispatch.land_conflict_paths(mono.clone, "main", "no-such-ref", 30)

    def test_fetch_feature_branch_reports_presence(self, mono) -> None:
        _git(mono.seed, "push", "-q", "origin", "HEAD:refs/heads/feat/x")
        assert dispatch.land_fetch_feature_branch(mono.clone, "feat/x", 30) is True
        assert _git(mono.clone, "rev-parse", "refs/remotes/origin/feat/x")
        assert dispatch.land_fetch_feature_branch(mono.clone, "feat/no", 30) is False

    def test_fetch_feature_branch_other_failure_raises(self, mono) -> None:
        _git(mono.clone, "remote", "set-url", "origin", "/nonexistent/origin.git")
        with pytest.raises(dispatch.LandGitError, match="git fetch origin failed"):
            dispatch.land_fetch_feature_branch(mono.clone, "feat/x", 30)

    def test_resolve_ref(self, mono) -> None:
        head = _git(mono.clone, "rev-parse", "HEAD")
        assert dispatch.land_resolve_ref(mono.clone, "refs/heads/main", 30) == head
        assert dispatch.land_resolve_ref(mono.clone, "refs/heads/nope", 30) == ""

    def test_conflict_markers_finds_a_leftover_marker(self, mono) -> None:
        (mono.clone / "README.md").write_text(f"a\n{_MARKER_LINE}b\n")
        (mono.clone / "clean.md").write_text("fine\n")
        _git(mono.clone, "add", "-A")
        _git(mono.clone, "commit", "-m", "m")
        assert dispatch.land_conflict_markers(
            mono.clone, "main", ["README.md", "clean.md"], 30
        ) == ["README.md"]

    def test_conflict_markers_ignores_a_setext_underline(self, mono) -> None:
        (mono.clone / "README.md").write_text("Title\n=======\n\nbody\n")
        _git(mono.clone, "add", "-A")
        _git(mono.clone, "commit", "-m", "m")
        assert (
            dispatch.land_conflict_markers(mono.clone, "main", ["README.md"], 30) == []
        )

    def test_conflict_markers_empty_paths_runs_no_git(self, tmp_path: Path) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as run:
            assert dispatch.land_conflict_markers(tmp_path, "main", [], 30) == []
        run.assert_not_called()

    def test_conflict_markers_other_exit_raises(self, mono) -> None:
        with pytest.raises(dispatch.LandGitError, match="git grep failed"):
            dispatch.land_conflict_markers(mono.clone, "no-such-rev", ["a.md"], 30)


# ---------------------------------------------------------------------------
# Green path
# ---------------------------------------------------------------------------


class TestGreenPath:
    async def test_rebases_onto_moved_origin_and_pushes(
        self, mono, gtd, caplog
    ) -> None:
        moved = _commit_unrelated(mono.seed, "moved.txt")
        _squash(mono.clone)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)

        assert isinstance(result, ReviewVerdict)
        assert result == _verdict()
        tip = _tip(mono.origin)
        assert _git(mono.origin, "rev-parse", "main~1") == moved
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        subjects, _ = dispatch.find_landed_commit_subjects(mono.ws, ITEM)
        assert subjects == {"repo": "fix(996b308e): Add X"}

        lines = _land_lines(caplog)
        assert len(lines) == 1
        assert f"gated_sha={tip}" in lines[0]
        assert f"pushed_sha={tip}" in lines[0]
        assert "origin_advanced=1" in lines[0]
        assert "push_attempt=1/3" in lines[0]
        assert "gate=passed" in lines[0]
        assert "outcome=pushed" in lines[0]
        assert "outcome=match" in caplog.text  # land-audit
        gtd.halt_rollout.assert_not_awaited()

        comment = gtd.post_comment.await_args.args[1]
        assert tip[:12] in comment
        assert "gate: passed in" in comment
        assert "rebased over 1 new origin commit(s)" in comment
        assert "fix relaunch: none" in comment

        phases = [c.args[1] for c in gtd.update_rollout_state.await_args_list]
        assert set(phases) == {"merging"}

    async def test_every_pushed_sha_is_the_last_gated_sha(
        self, mono, tmp_path: Path
    ) -> None:
        record = tmp_path / "gated.log"
        marker = tmp_path / "raced"
        other = _clone(mono.origin, tmp_path / "other")
        # First gate: race an unrelated commit onto origin -> push rejected.
        gate = (
            f"git rev-parse HEAD >> {record}; "
            f"if [ ! -f {marker} ]; then touch {marker}; cd {other} && "
            "echo r > race.txt && git add -A && git commit -qm race && "
            "git push -q origin main; fi; true"
        )
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        gated = record.read_text().split()
        assert len(gated) == 2
        assert _tip(mono.origin) == gated[-1]

    async def test_feature_branch_is_deleted_after_land(self, mono) -> None:
        _git(mono.clone, "checkout", "-q", "-b", "feat/996b308e-add-x")
        (mono.clone / "w.txt").write_text("w")
        _git(mono.clone, "add", "-A")
        _git(mono.clone, "commit", "-qm", "wip")
        _git(mono.clone, "push", "-q", "origin", "feat/996b308e-add-x")
        _git(mono.clone, "checkout", "-q", "main")
        _git(mono.clone, "merge", "--squash", "feat/996b308e-add-x")
        _git(mono.clone, "commit", "-m", f"fix: x\n\nRollout: {ROLLOUT}\nItem: {ITEM}")
        result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        assert _git(mono.origin, "branch", "--list", "feat/*") == ""
        assert _git(mono.clone, "branch", "--list", "feat/*") == ""

    async def test_branch_cleanup_failure_only_warns(self, mono, caplog) -> None:
        _squash(mono.clone)
        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        assert "branch-cleanup-failed" in caplog.text

    async def test_comment_failure_does_not_halt(self, mono, gtd) -> None:
        gtd.post_comment = AsyncMock(side_effect=RuntimeError("board down"))
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()

    async def test_commit_type_check_matches_after_land(
        self, mono, gtd, caplog
    ) -> None:
        _squash(mono.clone)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
            assert isinstance(result, ReviewVerdict)
            await main._act_on_verdict(
                ROLLOUT,
                ITEM,
                result,
                token=None,
                attribution=None,
                workspace=mono.ws,
            )
        assert any(
            "commit-type-check" in r.getMessage() and "outcome=match" in r.getMessage()
            for r in caplog.records
        )
        gtd.complete_in_rollout.assert_awaited_once()

    async def test_empty_gate_command_skips_the_gate(self, mono, gtd, caplog) -> None:
        _commit_unrelated(mono.seed, "moved.txt")
        _squash(mono.clone)
        with (
            patch.object(gates, "detect_gate_steps") as detect,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate="   ")
        detect.assert_not_called()
        assert isinstance(result, ReviewVerdict)
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        assert (
            f"rollout-loop: land-gate skipped_no_gate_command rollout_id={ROLLOUT} "
            f"item_id={ITEM}"
        ) in caplog.text
        assert "gate=skipped_no_gate_command" in _land_lines(caplog)[0]
        assert "SKIPPED" in gtd.post_comment.await_args.args[1]

    async def test_missing_gate_command_pushes_with_hooks(self, mono) -> None:
        _squash(mono.clone)
        calls = []
        real = dispatch.land_push

        def spy(*a, **kw):
            calls.append(kw["no_verify"])
            return real(*a, **kw)

        with patch.object(dispatch, "land_push", side_effect=spy):
            result = await _land(mono.ws, mono.pre, gate=None)
        assert isinstance(result, ReviewVerdict)
        assert calls == [False]


# ---------------------------------------------------------------------------
# Red gate, fix relaunch
# ---------------------------------------------------------------------------


def _fake_fixer(clone: Path, *, verdict: str = "merge", filename: str = "fixed.txt"):
    calls: list[dict] = []

    async def fake(*args, **kwargs):
        calls.append(kwargs)
        (clone / filename).write_text("fixed\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "--amend", "--no-edit")
        return ReviewVerdict(verdict, "added the missing file", "fix note", "")

    return fake, calls


class TestFixRelaunch:
    async def test_gate_fails_once_then_fix_lands_one_commit(
        self, mono, gtd, caplog
    ) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        fake, calls = _fake_fixer(mono.clone)
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate="test -f fixed.txt")
        assert isinstance(result, ReviewVerdict)
        assert len(calls) == 1
        assert calls[0]["gate_failure_output"] is not None
        assert _git(mono.origin, "rev-list", "--count", f"{before}..main") == "1"
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        fix_lines = [
            r.getMessage() for r in caplog.records if "land-fix" in r.getMessage()
        ]
        assert len(fix_lines) == 1
        assert "fixed.txt" in fix_lines[0]
        assert "files_changed=1" in fix_lines[0]
        assert result.merge_note == "original note"
        assert result.rationale.endswith("(land fix: added the missing file)")
        assert (
            "fix relaunch: used, amended 1 file(s)"
            in (gtd.post_comment.await_args.args[1])
        )
        assert any("outcome=fix_relaunched" in line for line in _land_lines(caplog))

    async def test_gate_always_fails_halts_with_the_tail(self, mono, gtd) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        fake, calls = _fake_fixer(mono.clone)
        gate = "for i in $(seq 1 400); do echo noise-line-$i; done; echo LAST-MARKER-42; exit 1"
        with patch.object(main, "_launch_reviewer", side_effect=fake):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert result == main._TICK_HALTED
        assert len(calls) == 1
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land gate failed for item {ITEM}: ")
        assert "LAST-MARKER-42" in reason
        assert len(reason) <= main.ERROR_TEXT_MAX_CHARS
        assert _tip(mono.origin) == before

    async def test_fix_verdict_not_merge_halts(self, mono, gtd, caplog) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        fake, _ = _fake_fixer(mono.clone, verdict="halt")
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate="false")
        assert result == main._TICK_HALTED
        assert "fix relaunch returned 'halt'" in _halt_reason(gtd)
        assert "outcome=fix_verdict_not_merge" in caplog.text
        assert _tip(mono.origin) == before

    async def test_gate_timeout_halts_without_relaunch(
        self, mono, gtd, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "LAND_GATE_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(config, "CANCEL_GRACE_SECONDS", 1)
        _squash(mono.clone)
        reviewer = AsyncMock()
        with patch.object(main, "_launch_reviewer", reviewer):
            result = await _land(mono.ws, mono.pre, gate="sleep 5")
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        assert _halt_reason(gtd).startswith(f"land gate timed out for item {ITEM}")

    async def test_gate_launch_error_halts_without_relaunch(self, mono, gtd) -> None:
        _squash(mono.clone)
        reviewer = AsyncMock()
        with (
            patch.object(main, "_launch_reviewer", reviewer),
            patch.object(
                dispatch,
                "run_gate_command",
                return_value=dispatch.GateResult(None, False, "no shell", 0.0),
            ),
        ):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        assert _halt_reason(gtd).startswith(f"land gate launch error for item {ITEM}")

    async def test_gate_install_failure_halts_without_relaunch(self, mono, gtd) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        reviewer = AsyncMock()
        failure = gates.GateFailure("repo", "pre-commit install", "boom", "", 3)
        with (
            patch.object(main, "_launch_reviewer", reviewer),
            patch.object(gates, "detect_gate_steps", return_value=[MagicMock()]),
            patch.object(gates, "run_gate_steps", return_value=failure),
        ):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        assert _halt_reason(gtd).startswith(f"land gate install failed for item {ITEM}")
        assert _tip(mono.origin) == before

    async def test_gate_install_success_continues(self, mono) -> None:
        _squash(mono.clone)
        with (
            patch.object(gates, "detect_gate_steps", return_value=[MagicMock()]),
            patch.object(gates, "run_gate_steps", return_value=None) as run_steps,
        ):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        run_steps.assert_called_once()

    async def test_head_moving_during_the_gate_halts(self, mono, gtd, caplog) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        gate = "echo z > z.txt && git add -A && git commit -q --amend --no-edit"
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert result == main._TICK_HALTED
        assert "land-invariant-violation" in caplog.text
        assert "HEAD moved during the gate" in _halt_reason(gtd)
        assert _tip(mono.origin) == before


# ---------------------------------------------------------------------------
# Rebase / push failures
# ---------------------------------------------------------------------------


class TestRebaseAndPushFailures:
    async def test_rebase_conflict_names_paths(self, mono, gtd) -> None:
        _git(mono.seed, "pull", "-q", "origin", "main")
        (mono.seed / "f.txt").write_text("origin-version\n")
        _git(mono.seed, "commit", "-qam", "origin edits f")
        _git(mono.seed, "push", "-q", "origin", "main")
        before = _tip(mono.origin)
        _squash(mono.clone, filename="f.txt", content="squash-version\n")
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land rebase conflict for item {ITEM}")
        assert "f.txt" in reason
        assert "repo" in reason
        assert _tip(mono.origin) == before
        assert not (mono.clone / ".git" / "rebase-merge").exists()
        assert not (mono.clone / ".git" / "rebase-apply").exists()

    async def test_non_fast_forward_race_regates_and_lands(
        self, mono, tmp_path: Path, caplog
    ) -> None:
        other = _clone(mono.origin, tmp_path / "other")
        marker = tmp_path / "raced"
        runs = tmp_path / "runs.log"
        gate = (
            f"echo run >> {runs}; "
            f"if [ ! -f {marker} ]; then touch {marker}; cd {other} && "
            "echo r > race.txt && git add -A && git commit -qm race && "
            "git push -q origin main; fi; true"
        )
        _squash(mono.clone)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        assert len(runs.read_text().split()) == 2
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        assert _git(mono.origin, "log", "-1", "--format=%s", "main~1") == "race"
        lines = _land_lines(caplog)
        assert "outcome=non_fast_forward" in lines[0]
        assert "push_attempt=2/3" in lines[-1]
        assert "outcome=pushed" in lines[-1]

    async def test_push_attempts_exhausted(self, mono, gtd, tmp_path: Path) -> None:
        other = _clone(mono.origin, tmp_path / "other")
        gate = (
            f"cd {other} && git pull -q origin main && echo $$ >> race.txt && "
            "git add -A && git commit -qm race && git push -q origin main"
        )
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre, gate=gate)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land push rejected 3 times for item {ITEM}"
        )
        assert f"Item: {ITEM}" not in _git(mono.origin, "log", "--format=%B", "main")

    async def test_push_to_missing_remote_fails(
        self, mono, gtd, tmp_path: Path
    ) -> None:
        _git(
            mono.clone,
            "remote",
            "set-url",
            "--push",
            "origin",
            str(tmp_path / "nope.git"),
        )
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(f"land push failed for item {ITEM}")

    async def test_audit_mismatch_halts(self, mono, gtd, caplog) -> None:
        _squash(mono.clone)
        reviewer = AsyncMock()
        with (
            patch.object(dispatch, "land_push", return_value=("pushed", "")),
            patch.object(main, "_act_on_verdict", reviewer),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        assert "outcome=mismatch" in caplog.text
        assert _halt_reason(gtd).startswith("land audit mismatch: remote ")
        reviewer.assert_not_awaited()

    async def test_git_timeout_halts(self, mono, gtd) -> None:
        _squash(mono.clone)
        with patch.object(
            dispatch, "land_rebase", side_effect=dispatch.LandGitTimeout("rebase")
        ):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith("land git timed out")
        assert "git rebase" in reason


# ---------------------------------------------------------------------------
# Guards on local state
# ---------------------------------------------------------------------------


class TestLocalStateGuards:
    async def test_reviewer_pushed_squash_itself(self, mono, gtd, caplog) -> None:
        _squash(mono.clone)
        _git(mono.clone, "push", "-q", "origin", "main")
        pushed = _tip(mono.origin)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(
            f"commit for item {ITEM} appeared on origin during review, "
            "before the worker's land"
        )
        assert "repo" in reason
        assert "outcome=reviewer_pushed" in caplog.text
        assert _tip(mono.origin) == pushed

    async def test_older_trailer_on_origin_does_not_trigger(
        self, tmp_path: Path, gtd
    ) -> None:
        origin, seed = _origin(tmp_path, "repo")
        _squash(seed, filename="old.txt", subject="fix: an earlier attempt")
        _git(seed, "push", "-q", "origin", "main")
        clone = _clone(origin, tmp_path / "repo")
        ws = _mono_ws(clone)
        pre = _pre(ws)
        _squash(clone)
        result = await _land(ws, pre)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()

    async def test_two_commits_ahead(self, mono, gtd) -> None:
        _squash(mono.clone)
        _squash(mono.clone, filename="second.txt")
        assert await _land(mono.ws, mono.pre) == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land bad local state for item {ITEM}")
        assert "repo" in reason
        assert "2 commit(s) ahead" in reason

    async def test_feature_branch_checked_out(self, mono, gtd) -> None:
        _squash(mono.clone)
        _git(mono.clone, "checkout", "-q", "-b", "feat/x")
        assert await _land(mono.ws, mono.pre) == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land bad local state for item {ITEM}")
        assert "feat/x" in reason

    async def test_squash_without_trailer(self, mono, gtd) -> None:
        _squash(mono.clone, trailer=False)
        assert await _land(mono.ws, mono.pre) == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land bad local state for item {ITEM}")
        assert "trailer" in reason

    async def test_zero_ahead_everywhere(self, mono, gtd, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            assert await _land(mono.ws, mono.pre) == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"merge verdict but no local squash commit for item {ITEM}"
        )
        assert "outcome=no_squash_commit" in caplog.text

    async def test_squash_emptied_by_rebase(self, mono, gtd) -> None:
        # Origin already carries the identical change: the rebase drops it.
        _git(mono.seed, "pull", "-q", "origin", "main")
        (mono.seed / "feature.txt").write_text("feature\n")
        _git(mono.seed, "add", "-A")
        _git(mono.seed, "commit", "-qm", "same change")
        _git(mono.seed, "push", "-q", "origin", "main")
        _squash(mono.clone)
        assert await _land(mono.ws, mono.pre) == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(f"land bad local state for item {ITEM}")


# ---------------------------------------------------------------------------
# Serialization on one host
# ---------------------------------------------------------------------------


class TestConflictGuard:
    async def test_doc_only_conflict_lands(self, mono, gtd, caplog) -> None:
        _conflict_squash(mono.clone, {"README.md": _README})
        pre = _pre(mono.ws)
        outcome = _verdict(resolved_conflict_paths=("README.md",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert _tip(mono.origin) != pre[mono.clone.name]
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        assert _git(mono.origin, "show", "main:README.md") == "branch line\nmain line"
        comment = gtd.post_comment.await_args.args[1]
        assert "Reviewer resolved documentation-only merge conflicts in: README.md" in (
            comment
        )
        assert (
            f"Conflict check ({mono.clone.name}): worker found documentation "
            "conflicts in README.md; reviewer reported resolving README.md"
        ) in comment
        assert "mismatch" not in comment
        assert "land-conflict-unreported" not in caplog.text
        checks = [
            r.getMessage()
            for r in caplog.records
            if "land-conflict-check" in r.getMessage()
        ]
        assert len(checks) == 1
        assert "decision=doc_only" in checks[0]
        assert "conflict_paths=README.md reported_resolved=README.md" in checks[0]
        assert "base=main~1" in checks[0]
        assert f"tip_ref=refs/heads/{FEATURE}" in checks[0]

    async def test_non_doc_conflict_halts(self, mono, gtd, caplog) -> None:
        _conflict_squash(mono.clone, {"README.md": _README, "src/app.py": _APP})
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("README.md",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: repo {mono.clone.name}: the squash "
            "merge conflicts on non-documentation paths: src/app.py"
        )
        assert _tip(mono.origin) == before
        assert "decision=non_doc_halt" in caplog.text
        assert "outcome=non_doc_conflict" in caplog.text

    async def test_reported_non_doc_path_halts(self, mono, gtd) -> None:
        _squash(mono.clone)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("src/app.py",))
        result = await _land(mono.ws, mono.pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land refused for item {ITEM}: the reviewer reported resolving "
            "non-documentation conflict paths: src/app.py"
        )
        assert _tip(mono.origin) == before

    async def test_missing_feature_branch_halts(self, mono, gtd) -> None:
        _squash(mono.clone)
        before = _tip(mono.origin)
        result = await _land(
            mono.ws, mono.pre, outcome=_verdict(branch_name="feat/missing")
        )
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land conflict check failed for item {ITEM}"
        )
        assert "feature branch feat/missing not found" in _halt_reason(gtd)
        assert _tip(mono.origin) == before

    async def test_empty_branch_name_halts(self, mono, gtd) -> None:
        _squash(mono.clone)
        before = _tip(mono.origin)
        result = await _land(mono.ws, mono.pre, outcome=_verdict(branch_name=""))
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land conflict check failed for item {ITEM}: the verdict names no "
            "feature branch"
        )
        assert _tip(mono.origin) == before

    async def test_branch_found_only_on_origin(self, mono, gtd, caplog) -> None:
        _squash(mono.clone, branch="feat/remote-only")
        _git(mono.clone, "push", "-q", "origin", "feat/remote-only")
        _git(mono.clone, "fetch", "-q", "origin")
        _git(mono.clone, "branch", "-D", "feat/remote-only")
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(
                mono.ws, mono.pre, outcome=_verdict(branch_name="feat/remote-only")
            )
        assert isinstance(result, ReviewVerdict)
        assert "tip_ref=refs/remotes/origin/feat/remote-only" in caplog.text
        assert "decision=clean" in caplog.text

    async def test_shallow_single_branch_clone_fetches_the_feature(
        self, mono, gtd, tmp_path, caplog
    ) -> None:
        shallow = tmp_path / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=50",
                "--single-branch",
                f"file://{mono.origin}",
                str(shallow),
            ],
            check=True,
            capture_output=True,
        )
        _git(shallow, "config", "user.name", "t")
        _git(shallow, "config", "user.email", "t@t")
        tip = _squash(shallow)
        _git(shallow, "push", "-q", "origin", f"HEAD:refs/heads/{FEATURE}")
        _git(shallow, "branch", "-D", FEATURE)
        missing = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{FEATURE}"],
            cwd=shallow,
            capture_output=True,
            check=False,
        )
        assert missing.returncode != 0
        ws = _mono_ws(shallow)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(ws, _pre(ws))
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert f"tip_ref=refs/remotes/origin/{FEATURE}" in caplog.text
        assert f"tip_sha={tip}" in caplog.text

    async def test_origin_copy_wins_over_a_stale_local_branch(
        self, mono, gtd, caplog
    ) -> None:
        tip = _squash(mono.clone)
        _git(mono.clone, "push", "-q", "origin", f"HEAD:refs/heads/{FEATURE}")
        _git(mono.clone, "branch", "-f", FEATURE, "main~1")
        stale = _git(mono.clone, "rev-parse", FEATURE)
        assert stale != tip
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        assert f"tip_ref=refs/remotes/origin/{FEATURE}" in caplog.text
        assert f"tip_sha={tip}" in caplog.text
        assert f"tip_sha={stale}" not in caplog.text

    async def test_fetch_failure_is_bad_local_state(
        self, mono, gtd, monkeypatch
    ) -> None:
        def boom(*_a, **_kw):
            raise dispatch.LandGitError("fetch origin", "unreachable")

        monkeypatch.setattr(dispatch, "land_fetch_feature_branch", boom)
        _squash(mono.clone)
        before = _tip(mono.origin)
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert "land bad local state for item" in reason
        assert "git fetch origin failed: unreachable" in reason
        assert "not found" not in reason
        assert _tip(mono.origin) == before

    async def test_unreported_doc_conflict_lands_with_a_warning(
        self, mono, gtd, caplog
    ) -> None:
        _conflict_squash(mono.clone, {"README.md": _README})
        pre = _pre(mono.ws)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=_verdict())
        assert isinstance(result, ReviewVerdict)
        assert "land-conflict-unreported" in caplog.text
        assert "unreported=README.md" in caplog.text
        comment = gtd.post_comment.await_args.args[1]
        assert (
            f"Conflict check ({mono.clone.name}): worker found documentation "
            "conflicts in README.md; reviewer reported resolving none "
            "(mismatch: unreported README.md)"
        ) in comment
        assert "Reviewer resolved documentation-only" not in comment

    async def test_leftover_markers_halt(self, mono, gtd, caplog) -> None:
        resolved = f"branch line\n{_MARKER_LINE}main line\n"
        _conflict_squash(
            mono.clone, {"README.md": ("branch line\n", "main line\n", resolved)}
        )
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("README.md",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land refused for item {ITEM}: repo {mono.clone.name}: conflict "
            "markers remain in: README.md"
        )
        assert _tip(mono.origin) == before
        assert "decision=markers_halt" in caplog.text

    async def test_merge_tree_failure_is_bad_local_state(
        self, mono, gtd, monkeypatch
    ) -> None:
        def boom(*_a, **_kw):
            raise dispatch.LandGitError("merge-tree", "exit 129: usage")

        monkeypatch.setattr(dispatch, "land_conflict_paths", boom)
        _squash(mono.clone)
        before = _tip(mono.origin)
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert "land bad local state for item" in reason
        assert "git merge-tree failed: exit" in reason
        assert _tip(mono.origin) == before

    async def test_clean_land_comment_is_unchanged(self, mono, gtd) -> None:
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        comment = gtd.post_comment.await_args.args[1]
        assert "\n" not in comment
        assert comment.startswith("Landed ")
        assert "Conflict check" not in comment


class TestConcurrentLands:
    async def test_two_lands_on_one_remote_serialize(
        self, tmp_path: Path, caplog
    ) -> None:
        origin, _seed = _origin(tmp_path, "repo")
        clone_a = _clone(origin, tmp_path / "a" / "repo")
        clone_b = _clone(origin, tmp_path / "b" / "repo")
        ws_a, ws_b = _mono_ws(clone_a), _mono_ws(clone_b)
        pre_a, pre_b = _pre(ws_a), _pre(ws_b)
        _squash(clone_a, ITEM, filename="a.txt")
        _squash(
            clone_b,
            ITEM_B,
            filename="b.txt",
            subject="feat(aaaabbbb): B",
            branch="feat/b",
        )
        record = tmp_path / "parents.log"
        gate = f'sleep 1; echo "$(pwd) $(git rev-parse HEAD~1)" >> {record}'
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            res_a, res_b = await asyncio.gather(
                _land(ws_a, pre_a, gate=gate, item_id=ITEM, loop_run_id="la"),
                _land(
                    ws_b,
                    pre_b,
                    gate=gate,
                    item_id=ITEM_B,
                    outcome=_verdict(branch_name="feat/b"),
                    loop_run_id="lb",
                ),
            )
        assert isinstance(res_a, ReviewVerdict)
        assert isinstance(res_b, ReviewVerdict)
        first_sha = _git(origin, "rev-parse", "main~1")
        second_dir = (
            clone_a
            if f"Item: {ITEM}" in _git(origin, "log", "-1", "--format=%B", "main")
            else clone_b
        )
        recorded = dict(line.split() for line in record.read_text().splitlines())
        assert recorded[str(second_dir)] == first_sha
        assert "outcome=non_fast_forward" not in caplog.text
        lines = _land_lines(caplog)
        assert len(lines) == 2
        assert all("push_attempt=1/3" in ln and "outcome=pushed" in ln for ln in lines)
        waits = [
            float(r.getMessage().rsplit("waited_secs=", 1)[1])
            for r in caplog.records
            if "land-lock rollout_id" in r.getMessage()
        ]
        assert len(waits) == 2
        assert max(waits) > 0
        assert caplog.text.count("land-lock-release") == 2


# ---------------------------------------------------------------------------
# Watchdog heartbeat
# ---------------------------------------------------------------------------


class TestLandHeartbeat:
    async def test_long_gate_keeps_publishing_the_heartbeat(
        self, mono, gtd, monkeypatch
    ) -> None:
        """A long land keeps the rollout's "last update" moving.

        Nothing reads the heartbeat for liveness any more (the staleness
        watchdog was retired), but the rollout monitor still shows it.
        """
        monkeypatch.setattr(main, "LAND_HEARTBEAT_SECONDS", 0.2)
        stamps: list[str] = []

        async def record(*_a, **_kw):
            stamps.append(datetime.now(UTC).isoformat())

        gtd.update_rollout_state = AsyncMock(side_effect=record)
        _squash(mono.clone)

        result = await _land(mono.ws, mono.pre, gate="sleep 3")
        assert isinstance(result, ReviewVerdict)
        assert len(stamps) > 5


# ---------------------------------------------------------------------------
# Workspace (multi-repo) projects
# ---------------------------------------------------------------------------


@pytest.fixture
def two_repo(tmp_path: Path):
    origin_a, seed_a = _origin(tmp_path, "repo-a")
    origin_b, seed_b = _origin(tmp_path, "repo-b")
    root = tmp_path / "ws"
    root.mkdir()
    clone_a = _clone(origin_a, root / "repo-a")
    clone_b = _clone(origin_b, root / "repo-b")
    ws = dispatch.RolloutWorkspace(
        root=root,
        repo_paths={"repo-a": clone_a, "repo-b": clone_b},
        default_branches={"repo-a": "main", "repo-b": "main"},
        created=False,
        workspace_mode=True,
    )
    return SimpleNamespace(
        origin_a=origin_a,
        origin_b=origin_b,
        seed_a=seed_a,
        seed_b=seed_b,
        clone_a=clone_a,
        clone_b=clone_b,
        ws=ws,
        pre=_pre(ws),
    )


class TestWorkspaceProjects:
    async def test_one_landing_repo_and_a_reset_sibling(
        self, two_repo, tmp_path: Path, caplog
    ) -> None:
        moved_b = _commit_unrelated(two_repo.seed_b, "sibling.txt")
        tip_b_before = _tip(two_repo.origin_b)
        _squash(two_repo.clone_a)
        # The reviewer left repo-b on a feature branch.
        _git(two_repo.clone_b, "checkout", "-q", "-b", "feat/996b308e-add-x")
        seen = tmp_path / "seen.log"
        gate = f"git -C repo-b rev-parse HEAD > {seen}"
        outcome = _verdict(
            repo_states=(("repo-a", "committed"), ("repo-b", "committed"))
        )
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(two_repo.ws, two_repo.pre, gate=gate, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        assert seen.read_text().strip() == moved_b
        assert _tip(two_repo.origin_b) == tip_b_before
        assert f"Item: {ITEM}" in _git(
            two_repo.origin_a, "log", "-1", "--format=%B", "main"
        )
        assert _git(two_repo.clone_b, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        disagreements = [
            r.getMessage()
            for r in caplog.records
            if "land-state-disagreement" in r.getMessage()
        ]
        assert len(disagreements) == 1
        assert "repo=repo-b reviewer_claim=committed git_ahead=0" in disagreements[0]
        lines = _land_lines(caplog)
        assert len(lines) == 1
        assert "repo=repo-a" in lines[0]

    async def test_doc_only_conflict_in_one_repo_lands(
        self, two_repo, gtd, caplog
    ) -> None:
        _conflict_squash(two_repo.clone_a, {"README.md": _README})
        pre = _pre(two_repo.ws)
        outcome = _verdict(
            repo_states=(("repo-a", "committed"),),
            resolved_conflict_paths=("repo-a/README.md",),
        )
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(two_repo.ws, pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert "land-conflict-unreported" not in caplog.text
        comment = gtd.post_comment.await_args.args[1]
        assert (
            "Conflict check (repo-a): worker found documentation conflicts in "
            "README.md; reviewer reported resolving README.md"
        ) in comment
        assert (
            "Reviewer resolved documentation-only merge conflicts in: repo-a/README.md"
        ) in comment

    async def test_both_repos_land(self, two_repo, gtd) -> None:
        _squash(two_repo.clone_a)
        _squash(two_repo.clone_b, filename="b.txt")
        result = await _land(two_repo.ws, two_repo.pre)
        assert isinstance(result, ReviewVerdict)
        for origin in (two_repo.origin_a, two_repo.origin_b):
            assert f"Item: {ITEM}" in _git(origin, "log", "-1", "--format=%B", "main")
        comment = gtd.post_comment.await_args.args[1]
        assert "repo-a/main" in comment
        assert "repo-b/main" in comment

    async def test_partial_push_then_red_regate_halts_without_relaunch(
        self, two_repo, gtd, tmp_path: Path
    ) -> None:
        _squash(two_repo.clone_a)
        _squash(two_repo.clone_b, filename="b.txt")
        tip_b = _tip(two_repo.origin_b)
        real_push = dispatch.land_push
        raced = []

        def racing_push(repo, *a, **kw):
            if repo == two_repo.clone_b and not raced:
                raced.append(_commit_unrelated(two_repo.seed_b, "race.txt"))
            return real_push(repo, *a, **kw)

        counter = tmp_path / "count"
        gate = (
            f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); "
            f"echo $n > {counter}; [ $n -lt 2 ]"
        )
        reviewer = AsyncMock()
        with (
            patch.object(dispatch, "land_push", side_effect=racing_push),
            patch.object(main, "_launch_reviewer", reviewer),
        ):
            result = await _land(two_repo.ws, two_repo.pre, gate=gate)
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land gate failed for item {ITEM}: ")
        assert "repo-a: pushed" in reason
        assert "repo-b: not pushed" in reason
        assert f"Item: {ITEM}" in _git(
            two_repo.origin_a, "log", "-1", "--format=%B", "main"
        )
        assert _tip(two_repo.origin_b) == raced[0]
        assert _tip(two_repo.origin_b) != tip_b

    async def test_rebase_conflict_in_second_repo_pushes_nothing(
        self, two_repo, gtd
    ) -> None:
        tip_a = _tip(two_repo.origin_a)
        _git(two_repo.seed_b, "pull", "-q", "origin", "main")
        (two_repo.seed_b / "f.txt").write_text("origin-version\n")
        _git(two_repo.seed_b, "commit", "-qam", "origin edits f")
        _git(two_repo.seed_b, "push", "-q", "origin", "main")
        _squash(two_repo.clone_a)
        _squash(two_repo.clone_b, filename="f.txt", content="mine\n")
        assert await _land(two_repo.ws, two_repo.pre) == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert "repo-b" in reason
        assert "f.txt" in reason
        assert _tip(two_repo.origin_a) == tip_a

    async def test_non_landing_repo_ahead_after_fix_halts(self, two_repo, gtd) -> None:
        _squash(two_repo.clone_a)

        async def bad_fix(*_a, **_kw):
            _squash(two_repo.clone_b, filename="sneaky.txt")
            return ReviewVerdict("merge", "fixed in the wrong repo", "", "")

        with patch.object(main, "_launch_reviewer", side_effect=bad_fix):
            result = await _land(two_repo.ws, two_repo.pre, gate="false")
        assert result == main._TICK_HALTED
        assert "changed the landing set" in _halt_reason(gtd)


# ---------------------------------------------------------------------------
# Through the wave tick
# ---------------------------------------------------------------------------


class TestThroughTheWaveTick:
    async def test_fix_relaunch_keeps_the_original_merge_note(self, mono, gtd) -> None:
        verdicts = []

        async def reviewer(*_a, **kwargs):
            if kwargs.get("gate_failure_output") is None:
                _squash(mono.clone)
                v = _verdict()
            else:
                (mono.clone / "fixed.txt").write_text("x")
                _git(mono.clone, "add", "-A")
                _git(mono.clone, "commit", "--amend", "--no-edit")
                v = ReviewVerdict("merge", "amended", "FIX NOTE", "")
            verdicts.append(v)
            return v

        gtd.advance_rollout = AsyncMock(
            return_value={
                "next_ready": [],
                "in_progress": [ITEM],
                "graph_complete": False,
            }
        )
        gtd.get_rollout = AsyncMock(
            return_value={"status": "running", "inFlightBuildRuns": []}
        )
        with (
            patch.object(main, "_launch_reviewer", side_effect=reviewer),
            patch.object(main, "_rollout_unrecorded_merges", {}),
        ):
            directive = await main._rollout_wave_tick(
                ROLLOUT,
                project={"name": "P", "gate_command": "test -f fixed.txt"},
                workspace_ref=[mono.ws],
                reviewer_engine=get_engine("claude-code-sonnet"),
                dispatch_attempts={},
                token=None,
                attribution=None,
                loop_run_id="loop-1",
            )
        assert directive == main._TICK_CONTINUE
        assert len(verdicts) == 2
        gtd.complete_in_rollout.assert_awaited_once()
        assert gtd.complete_in_rollout.await_args.kwargs["merge_note"] == (
            "original note"
        )
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")


class TestFixRelaunchAfterABoardTimeout:
    async def test_fix_relaunch_needs_no_board_item_read(self, mono, gtd) -> None:
        """A board read after the first reviewer ran would re-run the tick."""
        calls = []

        async def reviewer(*_a, **kwargs):
            # Mirrors the real _launch_reviewer: board read only without an item.
            if kwargs.get("item") is None:
                await gtd.get_item(ITEM, token=None)
            calls.append(kwargs.get("gate_failure_output"))
            if kwargs.get("gate_failure_output") is None:
                _squash(mono.clone)
                return _verdict()
            (mono.clone / "fixed.txt").write_text("x")
            _git(mono.clone, "add", "-A")
            _git(mono.clone, "commit", "--amend", "--no-edit")
            return ReviewVerdict("merge", "amended", "FIX NOTE", "")

        gtd.get_item = AsyncMock(
            side_effect=[
                {"id": ITEM, "title": "t"},
                httpx.ReadTimeout("board down"),
            ]
        )
        gtd.advance_rollout = AsyncMock(
            return_value={
                "next_ready": [],
                "in_progress": [ITEM],
                "graph_complete": False,
            }
        )
        gtd.get_rollout = AsyncMock(
            return_value={"status": "running", "inFlightBuildRuns": []}
        )
        with (
            patch.object(main, "_launch_reviewer", side_effect=reviewer),
            patch.object(main, "_rollout_unrecorded_merges", {}),
        ):
            directive = await main._rollout_wave_tick(
                ROLLOUT,
                project={"name": "P", "gate_command": "test -f fixed.txt"},
                workspace_ref=[mono.ws],
                reviewer_engine=get_engine("claude-code-sonnet"),
                dispatch_attempts={},
                token=None,
                attribution=None,
                loop_run_id="loop-1",
            )
        assert directive == main._TICK_CONTINUE
        assert len(calls) == 2
        assert calls[1] is not None  # the second launch was the fix relaunch
        gtd.get_item.assert_awaited_once()
        gtd.complete_in_rollout.assert_awaited_once()
