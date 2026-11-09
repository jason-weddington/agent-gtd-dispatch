"""Shared pytest fixtures for the whole tests/ tree."""

from __future__ import annotations

import os
from pathlib import Path
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
def _isolate_inherited_agent_path() -> Iterator[None]:
    # A build agent's shell exports the dispatch kill-guard shim dir (and the
    # agent's ~/.local/bin, ~/.cargo/bin) on PATH. Leaving it there makes the
    # suite non-hermetic in two ways: the guard dir is no longer "prepended
    # first" by prepend_agent_bin_dirs (so PATH-order assertions fail), and
    # real-process kill tests resolve `pkill` to the shim, which refuses every
    # pattern kill by design. Strip only the kill-guard dir — the one entry that
    # changes behavior — so the suite behaves the same on a build agent host as
    # on a clean CI runner. Restored by hand, like _isolate_git_env.
    guard = Path.home() / ".local" / "share" / "agent-gtd-dispatch" / "kill-guard"
    saved = os.environ.get("PATH", "")
    entries = [entry for entry in saved.split(":") if entry and entry != str(guard)]
    os.environ["PATH"] = ":".join(entries)
    yield
    os.environ["PATH"] = saved


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


@pytest.fixture(autouse=True)
def _isolate_teardown_registries() -> Iterator[None]:
    # `_tearing_down_tasks` / `_teardown_started` are process globals read by
    # every /info, /health and _restart_blocking_counts() call, so a single
    # leaked entry would make `restart_blocking_runs` order-dependent across the
    # suite.  Restored by hand rather than through `monkeypatch`, for the same
    # teardown-order reason documented on _isolate_git_env above.
    from agent_gtd_dispatch import main

    saved_tasks = main._tearing_down_tasks
    saved_started = main._teardown_started
    main._tearing_down_tasks = {}
    main._teardown_started = {}
    yield
    main._tearing_down_tasks = saved_tasks
    main._teardown_started = saved_started


@pytest.fixture(autouse=True)
def _isolate_evidence_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    # Gate logs land under config.EVIDENCE_ROOT; keep them off the real home.
    # Restored by hand, like _isolate_run_tmp_root.
    from agent_gtd_dispatch import config

    saved = config.EVIDENCE_ROOT
    config.EVIDENCE_ROOT = tmp_path_factory.mktemp("run-evidence")
    yield
    config.EVIDENCE_ROOT = saved


@pytest.fixture(autouse=True)
def _no_real_session_reap(request: pytest.FixtureRequest) -> Iterator[None]:
    # Every agent/gate/talos exit path now reaps its session, which runs real
    # pkill (and sudo in two-user mode).  Mocked tests must not do that, so the
    # reap is a MagicMock unless the test opts out with @pytest.mark.real_reap.
    # Restored by hand, not via `monkeypatch`, for the same teardown-order
    # reason as _isolate_git_env.
    if request.node.get_closest_marker("real_reap") is not None:
        yield
        return
    from unittest.mock import MagicMock

    from agent_gtd_dispatch import dispatch, main

    saved_dispatch = dispatch.reap_run_session
    saved_main = main.reap_run_session
    saved_marker = dispatch.reap_marker_processes
    dispatch.reap_run_session = MagicMock(return_value="clean")  # type: ignore[assignment]
    main.reap_run_session = MagicMock(return_value="clean")  # type: ignore[assignment]
    dispatch.reap_marker_processes = MagicMock(  # type: ignore[assignment]
        return_value=dispatch.MarkerReap(
            reaped=[], survivors=0, scan_ok=True, escaped_session=0
        )
    )
    yield
    dispatch.reap_run_session = saved_dispatch  # type: ignore[assignment]
    main.reap_run_session = saved_main  # type: ignore[assignment]
    dispatch.reap_marker_processes = saved_marker  # type: ignore[assignment]
