"""Tests for retention.py — verdict-free evidence capture and age-based pruning."""

from __future__ import annotations

import inspect
import json
import logging
import os
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


class TestCaptureEvidence:
    def test_two_repo_fixture_produces_patch_with_both_headers(self, tmp_path) -> None:
        workspace = config.WORKSPACE_ROOT / "ws-run1"
        workspace.mkdir(parents=True)
        (workspace / "transcript.txt").write_text("hello transcript")
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
        assert json.loads((target / "completion.json").read_text()) == {
            "disposition": "done"
        }
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
        assert (target / "patch.diff").read_text() == ""

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
        assert seen[0][:4] == ["sudo", "-u", "dispatch", "-H"]
        assert "git" in seen[0]

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
        assert seen[0][0] == "git"


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
