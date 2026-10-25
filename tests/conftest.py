"""Shared pytest fixtures for the whole tests/ tree."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# GIT_* variables that affect remotes/prompting, not repo selection.
_KEEP_GIT_ENV = frozenset({"GIT_SSH_COMMAND", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS"})


def strip_git_env() -> dict[str, str]:
    """Remove every inherited GIT_* variable except the remote-related ones.

    Fail closed: any GIT_* name is removed unless explicitly allowed, so a
    hook-run suite (git exports GIT_DIR etc. to hooks) cannot make test
    subprocesses operate on the outer repository. Returns the removed values.
    """
    removed = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("GIT_") and name not in _KEEP_GIT_ENV
    }
    for name in removed:
        del os.environ[name]
    return removed


@pytest.fixture(autouse=True)
def _isolate_git_env() -> Iterator[None]:
    # Deliberately not built on the `monkeypatch` fixture: requesting it here
    # would change its teardown order relative to other fixtures that tests
    # patch with it. Restore the environment by hand instead.
    removed = strip_git_env()
    yield
    os.environ.update(removed)
