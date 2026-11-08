"""The worker-owned land step: fetch, rebase onto current origin, gate, push.

Driven against real git repos with real bare origins (same shape as
``TestFindLandedCommitSubjects``); only the GTD client and, where a test says
so, the reviewer relaunch are faked.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def _write_or_delete(clone: Path, name: str, content: str | None) -> None:
    target = clone / name
    if content is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _conflict_squash(
    clone: Path,
    files: dict[str, tuple[str | None, str | None, str | None]],
    item_id: str = ITEM,
) -> str:
    """Fabricate a reviewer's squash commit whose merge conflicted.

    *files* maps path -> (branch content, default-branch content, resolved
    content); ``None`` deletes the path on that side or in the resolution.
    The default-branch edit is pushed to origin, so origin is the squash
    commit's parent.
    """
    _git(clone, "checkout", "-q", "-b", FEATURE)
    for name, (ours, _theirs, _resolved) in files.items():
        _write_or_delete(clone, name, ours)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "feat: branch side")
    _git(clone, "checkout", "-q", "main")
    for name, (_ours, theirs, _resolved) in files.items():
        _write_or_delete(clone, name, theirs)
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
        _write_or_delete(clone, name, resolved)
        _git(clone, "add", "-A", "--", name)
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
_REGISTRY = (
    "ITEMS = [\n    'a',\n    'b',\n]\n",
    "ITEMS = [\n    'a',\n    'c',\n]\n",
    "ITEMS = [\n    'a',\n    'b',\n    'c',\n]\n",
)
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
    gtd.acquire_land_lease = AsyncMock(
        return_value={"acquired": True, "lease": {"lease_id": "L1"}}
    )
    gtd.renew_land_lease = AsyncMock(
        return_value={"renewed": True, "lease": {"lease_id": "L1"}}
    )
    gtd.release_land_lease = AsyncMock(return_value={"released": True})
    gtd.get_rollout = AsyncMock(return_value={"status": "running"})
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
    loop_run_id: str = "100000000001",
    probe: main._LandProbe | None = None,
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
        probe=probe,
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
        assert main.MAX_LAND_CONFLICT_RELAUNCHES == 1
        assert main.LAND_HEARTBEAT_SECONDS == 300
        assert main.LAND_LEASE_RENEW_SECONDS == 60
        assert main.LAND_LEASE_POLL_SECONDS == 15
        assert main.LAND_LEASE_TTL_MARGIN_SECONDS == 600
        assert main.LAND_LEASE_WAIT_WARN_SECONDS == 1800

    def test_land_lease_ttl(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "LAND_GATE_TIMEOUT_SECONDS", 1800)
        assert main._land_lease_ttl() == 2400

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

    def test_paths_missing_lists_absent_paths(self, mono) -> None:
        assert dispatch.land_paths_missing(
            mono.clone, "main", ["f.txt", "gone.py"], 30
        ) == ["gone.py"]

    def test_merge_base_and_added_and_file_lines(self, mono) -> None:
        base = _git(mono.clone, "rev-parse", "HEAD")
        (mono.clone / "f.txt").write_text("a\n  b  \n\n")
        _git(mono.clone, "commit", "-qam", "edit")
        assert dispatch.land_merge_base(mono.clone, base, "HEAD", 30) == base
        assert dispatch.land_added_lines(mono.clone, base, "HEAD", "f.txt", 30) == ["b"]
        assert dispatch.land_file_lines(mono.clone, "HEAD", "f.txt", 30) == [
            "a",
            "b",
            "",
        ]

    def test_new_helpers_raise_on_git_errors(self, mono) -> None:
        with pytest.raises(dispatch.LandGitError, match="merge-base"):
            dispatch.land_merge_base(mono.clone, "no-such-rev", "main", 30)
        with pytest.raises(dispatch.LandGitError, match="diff"):
            dispatch.land_added_lines(mono.clone, "no-such-rev", "main", "f.txt", 30)
        with pytest.raises(dispatch.LandGitError, match="show"):
            dispatch.land_file_lines(mono.clone, "main", "nope.txt", 30)

    def test_lockfile_basenames(self) -> None:
        assert dispatch.LOCKFILE_BASENAMES == (
            "uv.lock",
            "poetry.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "Cargo.lock",
            "go.sum",
        )


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
        assert set(phases) == {"waiting_for_lease", "merging"}

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

    async def test_gate_failure_surfaces_failure_line_and_log_path(
        self, mono, gtd, caplog
    ) -> None:
        from agent_gtd_dispatch import retention

        _squash(mono.clone)
        fake, calls = _fake_fixer(mono.clone)
        gate = (
            "echo 'FAILED t::boom'; head -c 5000 /dev/zero | tr '\\0' x; echo; exit 1"
        )
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert "FAILED t::boom" in reason
        assert "(full log: " in reason
        assert len(reason) <= main.ERROR_TEXT_MAX_CHARS
        logs = list(retention.evidence_dir("100000000001").glob(f"land-{ITEM}-*.log"))
        assert len(logs) == 2
        assert str(max(logs).resolve()) in reason
        out = calls[0]["gate_failure_output"]
        assert "FAILED t::boom" in out
        assert "Full gate log:" not in out
        lines = [r.getMessage() for r in caplog.records]
        gate_lines = [x for x in lines if x.startswith("land gate:")]
        assert gate_lines
        for tok in ("log_path=", "log_bytes=", "failure_lines_matched="):
            assert tok in gate_lines[0]

    async def test_passing_gate_logs_land_gate_line(self, mono, gtd, caplog) -> None:
        _squash(mono.clone)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate="true")
        assert isinstance(result, ReviewVerdict)
        line = next(
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("land gate:")
        )
        assert "failure_lines_matched=0" in line
        assert "log_path=" in line

    def _lander(self, pushed: set[str]):
        lander = object.__new__(main._Lander)
        lander.landing = ["repo-a", "repo-b"]
        lander.repos = ["repo-a", "repo-b"]
        lander.pushed = pushed
        lander.rollout_id = "r"
        lander.item_id = ITEM
        lander.token = None
        lander.attribution = None
        lander._log_land = lambda *_a, **_k: None  # type: ignore[method-assign]
        return lander

    @pytest.mark.parametrize("ctx_len", [0, 3000])
    async def test_halt_budget_and_invariant(self, ctx_len, caplog) -> None:
        from pathlib import Path

        lander = self._lander({"repo-a"} if ctx_len == 0 else set())
        result = dispatch.GateResult(
            1,
            False,
            "",
            1.0,
            log_path=Path("/ev/land-x.log"),
            failure_lines=("FAILED t::boom",),
            failure_lines_total=1,
            tail="z" * 8000,
        )
        halt = lander._gate_halt(result, ["p" * ctx_len] if ctx_len else None)
        captured: dict[str, str] = {}

        async def _fake_halt(rollout_id, reason, **_k):
            captured["reason"] = reason
            return "halted"

        with (
            patch.object(main, "_halt_rollout_from_loop", _fake_halt),
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"),
        ):
            await lander._halt(halt)
        reason = captured["reason"]
        assert len(reason) <= main.ERROR_TEXT_MAX_CHARS
        if ctx_len == 0:
            assert "FAILED t::boom" in reason
            assert "/ev/land-x.log" in reason
            assert "repo-a: pushed" in reason
        else:
            assert "summary invariant violated" in caplog.text

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

    async def test_cancelled_land_reaps_dirs_before_tmp_removal(self, mono) -> None:
        import threading

        _squash(mono.clone)
        order: list[tuple[str, object]] = []
        entered = threading.Event()
        release = threading.Event()
        log_contexts: list[str] = []

        def _gate(
            *_a, tmp_dir=None, log_context="", log_dir=None, log_name_prefix="gate"
        ):
            log_contexts.append(log_context)
            entered.set()
            release.wait(10)
            return dispatch.GateResult(0, False, "", 0.0)

        def _reap(d, *, own_marker=None):
            order.append(("reap", d))
            return []

        real_remove = main.run_tmp.remove_run_tmp

        def _remove(p, *, kind):
            order.append(("remove_tmp", p))
            real_remove(p, kind=kind)

        terminate = AsyncMock()
        try:
            with (
                patch.object(dispatch, "run_gate_command", side_effect=_gate),
                patch.object(dispatch, "reap_workspace_processes", side_effect=_reap),
                patch.object(main.run_tmp, "remove_run_tmp", side_effect=_remove),
                patch.object(main, "_terminate_run_session", terminate),
            ):
                task = asyncio.create_task(_land(mono.ws, mono.pre))
                await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        assert len(order) == 3, order
        tmp_dir = order[2][1]
        assert order == [
            ("reap", mono.ws.root),
            ("reap", tmp_dir),
            ("remove_tmp", tmp_dir),
        ]
        terminate.assert_not_awaited()
        assert log_contexts == [f"rollout_id={ROLLOUT} item_id={ITEM}"]

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
    async def test_rebase_conflict_names_paths(self, mono, gtd, caplog) -> None:
        _git(mono.seed, "pull", "-q", "origin", "main")
        (mono.seed / "f.txt").write_text("origin-version\n")
        _git(mono.seed, "commit", "-qam", "origin edits f")
        _git(mono.seed, "push", "-q", "origin", "main")
        before = _tip(mono.origin)
        _squash(mono.clone, filename="f.txt", content="squash-version\n")
        refuse = AsyncMock(
            return_value=ReviewVerdict(
                "halt",
                "the sides disagree",
                "",
                "",
                judgment_conflict_paths=("f.txt",),
            )
        )
        with (
            patch.object(main, "_launch_reviewer", refuse),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land rebase conflict for item {ITEM}")
        assert "f.txt" in reason
        assert "repo" in reason
        assert "[conflicts needing judgment: f.txt]" in reason
        assert _tip(mono.origin) == before
        assert not (mono.clone / ".git" / "rebase-merge").exists()
        assert not (mono.clone / ".git" / "rebase-apply").exists()
        assert "outcome=land_conflict_not_resolved" in caplog.text
        assert "decision=relaunch" in caplog.text
        verdicts = [
            r.getMessage()
            for r in caplog.records
            if "land-conflict-relaunch-verdict" in r.getMessage()
        ]
        assert len(verdicts) == 1
        assert "verdict=halt" in verdicts[0]
        refuse.assert_awaited_once()

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
        assert (
            "Reviewer resolved merge conflicts by keeping both sides in: README.md"
        ) in comment
        assert (
            f"Conflict check ({mono.clone.name}): worker found conflicts in "
            "README.md; reviewer resolved keep-both: README.md"
        ) in comment
        assert "documentation-only" not in comment
        assert "documentation conflicts" not in comment
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

    async def test_resolved_code_conflict_lands(self, mono, gtd, caplog) -> None:
        _conflict_squash(mono.clone, {"src/registry.py": _REGISTRY})
        pre = _pre(mono.ws)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert _git(mono.origin, "show", "main:src/registry.py") == _REGISTRY[2].strip()
        comment = gtd.post_comment.await_args.args[1]
        assert (
            f"Conflict check ({mono.clone.name}): worker found conflicts in "
            "src/registry.py; reviewer resolved keep-both: src/registry.py"
        ) in comment
        assert (
            "Reviewer resolved merge conflicts by keeping both sides in: "
            "src/registry.py"
        ) in comment
        assert "keep-both audit" not in comment
        assert "decision=resolved" in caplog.text
        pushed = [line for line in _land_lines(caplog) if "outcome=pushed" in line]
        assert len(pushed) == 1
        assert "conflict=code conflict_code_paths=src/registry.py" in pushed[0]
        audit = [r for r in caplog.records if "land-keepboth-audit" in r.getMessage()]
        assert len(audit) == 1
        assert audit[0].levelno == logging.INFO
        assert "main_dropped=0 branch_dropped=0 outcome=ok" in audit[0].getMessage()

    async def test_resolved_code_conflict_gates_the_pushed_sha(
        self, mono, gtd, tmp_path: Path
    ) -> None:
        _conflict_squash(mono.clone, {"src/registry.py": _REGISTRY})
        pre = _pre(mono.ws)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        result = await _land(
            mono.ws,
            pre,
            outcome=outcome,
            gate=f"git rev-parse HEAD > {tmp_path}/gated_sha",
        )
        assert isinstance(result, ReviewVerdict)
        assert (tmp_path / "gated_sha").read_text().strip() == _tip(mono.origin)

    async def test_unreported_code_conflict_halts(self, mono, gtd, caplog) -> None:
        _conflict_squash(mono.clone, {"src/registry.py": _REGISTRY})
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=())
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: repo {mono.clone.name}: the squash "
            "merge conflicts on paths the reviewer did not report resolving: "
            "src/registry.py"
        )
        assert _tip(mono.origin) == before
        assert "decision=unreported_halt" in caplog.text
        assert "outcome=unreported_conflict" in caplog.text

    async def test_code_conflict_markers_halt(self, mono, gtd, caplog) -> None:
        resolved = f"ITEMS = [\n{_MARKER_LINE}    'b',\n    'c',\n]\n"
        _conflict_squash(
            mono.clone,
            {"src/registry.py": (_REGISTRY[0], _REGISTRY[1], resolved)},
        )
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land refused for item {ITEM}: repo {mono.clone.name}: conflict "
            "markers remain in: src/registry.py"
        )
        assert _tip(mono.origin) == before
        assert "decision=markers_halt" in caplog.text
        assert "outcome=conflict_markers" in caplog.text

    async def test_resolved_code_conflict_gate_failure_halts_without_fix_relaunch(
        self, mono, gtd, caplog
    ) -> None:
        _conflict_squash(mono.clone, {"src/registry.py": _REGISTRY})
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        fixer = AsyncMock()
        with (
            patch.object(main, "_launch_reviewer", fixer),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            # Output keeps the halt reason's trailing ": " (an empty tail is
            # stripped).
            result = await _land(
                mono.ws, pre, outcome=outcome, gate="echo gate-red; false"
            )
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land gate failed for item {ITEM} after keep-both conflict "
            "resolution in src/registry.py: "
        )
        assert _tip(mono.origin) == before
        fixer.assert_not_awaited()
        assert "land-fix-relaunch-skipped" in caplog.text
        assert "reason=code_conflict paths=src/registry.py" in caplog.text
        assert all("fix_relaunches=0" in line for line in _land_lines(caplog))

    def test_gate_halt_heads_name_the_conflict_paths(self, mono) -> None:
        lander = main._Lander(
            ROLLOUT,
            ITEM,
            _verdict(),
            project={"name": "P"},
            workspace=mono.ws,
            pre_review_origin={},
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="1",
            token=None,
            attribution=None,
        )
        timed_out = dispatch.GateResult(None, True, "", 7.0)
        launch = dispatch.GateResult(None, False, "boom", 0.0)
        ctx = " after keep-both conflict resolution in a.py, b.py"
        paths = ["a.py", "b.py"]
        assert lander._gate_halt(timed_out, paths).head == (
            f"land gate timed out for item {ITEM}{ctx} after 7s"
        )
        assert lander._gate_halt(launch, paths).head == (
            f"land gate launch error for item {ITEM}{ctx}: boom"
        )
        assert lander._gate_halt(timed_out, []).head == (
            f"land gate timed out for item {ITEM} after 7s"
        )
        assert lander._gate_halt(launch).head == (
            f"land gate launch error for item {ITEM}: boom"
        )

    async def test_resolved_code_conflict_without_gate_halts(
        self, mono, gtd, caplog
    ) -> None:
        _conflict_squash(mono.clone, {"src/registry.py": _REGISTRY})
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome, gate="")
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: code conflicts were resolved in "
            "src/registry.py but the project has no gate_command to verify the "
            "merged tree"
        )
        assert _tip(mono.origin) == before
        assert "decision=no_gate_halt" in caplog.text
        assert "outcome=conflict_needs_gate" in caplog.text

    async def test_overreported_code_path_lands_with_a_warning(
        self, mono, gtd, caplog
    ) -> None:
        before = _tip(mono.origin)
        _squash(mono.clone)
        outcome = _verdict(resolved_conflict_paths=("src/app.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert (
            f"land-conflict-overreported rollout_id={ROLLOUT} item_id={ITEM} "
            f"repo={mono.clone.name} overreported=src/app.py"
        ) in caplog.text
        assert _tip(mono.origin) != before
        assert "decision=clean" in caplog.text

    async def test_lockfile_conflict_halts(self, mono, gtd, caplog) -> None:
        _conflict_squash(
            mono.clone, {"uv.lock": ("a = 1\n", "a = 2\n", "a = 1\na = 2\n")}
        )
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("uv.lock",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: repo {mono.clone.name}: conflicts on "
            "lockfiles cannot be resolved by keeping both sides: uv.lock"
        )
        assert _tip(mono.origin) == before
        assert "decision=kind_halt" in caplog.text
        assert "outcome=unresolvable_conflict_kind" in caplog.text

    async def test_deleted_conflict_path_halts(self, mono, gtd, caplog) -> None:
        (mono.clone / "src").mkdir()
        (mono.clone / "src" / "registry.py").write_text("ITEMS = ['a']\n")
        _git(mono.clone, "add", "-A")
        _git(mono.clone, "commit", "-m", "chore: add registry")
        _git(mono.clone, "push", "-q", "origin", "main")
        _conflict_squash(
            mono.clone,
            {
                "src/registry.py": ("ITEMS = ['a', 'b']\n", None, None),
                # A clean branch-only file, so the squash commit is not empty.
                "feature.txt": ("x\n", None, "x\n"),
            },
        )
        pre = _pre(mono.ws)
        before = _tip(mono.origin)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: repo {mono.clone.name}: conflicting "
            "paths are absent from the squash commit (a delete or rename conflict "
            "needs judgment): src/registry.py"
        )
        assert _tip(mono.origin) == before
        assert "decision=missing_halt" in caplog.text
        assert "outcome=conflict_path_missing" in caplog.text

    async def test_keepboth_audit_flags_dropped_line(self, mono, gtd, caplog) -> None:
        branch_only = "ITEMS = [\n    'a',\n    'b',\n]\n"
        _conflict_squash(
            mono.clone,
            {"src/registry.py": (_REGISTRY[0], _REGISTRY[1], branch_only)},
        )
        pre = _pre(mono.ws)
        outcome = _verdict(resolved_conflict_paths=("src/registry.py",))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        audit = [r for r in caplog.records if "land-keepboth-audit" in r.getMessage()]
        assert len(audit) == 1
        assert audit[0].levelno == logging.WARNING
        assert "main_dropped=1 branch_dropped=0 outcome=dropped" in (
            audit[0].getMessage()
        )
        comment = gtd.post_comment.await_args.args[1]
        assert (
            " (keep-both audit: src/registry.py dropped 1 main / 0 branch added lines)"
        ) in comment

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
            f"Conflict check ({mono.clone.name}): worker found conflicts in "
            "README.md; reviewer resolved keep-both: none "
            "(mismatch: unreported README.md)"
        ) in comment
        assert "Reviewer resolved merge conflicts" not in comment

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
        landed, lease = comment.split("\n")
        assert landed.startswith("Landed ")
        assert lease == (
            "Land lease: waited 0s behind 0 holder(s); reacquires: 0; "
            "rejected while lease held: 0"
        )
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
                _land(ws_a, pre_a, gate=gate, item_id=ITEM, loop_run_id="a00000000001"),
                _land(
                    ws_b,
                    pre_b,
                    gate=gate,
                    item_id=ITEM_B,
                    outcome=_verdict(branch_name="feat/b"),
                    loop_run_id="b00000000001",
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
            "Conflict check (repo-a): worker found conflicts in "
            "README.md; reviewer resolved keep-both: README.md"
        ) in comment
        assert (
            "Reviewer resolved merge conflicts by keeping both sides in: "
            "repo-a/README.md"
        ) in comment

    async def test_keep_both_code_conflict_in_one_repo_lands(
        self, two_repo, gtd, caplog
    ) -> None:
        _conflict_squash(two_repo.clone_a, {"src/registry.py": _REGISTRY})
        pre = _pre(two_repo.ws)
        outcome = _verdict(
            repo_states=(("repo-a", "committed"),),
            resolved_conflict_paths=("repo-a/src/registry.py",),
        )
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(two_repo.ws, pre, outcome=outcome)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert "decision=resolved" in caplog.text
        comment = gtd.post_comment.await_args.args[1]
        assert (
            "Conflict check (repo-a): worker found conflicts in src/registry.py; "
            "reviewer resolved keep-both: src/registry.py"
        ) in comment
        assert comment.splitlines()[-1] == (
            "Reviewer resolved merge conflicts by keeping both sides in: "
            "repo-a/src/registry.py"
        )
        assert _git(two_repo.origin_a, "show", "main:src/registry.py") == (
            _REGISTRY[2].strip()
        )

    async def test_code_conflict_without_gate_names_the_repo(
        self, two_repo, gtd
    ) -> None:
        _conflict_squash(two_repo.clone_a, {"src/registry.py": _REGISTRY})
        pre = _pre(two_repo.ws)
        outcome = _verdict(
            repo_states=(("repo-a", "committed"),),
            resolved_conflict_paths=("repo-a/src/registry.py",),
        )
        result = await _land(two_repo.ws, pre, outcome=outcome, gate="")
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: code conflicts were resolved in "
            "repo-a/src/registry.py but the project has no gate_command to "
            "verify the merged tree"
        )

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
        self, two_repo, gtd, caplog
    ) -> None:
        tip_a = _tip(two_repo.origin_a)
        _git(two_repo.seed_b, "pull", "-q", "origin", "main")
        (two_repo.seed_b / "f.txt").write_text("origin-version\n")
        _git(two_repo.seed_b, "commit", "-qam", "origin edits f")
        _git(two_repo.seed_b, "push", "-q", "origin", "main")
        tip_b = _tip(two_repo.origin_b)
        _squash(two_repo.clone_a)
        _squash(two_repo.clone_b, filename="f.txt", content="mine\n")
        refuse = AsyncMock(
            return_value=ReviewVerdict(
                "halt",
                "the sides disagree",
                "",
                "",
                judgment_conflict_paths=("repo-b/f.txt",),
            )
        )
        with (
            patch.object(main, "_launch_reviewer", refuse),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(two_repo.ws, two_repo.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land rebase conflict for item {ITEM}")
        assert "repo-b" in reason
        assert "f.txt" in reason
        assert "[conflicts needing judgment: repo-b/f.txt]" in reason
        assert _tip(two_repo.origin_a) == tip_a
        assert _tip(two_repo.origin_b) == tip_b
        for clone in (two_repo.clone_a, two_repo.clone_b):
            assert not (clone / ".git" / "rebase-merge").exists()
            assert not (clone / ".git" / "rebase-apply").exists()
        assert "outcome=land_conflict_not_resolved" in caplog.text
        assert "decision=relaunch" in caplog.text
        assert "verdict=halt" in caplog.text
        assert refuse.await_args.kwargs["land_conflict"] == {"repo-b": ["f.txt"]}

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


async def _tick_until_settled(loop_state, *, max_ticks=10, **kwargs):
    """Run wave ticks, awaiting child review/land tasks between them.

    The tick's loop-fencing check reads this host's runs for the rollout from
    the local SQLite db. Stub it to "no runs" so the test never depends on
    config.WORKSPACE_ROOT existing (it does on a dispatch host, not elsewhere).
    """
    directive = None
    with patch.object(main.db, "list_runs_by_rollout", AsyncMock(return_value=[])):
        for _ in range(max_ticks):
            directive = await main._rollout_wave_tick(
                ROLLOUT, loop_state=loop_state, **kwargs
            )
            tasks = [e.task for e in loop_state.reviews.values()]
            if loop_state.active_land is not None:
                tasks.append(loop_state.active_land.task)
            if not tasks:
                return directive
            await asyncio.gather(*tasks, return_exceptions=True)
    return directive


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
            directive = await _tick_until_settled(
                main._RolloutLoopState(slots={0: mono.ws}),
                project={"name": "P", "gate_command": "test -f fixed.txt"},
                reviewer_engine=get_engine("claude-code-sonnet"),
                dispatch_attempts={},
                token=None,
                attribution=None,
                loop_run_id="100000000001",
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
            directive = await _tick_until_settled(
                main._RolloutLoopState(slots={0: mono.ws}),
                project={"name": "P", "gate_command": "test -f fixed.txt"},
                reviewer_engine=get_engine("claude-code-sonnet"),
                dispatch_attempts={},
                token=None,
                attribution=None,
                loop_run_id="100000000001",
            )
        assert directive == main._TICK_CONTINUE
        assert len(calls) == 2
        assert calls[1] is not None  # the second launch was the fix relaunch
        gtd.get_item.assert_awaited_once()
        gtd.complete_in_rollout.assert_awaited_once()


# ---------------------------------------------------------------------------
# Land rebase conflict: origin advanced between review and land
# ---------------------------------------------------------------------------

_BASE = "BASE = 0\n"
_LINE_A = "LINE_A = 1\n"  # origin's append, landed during review
_LINE_B = "LINE_B = 2\n"  # the squash commit's append
_BOTH = _BASE + _LINE_A + _LINE_B
_BOTH_GATE = "grep -q LINE_A registry.py && grep -q LINE_B registry.py"


def _git_continue(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *_GIT_ID, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "GIT_EDITOR": "true"},
    )


def _advance_during_review(
    seed: Path,
    clone: Path,
    ws: dispatch.RolloutWorkspace,
    *,
    name: str = "registry.py",
    base: str = _BASE,
    ours: str = _LINE_B,
    theirs: str = _LINE_A,
) -> tuple[dict[str, str], str]:
    """Seed *name*, approve a squash appending *ours*, then land *theirs*.

    Returns ``(pre_review_origin, origin tip after the advance)``.  Both sides
    append at the end of the same file, so the land's rebase conflicts.
    """
    _git(seed, "pull", "-q", "origin", "main")
    _write_or_delete(seed, name, base)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", f"seed {name}")
    _git(seed, "push", "-q", "origin", "main")
    _git(clone, "pull", "-q", "origin", "main")
    pre = _pre(ws)
    _squash(clone, filename=name, content=base + ours)
    _write_or_delete(seed, name, base + theirs)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", f"origin appends to {name}")
    _git(seed, "push", "-q", "origin", "main")
    return pre, _git(seed, "rev-parse", "HEAD")


def _resolver(
    clone: Path,
    *,
    name: str = "registry.py",
    content: str | None = _BOTH,
    report: tuple[str, ...] = ("registry.py",),
    rebase: bool = True,
    verdict: str = "merge",
    after=None,
):
    """A fake LAND CONFLICT MODE reviewer working in *clone*.

    Re-runs the rebase without fetching; on a merge verdict writes *content*
    (None keeps git's conflicted file, markers and all), stages it and
    continues.  *after* runs last (pushes from elsewhere, and so on).
    """

    async def fake(*_a, **_kw):
        if rebase:
            _git_continue(clone, "rebase", "origin/main", check=False)
            if verdict == "merge":
                if content is not None:
                    (clone / name).write_text(content)
                _git(clone, "add", name)
                _git_continue(clone, "rebase", "--continue")
        if after is not None:
            after()
        return ReviewVerdict(
            verdict,
            "kept both appends",
            "",
            "",
            resolved_conflict_paths=report if verdict == "merge" else (),
        )

    return fake


def _no_rebase_dirs(clone: Path) -> bool:
    return (
        not (clone / ".git" / "rebase-merge").exists()
        and not (clone / ".git" / "rebase-apply").exists()
    )


class TestLandRebaseConflict:
    async def test_keep_both_resolution_lands(self, mono, gtd, caplog) -> None:
        pre, advanced = _advance_during_review(mono.seed, mono.clone, mono.ws)
        with (
            patch.object(
                main, "_launch_reviewer", side_effect=_resolver(mono.clone)
            ) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert "(land conflict: kept both appends)" in result.rationale
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        assert _git(mono.origin, "rev-parse", "main~1") == advanced
        assert _git(mono.origin, "show", "main:registry.py") == _BOTH.strip()
        reviewer.assert_awaited_once()
        assert reviewer.await_args.kwargs["land_conflict"] == {
            mono.clone.name: ["registry.py"]
        }
        assert reviewer.await_args.kwargs.get("gate_failure_output") is None
        comment = gtd.post_comment.await_args.args[1]
        assert (
            f"Land rebase conflict ({mono.clone.name}): origin advanced by 1 "
            "commit(s) during review; worker found conflicts in registry.py; "
            "reviewer resolved keep-both: registry.py"
        ) in comment
        pushed = [line for line in _land_lines(caplog) if "outcome=pushed" in line]
        assert len(pushed) == 1
        assert (
            "conflict_relaunches=1 rebase_conflict=code "
            "rebase_conflict_code_paths=registry.py"
        ) in pushed[0]
        assert "decision=relaunch" in caplog.text
        assert "phase=land_rebase" in caplog.text
        assert "tip_ref=old-squash" in caplog.text
        verdicts = [
            r for r in caplog.records if "land-conflict-relaunch-verdict" in r.msg
        ]
        assert len(verdicts) == 1
        assert verdicts[0].levelno == logging.INFO
        assert "verdict=merge resolved=registry.py" in verdicts[0].getMessage()
        assert _no_rebase_dirs(mono.clone)

    async def test_lockfile_conflict_halts_without_relaunch(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws, name="uv.lock")
        before = _tip(mono.origin)
        reviewer = AsyncMock()
        with (
            patch.object(main, "_launch_reviewer", reviewer),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre)
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        assert _halt_reason(gtd) == (
            f"land rebase conflict for item {ITEM} in repo {mono.clone.name}: "
            "conflicting paths: uv.lock; lockfiles cannot be resolved by keeping "
            "both sides: uv.lock"
        )
        assert "decision=halt_lockfile" in caplog.text
        assert "outcome=rebase_conflict" in caplog.text
        assert _tip(mono.origin) == before

    async def test_code_conflict_without_gate_halts_without_relaunch(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        reviewer = AsyncMock()
        with (
            patch.object(main, "_launch_reviewer", reviewer),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=None)
        assert result == main._TICK_HALTED
        reviewer.assert_not_awaited()
        assert _halt_reason(gtd) == (
            f"land rebase conflict for item {ITEM} in repo {mono.clone.name}: "
            "conflicting paths: registry.py; code conflicts need a gate_command "
            "to verify the merged tree: registry.py"
        )
        assert "decision=halt_needs_gate" in caplog.text
        assert "gate=off" in caplog.text

    async def test_doc_conflict_without_gate_relaunches_and_lands(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws, name="notes.md")
        fake = _resolver(mono.clone, name="notes.md", report=("notes.md",))
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=None)
        assert isinstance(result, ReviewVerdict)
        reviewer.assert_awaited_once()
        assert "decision=relaunch" in caplog.text
        assert _git(mono.origin, "show", "main:notes.md") == _BOTH.strip()
        pushed = [line for line in _land_lines(caplog) if "outcome=pushed" in line]
        assert "rebase_conflict=doc rebase_conflict_code_paths=-" in pushed[0]

    async def test_markers_left_in_the_resolution_halt(self, mono, gtd, caplog) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        before = _tip(mono.origin)
        fake = _resolver(mono.clone, content=None)
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land refused for item {ITEM}")
        assert "conflict markers remain in: registry.py" in reason
        assert "outcome=conflict_markers" in caplog.text
        assert _tip(mono.origin) == before

    async def test_unreported_resolution_halts(self, mono, gtd, caplog) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        fake = _resolver(mono.clone, report=())
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd) == (
            f"land refused for item {ITEM}: repo {mono.clone.name}: the land "
            "rebase conflicts on paths the reviewer did not report resolving: "
            "registry.py"
        )
        assert "outcome=unreported_conflict" in caplog.text

    async def test_red_gate_after_resolution_halts_without_fix_mode(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        before = _tip(mono.origin)
        with (
            patch.object(
                main, "_launch_reviewer", side_effect=_resolver(mono.clone)
            ) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate="false")
        assert result == main._TICK_HALTED
        reviewer.assert_awaited_once()
        assert "gate_failure_output" not in reviewer.await_args.kwargs
        reason = _halt_reason(gtd)
        assert reason.startswith(
            f"land gate failed for item {ITEM} after keep-both conflict "
            "resolution in registry.py:"
        )
        assert "land-fix-relaunch-skipped" in caplog.text
        assert _tip(mono.origin) == before

    async def test_second_conflict_exhausts_the_budget(
        self, mono, gtd, caplog, tmp_path: Path
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        other = _clone(mono.origin, tmp_path / "other")
        marker = tmp_path / "raced"
        gate = (
            f"if [ ! -f {marker} ]; then touch {marker}; cd {other} && "
            "git pull -q origin main && echo 'LINE_C = 3' >> registry.py && "
            "git commit -qam race && git push -q origin main; fi; true"
        )
        with (
            patch.object(
                main, "_launch_reviewer", side_effect=_resolver(mono.clone)
            ) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=gate)
        assert result == main._TICK_HALTED
        reviewer.assert_awaited_once()
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land rebase conflict for item {ITEM}")
        assert reason.endswith("; land conflict relaunch budget exhausted (1/1)")
        assert "decision=halt_budget" in caplog.text
        assert "outcome=non_fast_forward" in caplog.text
        assert f"Item: {ITEM}" not in _git(mono.origin, "log", "--format=%B", "main")

    async def test_origin_advancing_again_during_the_relaunch_lands(
        self, mono, gtd, caplog, tmp_path: Path
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        other = _clone(mono.origin, tmp_path / "other")
        raced: list[str] = []

        def push_unrelated() -> None:
            raced.append(_commit_unrelated(other, "unrelated.txt"))

        fake = _resolver(mono.clone, after=push_unrelated)
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert isinstance(result, ReviewVerdict)
        reviewer.assert_awaited_once()
        gtd.halt_rollout.assert_not_awaited()
        assert "bad_local_state" not in caplog.text
        assert _git(mono.origin, "rev-parse", "main~1") == raced[0]
        assert f"Item: {ITEM}" in _git(mono.origin, "log", "-1", "--format=%B", "main")
        assert _git(mono.origin, "show", "main:registry.py") == _BOTH.strip()

    async def test_reviewer_pushing_the_resolution_halts(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)

        def push_it() -> None:
            _git(mono.clone, "push", "-q", "origin", "HEAD:main")

        fake = _resolver(mono.clone, after=push_it)
        with patch.object(main, "_launch_reviewer", side_effect=fake):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"commit for item {ITEM} appeared on origin during review"
        )

    async def test_merge_without_rebasing_halts(self, mono, gtd, caplog) -> None:
        pre, advanced = _advance_during_review(mono.seed, mono.clone, mono.ws)
        fake = _resolver(mono.clone, rebase=False)
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land bad local state for item {ITEM}")
        assert "rebase not completed onto origin/main" in reason
        assert f"expected {advanced}" in reason
        assert "check=parent_not_target" in caplog.text

    async def test_rebase_left_in_progress_on_merge_halts(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)

        async def leave_it(*_a, **_kw):
            _git_continue(mono.clone, "rebase", "origin/main", check=False)
            return ReviewVerdict(
                "merge", "done", "", "", resolved_conflict_paths=("registry.py",)
            )

        with (
            patch.object(main, "_launch_reviewer", side_effect=leave_it),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        assert "rebase not completed onto origin/main" in _halt_reason(gtd)
        assert "check=rebase_in_progress" in caplog.text

    async def test_halt_leaving_a_rebase_aborts_it(self, mono, gtd, caplog) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        squash = _git(mono.clone, "rev-parse", "HEAD")
        fake = _resolver(mono.clone, verdict="halt")
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert result == main._TICK_HALTED
        assert "outcome=land_conflict_not_resolved" in caplog.text
        assert _halt_reason(gtd).startswith(
            f"land rebase conflict for item {ITEM} in repo(s) {mono.clone.name}: "
            "the reviewer did not resolve it (halt): kept both appends"
        )
        assert _no_rebase_dirs(mono.clone)
        assert _git(mono.clone, "rev-parse", "HEAD") == squash

    async def test_two_repo_conflict_in_one_repo_lands(
        self, two_repo, gtd, caplog
    ) -> None:
        _git(two_repo.seed_b, "pull", "-q", "origin", "main")
        (two_repo.seed_b / "registry.py").write_text(_BASE)
        _git(two_repo.seed_b, "add", "-A")
        _git(two_repo.seed_b, "commit", "-qm", "seed registry")
        _git(two_repo.seed_b, "push", "-q", "origin", "main")
        _git(two_repo.clone_b, "pull", "-q", "origin", "main")
        pre = _pre(two_repo.ws)
        _squash(two_repo.clone_a)
        _squash(two_repo.clone_b, filename="registry.py", content=_BASE + _LINE_B)
        (two_repo.seed_b / "registry.py").write_text(_BASE + _LINE_A)
        _git(two_repo.seed_b, "commit", "-qam", "origin appends")
        _git(two_repo.seed_b, "push", "-q", "origin", "main")
        fake = _resolver(two_repo.clone_b, report=("repo-b/registry.py",))
        with (
            patch.object(main, "_launch_reviewer", side_effect=fake) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(two_repo.ws, pre, gate=f"cd repo-b && {_BOTH_GATE}")
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        reviewer.assert_awaited_once()
        assert reviewer.await_args.kwargs["land_conflict"] == {
            "repo-b": ["registry.py"]
        }
        for origin in (two_repo.origin_a, two_repo.origin_b):
            assert f"Item: {ITEM}" in _git(origin, "log", "-1", "--format=%B", "main")
        assert "repos=repo-b paths=repo-b/registry.py" in caplog.text
        comment = gtd.post_comment.await_args.args[1]
        assert (
            "Land rebase conflict (repo-b): origin advanced by 1 commit(s) during "
            "review; worker found conflicts in registry.py; reviewer resolved "
            "keep-both: registry.py"
        ) in comment

    async def test_recompute_mismatch_uses_the_union(self, mono, gtd, caplog) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        real = dispatch.land_conflict_paths
        calls: list[int] = []

        def extra(repo, base, tip, timeout):
            calls.append(1)
            paths = real(repo, base, tip, timeout)
            # The squash-phase guard runs first; only the land_rebase phase
            # recompute is skewed.
            return [*paths, "base.txt"] if len(calls) > 1 else paths

        with (
            patch.object(main, "_launch_reviewer", side_effect=_resolver(mono.clone)),
            patch.object(dispatch, "land_conflict_paths", side_effect=extra),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre, gate=_BOTH_GATE)
        assert isinstance(result, ReviewVerdict)
        mismatch = [
            r for r in caplog.records if "land-conflict-recompute-mismatch" in r.msg
        ]
        assert len(mismatch) == 1
        assert mismatch[0].levelno == logging.WARNING
        assert (
            "rebase_paths=registry.py recomputed=registry.py,base.txt"
            in mismatch[0].getMessage()
        )
        comment = gtd.post_comment.await_args.args[1]
        assert "worker found conflicts in base.txt, registry.py;" in comment
        assert "(mismatch: unreported base.txt)" in comment


class TestLandRebaseConflictUnits:
    def _lander(self, mono, *, gate: str = "true") -> main._Lander:
        lander = main._Lander(
            ROLLOUT,
            ITEM,
            _verdict(),
            project={"name": "P", "gate_command": gate},
            workspace=mono.ws,
            pre_review_origin=mono.pre,
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="1",
            token=None,
            attribution=None,
        )
        lander.landing = [mono.clone.name]
        return lander

    def test_decision_order(self, mono) -> None:
        repo = mono.clone.name
        lander = self._lander(mono)
        fails = [(repo, ["uv.lock", "a.py"], "")]
        assert lander._rebase_conflict_decision(fails)[0] == "halt_lockfile"
        assert lander._rebase_conflict_decision([(repo, [], "x")])[0] == (
            "halt_no_paths"
        )
        lander.conflict_relaunches = 1
        assert lander._rebase_conflict_decision(fails)[0] == "halt_budget"
        lander.pushed = {repo: "abc"}
        assert lander._rebase_conflict_decision(fails) == (
            "halt_already_pushed",
            repo,
            "",
        )
        nogate = self._lander(mono, gate="")
        assert nogate._rebase_conflict_decision([(repo, ["a.py"], "")])[0] == (
            "halt_needs_gate"
        )
        assert nogate._rebase_conflict_decision([(repo, ["a.md"], "")])[0] == (
            "relaunch"
        )

    async def test_launch_reviewer_rejects_both_modes(self, mono) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            await main._launch_reviewer(
                ROLLOUT,
                ITEM,
                project={},
                workspace=mono.ws,
                reviewer_engine=get_engine("claude-code-sonnet"),
                token=None,
                attribution=None,
                gate_failure_output="boom",
                land_conflict={"r": ["a.py"]},
            )

    async def test_abort_failure_only_warns(self, mono, caplog) -> None:
        lander = self._lander(mono)
        with (
            patch.object(
                dispatch, "land_rebase_in_progress", side_effect=OSError("nope")
            ),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await lander._abort_leftover_rebase(mono.clone.name)
        assert "land-conflict-abort-failed" in caplog.text

    def test_rebase_helpers(self, mono) -> None:
        assert dispatch.land_rebase_in_progress(mono.clone, 30) == []
        assert dispatch.land_rebase_abort(mono.clone, 30) != ""

    async def test_abort_moving_head_is_an_invariant_violation(
        self, mono, gtd, caplog
    ) -> None:
        pre, _ = _advance_during_review(mono.seed, mono.clone, mono.ws)
        real = dispatch.land_rebase

        def moving(repo, branch, timeout):
            result = real(repo, branch, timeout)
            _git(repo, "commit", "--allow-empty", "-qm", "sneaky")
            return result

        with (
            patch.object(dispatch, "land_rebase", side_effect=moving),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, pre)
        assert result == main._TICK_HALTED
        assert "HEAD moved across an aborted rebase" in _halt_reason(gtd)
        assert "kind=rebase_abort_moved_head" in caplog.text


# ---------------------------------------------------------------------------
# Board-held per-project land lease
# ---------------------------------------------------------------------------

_LEASE_CLEAN = (
    "Land lease: waited 0s behind 0 holder(s); reacquires: 0; "
    "rejected while lease held: 0"
)


def _acquired(lease_id: str = "L1") -> dict:
    return {"acquired": True, "lease": {"lease_id": lease_id}}


def _held(rollout: str = "R2", item: str = "I2", lease_id: str = "H1") -> dict:
    return {
        "acquired": False,
        "holder": {
            "lease_id": lease_id,
            "rollout_id": rollout,
            "item_id": item,
            "expires_at": "2026-11-02T00:00:00+00:00",
            "renewed_at": "2026-11-01T23:59:00+00:00",
        },
    }


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("board down")


def _counting_gate(tmp_path: Path) -> tuple[str, Path]:
    runs = tmp_path / "gate-runs.log"
    return f"echo run >> {runs}; true", runs


def _gate_runs(runs: Path) -> int:
    return len(runs.read_text().split()) if runs.exists() else 0


def _steps(gtd) -> list[str]:
    return [
        str(c.kwargs.get("current_step") or "")
        for c in gtd.update_rollout_state.await_args_list
    ]


@pytest.fixture
def fast_lease(monkeypatch):
    monkeypatch.setattr(main, "LAND_LEASE_POLL_SECONDS", 0)


class TestLandLease:
    async def test_green_path_acquires_before_fetch_and_releases(
        self, mono, gtd
    ) -> None:
        _squash(mono.clone)
        order: list[str] = []
        real_fetch = dispatch.land_fetch

        def fetch(*a, **kw):
            order.append("fetch")
            return real_fetch(*a, **kw)

        async def acquire(*_a, **_kw):
            order.append("acquire")
            return _acquired()

        gtd.acquire_land_lease.side_effect = acquire
        with patch.object(dispatch, "land_fetch", side_effect=fetch):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        assert order[0] == "acquire"
        assert order.count("acquire") == 1
        kw = gtd.acquire_land_lease.await_args.kwargs
        assert gtd.acquire_land_lease.await_args.args == (ROLLOUT,)
        assert kw["item_id"] == ITEM
        assert kw["loop_run_id"] == "100000000001"
        assert kw["ttl_seconds"] == main._land_lease_ttl()
        gtd.release_land_lease.assert_awaited_once()
        assert gtd.release_land_lease.await_args.kwargs["lease_id"] == "L1"
        comment = gtd.post_comment.await_args.args[1]
        assert _LEASE_CLEAN in comment
        # The pre-push confirm renewed the held lease.
        assert gtd.renew_land_lease.await_args.kwargs["lease_id"] == "L1"

    async def test_lease_wait_does_not_consume_push_attempts(
        self, mono, gtd, fast_lease, caplog
    ) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_held(), _held(), _acquired()]
        gtd.get_rollout = AsyncMock(return_value={"status": "running"})
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        comment = gtd.post_comment.await_args.args[1]
        assert "push attempts: 1" in comment
        assert "behind 1 holder(s)" in comment
        assert any(
            "waiting for land lease held by rollout R2 (item I2)" in s
            for s in _steps(gtd)
        )
        waiting = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("rollout-loop: land-lease waiting ")
        ]
        assert len(waiting) == 1
        assert "holder_rollout_id=R2 holder_item_id=I2" in waiting[0]
        assert "rollout-loop: land-lease acquired rollout_id=" in caplog.text
        assert "rollout-loop: land-lease released rollout_id=" in caplog.text
        assert gtd.get_rollout.await_count == 3

    async def test_wait_long_warning(self, mono, gtd, fast_lease, monkeypatch, caplog):
        _squash(mono.clone)
        monkeypatch.setattr(main, "LAND_LEASE_WAIT_WARN_SECONDS", 1e-9)
        gtd.acquire_land_lease.side_effect = [_held(), _acquired()]
        gtd.get_rollout = AsyncMock(return_value={"status": "running"})
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        assert "rollout-loop: land-lease wait-long rollout_id=" in caplog.text
        assert "holder_lease_id=H1" in caplog.text
        assert any(s.startswith("Land: waiting 0m for land lease") for s in _steps(gtd))

    async def test_lost_lease_before_push_regates_under_new_lease(
        self, mono, gtd, tmp_path: Path, caplog
    ) -> None:
        _squash(mono.clone)
        gate, runs = _counting_gate(tmp_path)
        gtd.acquire_land_lease.side_effect = [_acquired("L1"), _acquired("L2")]
        gtd.renew_land_lease.side_effect = [
            {"renewed": False},
            {"renewed": True, "lease": {"lease_id": "L2"}},
        ]
        pushes = MagicMock(side_effect=dispatch.land_push)
        with (
            patch.object(dispatch, "land_push", pushes),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        assert _gate_runs(runs) == 2
        assert pushes.call_count == 1
        comment = gtd.post_comment.await_args.args[1]
        assert "push attempts: 1" in comment
        assert "reacquires: 1" in comment
        renew_ids = [c.kwargs["lease_id"] for c in gtd.renew_land_lease.await_args_list]
        assert renew_ids == ["L1", "L2"]
        assert gtd.release_land_lease.await_args.kwargs["lease_id"] == "L2"
        assert "land-lease reacquire rollout_id=" in caplog.text
        assert "reason=lost" in caplog.text

    async def test_renew_error_same_lease_pushes_without_regate(
        self, mono, gtd, tmp_path: Path, caplog
    ) -> None:
        _squash(mono.clone)
        gate, runs = _counting_gate(tmp_path)
        gtd.renew_land_lease.side_effect = [_connect_error()]
        pushes = MagicMock(side_effect=dispatch.land_push)
        with (
            patch.object(dispatch, "land_push", pushes),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        assert _gate_runs(runs) == 1
        assert pushes.call_count == 1
        assert gtd.acquire_land_lease.await_count == 2
        assert "reason=renew_error_same_lease" in caplog.text
        assert "reacquires: 0" in gtd.post_comment.await_args.args[1]

    async def test_renew_error_new_lease_regates(
        self, mono, gtd, tmp_path: Path, caplog
    ) -> None:
        _squash(mono.clone)
        gate, runs = _counting_gate(tmp_path)
        gtd.acquire_land_lease.side_effect = [_acquired("L1"), _acquired("L2")]
        gtd.renew_land_lease.side_effect = [
            _connect_error(),
            {"renewed": True, "lease": {"lease_id": "L2"}},
        ]
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        assert _gate_runs(runs) == 2
        assert "reason=renew_error " in caplog.text
        comment = gtd.post_comment.await_args.args[1]
        assert "reacquires: 1" in comment
        assert "push attempts: 1" in comment

    async def test_halt_releases_lease_after_halting(
        self, mono, gtd, monkeypatch
    ) -> None:
        _squash(mono.clone)
        monkeypatch.setattr(main, "MAX_LAND_FIX_RELAUNCHES", 0)
        calls: list[str] = []

        async def halt(*_a, **_kw):
            calls.append("halt")

        async def release(*_a, **_kw):
            calls.append("release")
            return {"released": True}

        gtd.halt_rollout.side_effect = halt
        gtd.release_land_lease.side_effect = release
        result = await _land(mono.ws, mono.pre, gate="false")
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(f"land gate failed for item {ITEM}")
        assert calls == ["halt", "release"]
        assert gtd.release_land_lease.await_args.kwargs["lease_id"] == "L1"

    async def test_release_failure_is_logged_not_raised(
        self, mono, gtd, monkeypatch, caplog
    ) -> None:
        _squash(mono.clone)
        monkeypatch.setattr(main, "MAX_LAND_FIX_RELAUNCHES", 0)
        gtd.release_land_lease.side_effect = _connect_error()
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate="false")
        assert result == main._TICK_HALTED
        assert "rollout-loop: land-lease release-failed rollout_id=" in caplog.text

    async def test_non_transient_acquire_error_halts(self, mono, gtd) -> None:
        _squash(mono.clone)
        before = _tip(mono.origin)
        request = httpx.Request("POST", "http://board/api/x")
        gtd.acquire_land_lease.side_effect = httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land lease acquire failed for item {ITEM}: HTTPStatusError"
        )
        assert _tip(mono.origin) == before
        gtd.release_land_lease.assert_not_awaited()

    async def test_transient_acquire_error_is_retried(
        self, mono, gtd, fast_lease
    ) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_connect_error(), _acquired()]
        result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        gtd.halt_rollout.assert_not_awaited()
        assert gtd.acquire_land_lease.await_count == 2

    async def test_persistent_board_outage_halts(
        self, mono, gtd, fast_lease, monkeypatch
    ) -> None:
        _squash(mono.clone)
        monkeypatch.setattr(config, "ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS", 0)
        gtd.acquire_land_lease.side_effect = _connect_error()
        result = await _land(mono.ws, mono.pre)
        assert result == main._TICK_HALTED
        reason = _halt_reason(gtd)
        assert reason.startswith(f"land lease acquire failed for item {ITEM}")
        assert "board unreachable for" in reason

    async def _assert_aborted(
        self, mono, gtd, caplog, directive: str, *, source: str, observed: str
    ) -> None:
        before = _tip(mono.origin)
        fetch = MagicMock(side_effect=dispatch.land_fetch)
        push = MagicMock(side_effect=dispatch.land_push)
        gate = MagicMock(side_effect=dispatch.run_gate_command)
        probe = main._LandProbe()
        with (
            patch.object(dispatch, "land_fetch", fetch),
            patch.object(dispatch, "land_push", push),
            patch.object(dispatch, "run_gate_command", gate),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            result = await _land(mono.ws, mono.pre, probe=probe)
        assert result == directive
        assert probe.aborted == directive
        assert probe.lander_returned is True
        fetch.assert_not_called()
        push.assert_not_called()
        gate.assert_not_called()
        gtd.release_land_lease.assert_not_awaited()
        gtd.halt_rollout.assert_not_awaited()
        gtd.post_comment.assert_not_awaited()
        phases = [c.args[1] for c in gtd.update_rollout_state.await_args_list]
        assert "halted" not in phases
        assert "land_lease_error" not in caplog.text
        aborted = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("rollout-loop: land-lease wait-aborted ")
        ]
        assert len(aborted) == 1
        assert (
            f"rollout_id={ROLLOUT} item_id={ITEM} source={source} "
            f"observed={observed} directive={directive} waited_secs="
        ) in aborted[0]
        assert _tip(mono.origin) == before

    @pytest.mark.parametrize(
        ("status", "directive"),
        [
            ("halted", main._TICK_HALTED),
            ("cancelled", main._TICK_DONE),
            ("completed", main._TICK_DONE),
        ],
    )
    async def test_board_status_aborts_pre_lease_wait(
        self, mono, gtd, caplog, status: str, directive: str
    ) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_held()]
        gtd.get_rollout = AsyncMock(return_value={"status": status})
        await self._assert_aborted(
            mono, gtd, caplog, directive, source="poll", observed=status
        )
        assert "holders_seen=0" in caplog.text
        assert gtd.acquire_land_lease.await_count == 0

    async def test_cancelled_rollout_aborts_wait(self, mono, gtd, caplog) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_held()]
        gtd.get_rollout = AsyncMock(return_value={"status": "cancelled"})
        await self._assert_aborted(
            mono, gtd, caplog, main._TICK_DONE, source="poll", observed="cancelled"
        )
        assert gtd.acquire_land_lease.await_count == 0

    @pytest.mark.parametrize(
        ("refused", "directive"),
        [
            ("rollout_halted", main._TICK_HALTED),
            ("rollout_cancelled", main._TICK_DONE),
            ("rollout_completed", main._TICK_DONE),
        ],
    )
    async def test_refused_acquire_aborts(
        self, mono, gtd, caplog, refused: str, directive: str
    ) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [{"acquired": False, "refused": refused}]
        await self._assert_aborted(
            mono, gtd, caplog, directive, source="refused", observed=refused
        )
        assert gtd.acquire_land_lease.await_args.kwargs["continuing_land"] is False

    async def test_superseded_loop_aborts_wait(
        self, mono, gtd, monkeypatch, caplog
    ) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_held()]
        gtd.get_rollout = AsyncMock(
            return_value={"status": "running", "loop_remote_run_id": "OTHER-LOOP"}
        )
        monkeypatch.setattr(main.db, "list_runs_by_rollout", AsyncMock(return_value=[]))
        await self._assert_aborted(
            mono,
            gtd,
            caplog,
            main._TICK_SUPERSEDED,
            source="poll",
            observed="superseded",
        )

    def _bare_lander(self, mono) -> main._Lander:
        return main._Lander(
            ROLLOUT,
            ITEM,
            _verdict(),
            project={"name": "P"},
            workspace=mono.ws,
            pre_review_origin=mono.pre,
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="100000000001",
            token=None,
            attribution=None,
        )

    async def test_refused_after_push_still_halts(self, mono, gtd) -> None:
        lander = self._bare_lander(mono)
        lander.pushed = {"repo": "abc"}
        gtd.acquire_land_lease.side_effect = [
            {"acquired": False, "refused": "rollout_halted"}
        ]
        with pytest.raises(main._LandHalt) as info:
            await lander._acquire_lease()
        assert gtd.acquire_land_lease.await_args.kwargs["continuing_land"] is True
        result = await lander._halt(info.value)
        assert result == main._TICK_HALTED
        assert _halt_reason(gtd).startswith(
            f"land aborted for item {ITEM}: rollout halted while waiting for "
            "the land lease"
        )

    async def test_continuing_land_reacquires_while_halted(
        self, mono, gtd, fast_lease
    ) -> None:
        lander = self._bare_lander(mono)
        lander.lease_ever_acquired = True
        gtd.get_rollout = AsyncMock(return_value={"status": "halted"})
        gtd.acquire_land_lease.side_effect = [_held(), _acquired("L2")]
        try:
            lease_id = await lander._acquire_lease()
        finally:
            await lander._stop_lease_renewal()
        assert lease_id == "L2"
        assert lander.lease_id == "L2"
        for call in gtd.acquire_land_lease.await_args_list:
            assert call.kwargs["continuing_land"] is True
        gtd.halt_rollout.assert_not_awaited()

    async def test_phases_wait_then_merging(self, mono, gtd, fast_lease) -> None:
        _squash(mono.clone)
        gtd.acquire_land_lease.side_effect = [_held(), _held("R3"), _acquired()]
        gtd.get_rollout = AsyncMock(return_value={"status": "running"})
        result = await _land(mono.ws, mono.pre)
        assert isinstance(result, ReviewVerdict)
        calls = gtd.update_rollout_state.await_args_list
        phases = [c.args[1] for c in calls]
        first_merging = phases.index("merging")
        assert first_merging > 0
        assert set(phases[:first_merging]) == {"waiting_for_lease"}
        assert (
            calls[first_merging].kwargs["current_step"]
            == f"Land: lease acquired for {ITEM}"
        )
        assert "waiting_for_lease" not in phases[first_merging:]

    async def test_probe_tracks_lander(self, mono, gtd) -> None:
        _squash(mono.clone)
        probe = main._LandProbe()
        seen: list[tuple[object, bool]] = []

        async def acquire(*_a, **_kw):
            seen.append((probe.lander, probe.lander_returned))
            return _acquired()

        gtd.acquire_land_lease.side_effect = acquire
        result = await _land(mono.ws, mono.pre, probe=probe)
        assert isinstance(result, ReviewVerdict)
        assert isinstance(seen[0][0], main._Lander)
        assert seen[0][1] is False
        assert probe.lander is seen[0][0]
        assert probe.lander_returned is True
        assert probe.aborted is None

    async def test_cancel_during_gate_releases_lease(self, mono, gtd) -> None:
        import threading

        _squash(mono.clone)
        entered = threading.Event()
        release = threading.Event()

        def _gate(
            *_a, tmp_dir=None, log_context="", log_dir=None, log_name_prefix="gate"
        ):
            entered.set()
            release.wait(10)
            return dispatch.GateResult(0, False, "", 0.0)

        try:
            with patch.object(dispatch, "run_gate_command", side_effect=_gate):
                task = asyncio.create_task(_land(mono.ws, mono.pre))
                await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        gtd.release_land_lease.assert_awaited_once()
        assert gtd.release_land_lease.await_args.kwargs["lease_id"] == "L1"

    async def test_push_rejected_while_held(
        self, mono, gtd, tmp_path: Path, caplog
    ) -> None:
        other = _clone(mono.origin, tmp_path / "other")
        marker = tmp_path / "raced"
        gate = (
            f"if [ ! -f {marker} ]; then touch {marker}; cd {other} && "
            "echo r > race.txt && git add -A && git commit -qm race && "
            "git push -q origin main; fi; true"
        )
        _squash(mono.clone)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            result = await _land(mono.ws, mono.pre, gate=gate)
        assert isinstance(result, ReviewVerdict)
        held = [
            r
            for r in caplog.records
            if "land-lease push-rejected-while-held" in r.getMessage()
        ]
        assert len(held) == 1
        assert held[0].levelno == logging.WARNING
        assert "lease_id=L1 repo=repo push_round=1" in held[0].getMessage()
        comment = gtd.post_comment.await_args.args[1]
        assert "rejected while lease held: 1" in comment
        assert "push attempts: 2" in comment
        gtd.release_land_lease.assert_awaited_once()

    async def test_lands_without_gate_command_take_the_lease(self, mono, gtd) -> None:
        _squash(mono.clone)
        result = await _land(mono.ws, mono.pre, gate=None)
        assert isinstance(result, ReviewVerdict)
        gtd.acquire_land_lease.assert_awaited_once()
        gtd.release_land_lease.assert_awaited_once()


class TestLeaseRenewTask:
    def _lander(self, mono) -> main._Lander:
        return main._Lander(
            ROLLOUT,
            ITEM,
            _verdict(),
            project={"name": "P"},
            workspace=mono.ws,
            pre_review_origin=mono.pre,
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="100000000001",
            token=None,
            attribution=None,
        )

    async def test_renew_errors_then_ok_then_lost(
        self, mono, gtd, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(main, "LAND_LEASE_RENEW_SECONDS", 0)
        lander = self._lander(mono)
        lander.lease_id = "L1"
        failures_seen: list[int] = []

        async def renew(*_a, **kw):
            failures_seen.append(lander.lease_renew_failures)
            n = len(failures_seen)
            if n <= 2:
                raise _connect_error()
            if n == 3:
                return {"renewed": True, "lease": {"lease_id": "L1"}}
            return {"renewed": False}

        gtd.renew_land_lease.side_effect = renew
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await asyncio.wait_for(lander._renew_lease("L1", asyncio.Event()), 5)
        assert failures_seen == [0, 1, 2, 0]
        assert lander.lease_lost is True
        assert "consecutive_failures=2" in caplog.text
        assert "rollout-loop: land-lease renew-error rollout_id=" in caplog.text
        assert "rollout-loop: land-lease lost rollout_id=" in caplog.text
        assert all(
            c.kwargs["lease_id"] == "L1" for c in gtd.renew_land_lease.await_args_list
        )

    async def test_lost_stale_lease_does_not_flag_current(
        self, mono, gtd, monkeypatch
    ) -> None:
        monkeypatch.setattr(main, "LAND_LEASE_RENEW_SECONDS", 0)
        lander = self._lander(mono)
        lander.lease_id = "L2"
        gtd.renew_land_lease.return_value = {"renewed": False}
        await asyncio.wait_for(lander._renew_lease("L1", asyncio.Event()), 5)
        assert lander.lease_lost is False

    async def test_stop_ends_renewal(self, mono, gtd) -> None:
        lander = self._lander(mono)
        lander._start_lease_renewal("L1")
        await lander._stop_lease_renewal()
        assert lander.lease_renew_task is None
        gtd.renew_land_lease.assert_not_awaited()


class TestLandIsPreLease:
    def _active(self, probe: main._LandProbe | None) -> main._ActiveLand:
        return main._ActiveLand("I", 0, MagicMock(), 0.0, probe=probe)

    def _lander(self, **attrs: object) -> SimpleNamespace:
        base: dict[str, object] = {
            "lease_ever_acquired": False,
            "lease_id": None,
            "pushed": {},
            "in_acquire_call": False,
        }
        base.update(attrs)
        return SimpleNamespace(**base)

    @pytest.mark.parametrize(
        ("attrs", "expected"),
        [
            ({}, True),
            ({"lease_ever_acquired": True}, False),
            ({"lease_id": "L1", "lease_ever_acquired": True}, False),
            ({"pushed": {"repo": "abc"}}, False),
            ({"in_acquire_call": True}, False),
        ],
    )
    def test_lander_rows(self, attrs: dict[str, object], expected: bool) -> None:
        probe = main._LandProbe(lander=self._lander(**attrs))  # type: ignore[arg-type]
        assert main._land_is_pre_lease(self._active(probe)) is expected

    def test_no_probe(self) -> None:
        assert main._land_is_pre_lease(self._active(None)) is False

    def test_no_lander_yet(self) -> None:
        assert main._land_is_pre_lease(self._active(main._LandProbe())) is True

    def test_lander_returned(self) -> None:
        probe = main._LandProbe(lander=self._lander(), lander_returned=True)  # type: ignore[arg-type]
        assert main._land_is_pre_lease(self._active(probe)) is False


_DRAIN = "host 'h' is draining: deploy"


def _draining() -> dict:
    return {"status": "running", "loop_host_drain_reason": _DRAIN}


def _running() -> dict:
    return {"status": "running"}


class TestDrainHold:
    def _lander(self, mono) -> main._Lander:
        return main._Lander(
            ROLLOUT,
            ITEM,
            _verdict(),
            project={"name": "P"},
            workspace=mono.ws,
            pre_review_origin=mono.pre,
            reviewer_engine=get_engine("claude-code-sonnet"),
            loop_run_id="100000000001",
            token=None,
            attribution=None,
        )

    async def _acquire(self, lander: main._Lander) -> str:
        try:
            return await lander._acquire_lease()
        finally:
            await lander._stop_lease_renewal()

    async def test_hold_then_undrain_acquires(
        self, mono, gtd, fast_lease, caplog
    ) -> None:
        lander = self._lander(mono)
        seen: list[int] = []

        async def get_rollout(*_a, **_kw):
            seen.append(gtd.acquire_land_lease.await_count)
            return [_draining(), _draining(), _draining(), _running()][len(seen) - 1]

        gtd.get_rollout = AsyncMock(side_effect=get_rollout)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            assert await self._acquire(lander) == "L1"
        assert seen == [0, 0, 0, 0]
        gtd.acquire_land_lease.assert_awaited_once()
        assert gtd.acquire_land_lease.await_args.kwargs["continuing_land"] is False
        expected = (
            f"Land: host draining ({_DRAIN}); not acquiring the land lease for {ITEM}"
        )
        assert _steps(gtd).count(expected) == 1
        assert caplog.text.count("land-lease drain-hold rollout_id=") == 1
        assert caplog.text.count("land-lease drain-released") == 1
        assert "drain_polls=3" in caplog.text
        assert "continuing=False" in caplog.text

    async def test_two_episodes_with_holder_between(self, mono, gtd, fast_lease):
        lander = self._lander(mono)
        gtd.get_rollout = AsyncMock(
            side_effect=[
                _draining(),
                _draining(),
                _running(),
                _draining(),
                _running(),
                _running(),
            ]
        )
        gtd.acquire_land_lease.side_effect = [_held(), _held(), _acquired()]
        await self._acquire(lander)
        steps = _steps(gtd)
        drain_step = (
            f"Land: host draining ({_DRAIN}); not acquiring the land lease for {ITEM}"
        )
        waiting = "waiting for land lease held by rollout R2 (item I2)"
        assert steps.count(drain_step) == 2
        assert sum(waiting in s for s in steps) == 2

    async def test_lease_holder_never_reads_board(self, mono, gtd) -> None:
        lander = self._lander(mono)
        lander.lease_id = "L1"
        lander.lease_ever_acquired = True
        gtd.get_rollout = AsyncMock(return_value=_draining())
        assert await lander._lease_wait_abort_check() is None
        gtd.get_rollout.assert_not_awaited()

    async def test_continuing_reacquire_not_held(self, mono, gtd, fast_lease) -> None:
        lander = self._lander(mono)
        lander.pushed = {"repo": "abc"}
        gtd.get_rollout = AsyncMock(return_value=_draining())
        await self._acquire(lander)
        gtd.get_rollout.assert_not_awaited()
        assert gtd.acquire_land_lease.await_args.kwargs["continuing_land"] is True

    async def test_halted_wins_over_drain(self, mono, gtd) -> None:
        lander = self._lander(mono)
        gtd.get_rollout = AsyncMock(
            return_value={"status": "halted", "loop_host_drain_reason": _DRAIN}
        )
        with pytest.raises(main._LeaseWaitAborted) as info:
            await lander._acquire_lease()
        assert info.value.directive == main._TICK_HALTED
        assert gtd.acquire_land_lease.await_count == 0

    async def test_transient_error_absorbed(self, mono, gtd, fast_lease) -> None:
        lander = self._lander(mono)
        gtd.get_rollout = AsyncMock(side_effect=[_connect_error(), _running()])
        assert await self._acquire(lander) == "L1"

    async def test_drain_poll_resets_outage(
        self, mono, gtd, fast_lease, monkeypatch
    ) -> None:
        lander = self._lander(mono)
        clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 2000.0, 2000.0])
        last = [0.0]

        def mono_clock() -> float:
            last[0] = next(clock, last[0])
            return last[0]

        monkeypatch.setattr(main, "_monotonic", mono_clock)
        monkeypatch.setattr(config, "ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS", 100)
        gtd.get_rollout = AsyncMock(
            side_effect=[
                _connect_error(),
                _draining(),
                _draining(),
                _connect_error(),
                _running(),
            ]
        )
        assert await self._acquire(lander) == "L1"
        gtd.halt_rollout.assert_not_awaited()

    async def test_drain_held_land_counts_as_pre_lease(
        self, mono, gtd, fast_lease
    ) -> None:
        lander = self._lander(mono)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def get_rollout(*_a, **_kw):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await release.wait()
                return _running()
            return _draining()

        gtd.get_rollout = AsyncMock(side_effect=get_rollout)
        task = asyncio.create_task(self._acquire(lander))
        await asyncio.wait_for(entered.wait(), 5)
        active = main._ActiveLand(
            ITEM, 0, task, 0.0, probe=main._LandProbe(lander=lander)
        )
        assert main._land_is_pre_lease(active) is True
        assert lander.in_acquire_call is False
        assert gtd.acquire_land_lease.await_count == 0
        release.set()
        assert await asyncio.wait_for(task, 5) == "L1"

    async def test_drain_hold_long_warning(
        self, mono, gtd, fast_lease, monkeypatch, caplog
    ) -> None:
        lander = self._lander(mono)
        monkeypatch.setattr(main, "LAND_LEASE_WAIT_WARN_SECONDS", 1e-9)
        gtd.get_rollout = AsyncMock(
            side_effect=[_draining(), _draining(), _draining(), _running()]
        )
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            await self._acquire(lander)
        assert caplog.text.count("land-lease drain-hold-long rollout_id=") >= 1
        assert any(s.startswith("Land: held ") for s in _steps(gtd))
