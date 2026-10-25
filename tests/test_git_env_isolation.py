"""Regression: a hook-run suite must not let test git calls hit the outer repo."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from tests.conftest import strip_git_env
from tests.test_dispatch import _make_local_git_repo

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_autouse_fixture_removed_repo_selection_vars() -> None:
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        assert name not in os.environ


def test_fixture_keeps_remote_vars_and_strips_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -x")
    monkeypatch.setenv("GIT_FUTURE_THING", "1")
    strip_git_env()
    assert os.environ["GIT_SSH_COMMAND"] == "ssh -x"
    assert "GIT_FUTURE_THING" not in os.environ


def test_hook_env_does_not_leak_into_outer_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    assert _git(outer, "init").returncode == 0
    # Simulate the hook environment, then let the fixture logic strip it.
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    strip_git_env()

    _make_local_git_repo(tmp_path / "inner")

    config = (outer / ".git" / "config").read_text()
    assert "bare = true" not in config
    assert "[user]" not in config
    assert "name = " not in config
