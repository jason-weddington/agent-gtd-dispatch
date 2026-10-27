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


@pytest.fixture(autouse=True)
def _isolate_run_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    # A run-tmp root OUTSIDE each test's tmp_path, so tests that set
    # DISPATCH_WORKSPACE_ROOT=tmp_path never trip config's overlap guard, and
    # no test touches ~/run-tmp or tmp_path.parent/run-tmp.
    #
    # Restored by hand rather than through `monkeypatch`, for the same reason
    # as _isolate_git_env: an autouse fixture requesting `monkeypatch` sets it
    # up before every other fixture, so its undo would run AFTER theirs (e.g.
    # test_api's `client` would shut the app down while a test's patched
    # `_active_processes` was still in place).
    from agent_gtd_dispatch import config, run_tmp

    root = tmp_path_factory.mktemp("run-tmp")
    saved_env = os.environ.get("DISPATCH_RUN_TMP_ROOT")
    saved_root = config.RUN_TMP_ROOT
    os.environ["DISPATCH_RUN_TMP_ROOT"] = str(root)
    config.RUN_TMP_ROOT = root
    with run_tmp._LOCK:
        run_tmp._LIVE.clear()
    yield
    with run_tmp._LOCK:
        run_tmp._LIVE.clear()
    config.RUN_TMP_ROOT = saved_root
    if saved_env is None:
        os.environ.pop("DISPATCH_RUN_TMP_ROOT", None)
    else:
        os.environ["DISPATCH_RUN_TMP_ROOT"] = saved_env
