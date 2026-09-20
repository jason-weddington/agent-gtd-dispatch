"""Tests for retention.py — verdict-free evidence capture and age-based pruning."""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, retention


@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "DISPATCH_EVIDENCE_ROOT": str(tmp_path / "evidence"),
        "DISPATCH_AGENT_SUBPROCESS_USER": "",
    }
    (tmp_path / "workspace").mkdir()
    with patch.dict(os.environ, env):
        config.load()
        yield


# ---------------------------------------------------------------------------
# verdict-free property
# ---------------------------------------------------------------------------


FORBIDDEN = ("RunStatus", "already_satisfied", "succeeded", "PushStatus", "passed")


def test_retention_is_verdict_free() -> None:
    """The retention path must never see, accept, or branch on a verdict.

    Both checks are string-based: the module uses ``from __future__ import
    annotations``, so annotations are plain strings at runtime and identity checks
    against the real types are impossible.
    """
    source = Path(retention.__file__).read_text()
    for needle in FORBIDDEN:
        assert needle not in source, f"{needle!r} leaked into retention.py"

    for name, obj in vars(retention).items():
        if name.startswith("_") or not callable(obj):
            continue
        if getattr(obj, "__module__", None) != retention.__name__:
            continue
        annotations = inspect.get_annotations(obj, eval_str=False)
        for ann in annotations.values():
            assert "RunStatus" not in str(ann), f"{name} annotation mentions RunStatus"


# ---------------------------------------------------------------------------
# capture_evidence
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_repo(root: Path, name: str) -> tuple[Path, str]:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "f.txt").write_text("base\nCHANGED-LINE\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base


def _make_repo_full(root: Path, name: str) -> tuple[Path, str]:
    """A repo exhibiting all three change shapes relative to ``base``.

    Committed (a second commit after base), uncommitted-tracked (a modification
    to a tracked file, never staged) and untracked (a brand new file never
    ``git add``-ed) — the exact combination the talos failure shape needs
    covered, since talos work is uncommitted and often adds new modules.
    """
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    (repo / "tracked.txt").write_text("tracked-base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # committed change
    (repo / "f.txt").write_text("base\nCOMMITTED-LINE\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "committed change")
    # uncommitted modification to a tracked file — never staged
    (repo / "tracked.txt").write_text("tracked-base\nUNCOMMITTED-LINE\n")
    # brand new untracked file — never git-added
    (repo / "new_module.py").write_text("NEW-UNTRACKED-CONTENT\n")
    return repo, base


class TestCaptureEvidence:
    def test_two_repo_fixture_produces_patch_with_both_headers(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run1"
        workspace.mkdir(parents=True)
        (workspace / "transcript.txt").write_text("hello transcript")
        # An agent may still volunteer a `.dispatch/completion.json`; nothing asks
        # for one and evidence capture deliberately ignores it. Written here so the
        # assertion below pins that it is NOT copied — the completion-artifact
        # contract was deleted, and a capture path left behind is how it grows back.
        (workspace / ".dispatch").mkdir()
        (workspace / ".dispatch" / "completion.json").write_text(
            json.dumps({"disposition": "done"})
        )
        repo_a, base_a = _make_repo(workspace, "repo_a")
        repo_b, base_b = _make_repo(workspace, "repo_b")

        target = retention.capture_evidence(
            "run1", workspace, [("repo_a", repo_a, base_a), ("repo_b", repo_b, base_b)]
        )

        assert (target / "transcript.txt").read_text() == "hello transcript"
        assert not (target / "completion.json").exists()
        patch_text = (target / "patch.diff").read_text()
        assert "# repo: repo_a" in patch_text
        assert "# repo: repo_b" in patch_text
        assert "CHANGED-LINE" in patch_text

    def test_no_repos_still_captures_transcript(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run2"
        workspace.mkdir(parents=True)
        (workspace / "transcript.txt").write_text("t")
        target = retention.capture_evidence("run2", workspace, [])
        assert (target / "transcript.txt").exists()
        # No repos means no "# repo:" chunks, but the header is unconditional.
        patch_text = (target / "patch.diff").read_text()
        assert "# dispatch_run_id: run2" in patch_text
        assert "# repo:" not in patch_text

    def test_missing_transcript_does_not_raise(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run3"
        workspace.mkdir(parents=True)
        target = retention.capture_evidence("run3", workspace, [])
        assert not (target / "transcript.txt").exists()

    def test_none_workspace_does_not_raise(self) -> None:
        target = retention.capture_evidence("run4", None, [])
        assert target.is_dir()

    def test_none_base_sha_records_a_reason_and_continues(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run5"
        workspace.mkdir(parents=True)
        target = retention.capture_evidence(
            "run5", workspace, [("repo_a", workspace / "repo_a", None)]
        )
        patch_text = (target / "patch.diff").read_text()
        assert "# repo: repo_a" in patch_text
        assert "# no diff captured" in patch_text

    def test_raising_diff_helper_does_not_raise_out(
        self, tmp_path, monkeypatch
    ) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run6"
        workspace.mkdir(parents=True)
        (workspace / "transcript.txt").write_text("kept")

        def _boom(repo_path, base_sha):
            msg = "git exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(retention, "repo_diff", _boom)
        target = retention.capture_evidence(
            "run6", workspace, [("repo_a", workspace / "repo_a", "abc123")]
        )
        assert (target / "transcript.txt").read_text() == "kept"
        assert "git exploded" in (target / "patch.diff").read_text()

    def test_logs_capture_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run7"
        workspace.mkdir(parents=True)
        (workspace / "transcript.txt").write_text("t")
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.retention"):
            retention.capture_evidence("run7", workspace, [])
        assert "evidence captured: run_id=run7" in caplog.text

    def test_diff_command_goes_through_sudo_wrap(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        seen: list[list[str]] = []

        class _Result:
            returncode = 0
            stdout = b""
            stderr = b""

        def _fake_run(argv, **kwargs):
            seen.append(argv)
            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        retention.repo_diff(Path("/srv/repo"), "abc")
        # add -A, diff --cached, and the throwaway-index cleanup — all cross-user.
        assert len(seen) == 3
        for argv in seen:
            assert argv[:4] == ["sudo", "-u", "dispatch", "-H"]
        # add/diff go through the bash wrapper that sets GIT_INDEX_FILE inline,
        # since sudo would otherwise strip an env var not in env_keep.
        assert seen[0][4] == "bash"
        assert "add" in seen[0] and "-A" in seen[0]
        assert seen[1][4] == "bash"
        assert "diff" in seen[1] and "--cached" in seen[1] and "abc" in seen[1]
        assert seen[2][4:6] == ["rm", "-f"]

    def test_diff_command_has_no_sudo_prefix_when_unset(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        seen: list[list[str]] = []

        class _Result:
            returncode = 0
            stdout = b""
            stderr = b""

        def _fake_run(argv, **kwargs):
            seen.append(argv)
            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        retention.repo_diff(Path("/srv/repo"), "abc")
        assert len(seen) == 3
        assert seen[0][0] == "bash"
        assert seen[1][0] == "bash"
        assert seen[2][:2] == ["rm", "-f"]

    def test_captures_uncommitted_untracked_and_committed_changes(
        self, tmp_path
    ) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run8"
        workspace.mkdir(parents=True)
        repo, base = _make_repo_full(workspace, "repo_a")

        target = retention.capture_evidence("run8", workspace, [("repo_a", repo, base)])

        patch_text = (target / "patch.diff").read_text()
        assert "COMMITTED-LINE" in patch_text
        assert "UNCOMMITTED-LINE" in patch_text
        assert "NEW-UNTRACKED-CONTENT" in patch_text

        # The real index/working tree must be untouched by capture.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "?? new_module.py" in status  # still untracked, never staged
        assert " M tracked.txt" in status  # still an unstaged modification

    def test_no_change_repo_produces_empty_body_and_warning(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run9"
        workspace.mkdir(parents=True)
        repo, _base = _make_repo(workspace, "repo_a")
        # _make_repo leaves one committed change past base; reset base to HEAD
        # so this repo has NO difference of any kind from its "base".
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.retention"):
            target = retention.capture_evidence(
                "run9", workspace, [("repo_a", repo, head)]
            )

        patch_text = (target / "patch.diff").read_text()
        assert "# repo: repo_a" in patch_text
        assert "essentially empty patch" in caplog.text
        assert "run_id=run9" in caplog.text
        assert "repo=repo_a" in caplog.text

    def test_header_carries_both_ids_and_utc_timestamp(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run10"
        workspace.mkdir(parents=True)

        target = retention.capture_evidence(
            "run10", workspace, [], agent_gtd_run_id="gtd-abc123"
        )

        patch_text = (target / "patch.diff").read_text()
        assert "# dispatch_run_id: run10" in patch_text
        assert "# agent_gtd_run_id: gtd-abc123" in patch_text
        assert re.search(
            r"# captured_at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", patch_text
        )

    def test_header_says_agent_gtd_run_id_unknown_when_not_supplied(
        self, tmp_path
    ) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run11"
        workspace.mkdir(parents=True)

        target = retention.capture_evidence("run11", workspace, [])

        patch_text = (target / "patch.diff").read_text()
        assert "# agent_gtd_run_id: unknown" in patch_text

    def test_headered_patch_applies_cleanly(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run12"
        workspace.mkdir(parents=True)
        repo, base = _make_repo_full(workspace, "repo_a")

        target = retention.capture_evidence(
            "run12", workspace, [("repo_a", repo, base)]
        )
        patch_text = (target / "patch.diff").read_text()

        scratch = tmp_path / "scratch"
        subprocess.run(
            ["git", "clone", "-q", str(repo), str(scratch)],
            check=True,
            capture_output=True,
        )
        _git(scratch, "checkout", "-q", base)

        result = subprocess.run(
            ["git", "apply", "-"],
            input=patch_text,
            text=True,
            cwd=scratch,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr

        assert (scratch / "f.txt").read_text() == "base\nCOMMITTED-LINE\n"
        assert (scratch / "tracked.txt").read_text() == (
            "tracked-base\nUNCOMMITTED-LINE\n"
        )
        assert (scratch / "new_module.py").read_text() == "NEW-UNTRACKED-CONTENT\n"


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class TestPrune:
    def test_active_workspaces_kept_across_all_three_naming_shapes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        active_id = "aaaabbbbcccc"
        stale_id = "ddddeeeeffff"
        root = config.WORKSPACE_ROOT
        keep_repo = root / f"myrepo-{active_id}"
        keep_ws = root / f"ws-{active_id}"
        drop = root / f"repos-{stale_id}"
        for d in (keep_repo, keep_ws, drop):
            d.mkdir(parents=True)
            (d / "big.bin").write_bytes(b"0" * 128)
            _age(d, 10)

        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.retention"):
            retention.prune(frozenset({active_id}))

        assert keep_repo.is_dir()
        assert keep_ws.is_dir()
        assert not drop.exists()
        assert "skipped active-run path" in caplog.text

    def test_fresh_workspace_is_kept(self) -> None:
        root = config.WORKSPACE_ROOT
        fresh = root / "repos-freshfresh11"
        fresh.mkdir(parents=True)
        retention.prune(frozenset())
        assert fresh.is_dir()

    def test_evidence_age_and_active_protection(self) -> None:
        active_id = "111122223333"
        eroot = config.EVIDENCE_ROOT
        eroot.mkdir(parents=True, exist_ok=True)
        keep_active = eroot / active_id
        keep_fresh = eroot / "444455556666"
        drop_old = eroot / "777788889999"
        for d in (keep_active, keep_fresh, drop_old):
            d.mkdir(parents=True)
            (d / "transcript.txt").write_text("x")
        _age(keep_active, 90)
        _age(drop_old, 90)

        retention.prune(frozenset({active_id}))

        assert keep_active.is_dir()
        assert keep_fresh.is_dir()
        assert not drop_old.exists()

    def test_tick_summary_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.retention"):
            retention.prune(frozenset())
        assert "retention: tick" in caplog.text
        assert "workspaces_pruned=" in caplog.text
        assert "bytes_freed=" in caplog.text

    def test_missing_roots_do_not_raise(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "nope")
        monkeypatch.setattr(config, "EVIDENCE_ROOT", tmp_path / "also-nope")
        retention.prune(frozenset())


class TestRetentionConfig:
    def test_defaults(self) -> None:
        assert config.EVIDENCE_RETENTION_DAYS == 30
        assert config.WORKSPACE_RETENTION_HOURS == 48
        assert config.RETENTION_INTERVAL_SECONDS == 3600

    def test_evidence_root_defaults_beside_workspace_root(self) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "sk-ant",
            "DISPATCH_WORKSPACE_ROOT": "/srv/agent/workspace",
        }
        with patch.dict(os.environ, env):
            os.environ.pop("DISPATCH_EVIDENCE_ROOT", None)
            config.load()
            assert Path("/srv/agent/run-evidence") == config.EVIDENCE_ROOT
