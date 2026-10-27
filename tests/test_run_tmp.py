"""Private per-run TMPDIR (run_tmp.py) and its wiring into every launch path."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, db, dispatch, gates, main, run_tmp, talos
from agent_gtd_dispatch.dispatch import GateResult
from agent_gtd_dispatch.engines import CLAUDE, TALOS_SONNET
from agent_gtd_dispatch.models import DispatchMode, Run, RunStatus
from agent_gtd_dispatch.run_tmp import with_tmp_env

_REQUIRED_ENV = {
    "DISPATCH_API_KEY": "test-key",
    "AGENT_GTD_URL": "http://localhost:9999",
    "AGENT_GTD_API_KEY": "test-gtd-key",
}


@pytest.fixture
def _worker_env(tmp_path, monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("DISPATCH_AGENT_SUBPROCESS_USER", raising=False)
    monkeypatch.delenv("DISPATCH_EVIDENCE_ROOT", raising=False)
    config.load()


def _age(path: Path, seconds: int) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# with_tmp_env / _sudo_wrap (AC-2, AC-3)
# ---------------------------------------------------------------------------


class TestWithTmpEnv:
    def test_none_is_identity(self) -> None:
        cmd = ["claude", "--print", "x"]
        assert with_tmp_env(cmd, None) == cmd

    def test_prefix_is_exact(self, tmp_path) -> None:
        p = tmp_path / "rt-x"
        assert with_tmp_env(["a", "b"], p) == [
            "/usr/bin/env",
            f"TMPDIR={p}",
            f"TMP={p}",
            f"TEMP={p}",
            "a",
            "b",
        ]

    def test_never_carries_anthropic_key(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-secret")
        out = with_tmp_env(["x"], tmp_path)
        assert not any("ANTHROPIC_API_KEY" in part for part in out)
        assert not any(
            "ANTHROPIC_API_KEY" in part for part in with_tmp_env(["x"], None)
        )

    def test_env_prefix_sits_inside_the_sudo_wrap(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        p = tmp_path / "rt-x"
        assert dispatch._sudo_wrap(with_tmp_env(["x"], p)) == [
            "sudo",
            "-u",
            "dispatch",
            "-H",
            "/bin/bash",
            "-c",
            dispatch._UMASK_SHIM,
            "agent-gtd-umask",
            "/usr/bin/env",
            f"TMPDIR={p}",
            f"TMP={p}",
            f"TEMP={p}",
            "x",
        ]


# ---------------------------------------------------------------------------
# config (AC-4)
# ---------------------------------------------------------------------------


class TestConfig:
    def test_module_defaults(self) -> None:
        assert config.RUN_TMP_ORPHAN_MIN_AGE_SECONDS >= 0

    def test_default_is_sibling_of_workspace_root(self, tmp_path, monkeypatch) -> None:
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        ws = tmp_path / "ws"
        monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(ws))
        monkeypatch.delenv("DISPATCH_RUN_TMP_ROOT")
        monkeypatch.delenv("DISPATCH_EVIDENCE_ROOT", raising=False)
        monkeypatch.delenv("DISPATCH_RUN_TMP_ORPHAN_MIN_AGE_SECONDS", raising=False)
        config.load()
        assert ws.parent / "run-tmp" == config.RUN_TMP_ROOT
        assert config.RUN_TMP_ORPHAN_MIN_AGE_SECONDS == 600

    def test_env_override(self, tmp_path, monkeypatch) -> None:
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("DISPATCH_RUN_TMP_ROOT", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("DISPATCH_RUN_TMP_ORPHAN_MIN_AGE_SECONDS", "30")
        config.load()
        assert tmp_path / "elsewhere" == config.RUN_TMP_ROOT
        assert config.RUN_TMP_ORPHAN_MIN_AGE_SECONDS == 30

    @pytest.mark.parametrize(
        "case",
        [
            "eq_workspace",
            "inside_workspace",
            "workspace_parent",
            "inside_evidence",
            "home",
            "tmp",
        ],
    )
    def test_guard_rejects(self, case, tmp_path, monkeypatch) -> None:
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        ws = tmp_path / "ws"
        ev = tmp_path / "ev"
        monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("DISPATCH_EVIDENCE_ROOT", str(ev))
        root = {
            "eq_workspace": ws,
            "inside_workspace": ws / "run-tmp",
            "workspace_parent": tmp_path,
            "inside_evidence": ev / "run-tmp",
            "home": Path.home(),
            "tmp": Path("/tmp"),  # noqa: S108
        }[case]
        monkeypatch.setenv("DISPATCH_RUN_TMP_ROOT", str(root))
        with pytest.raises(ValueError, match="DISPATCH_RUN_TMP_ROOT"):
            config.load()

    def test_guard_rejects_negative_min_age(self, tmp_path, monkeypatch) -> None:
        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("DISPATCH_RUN_TMP_ORPHAN_MIN_AGE_SECONDS", "-1")
        with pytest.raises(ValueError, match="MIN_AGE"):
            config.load()


# ---------------------------------------------------------------------------
# create_run_tmp (AC-5)
# ---------------------------------------------------------------------------


class TestCreate:
    def test_single_user_creates_and_registers(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        path = run_tmp.create_run_tmp("aaaaaaaaaaab")
        assert path == config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        assert path.is_dir()
        assert path in run_tmp.live_paths()

    def test_land_key(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        key = "land-aaaaaaaaaaab-996b308e-1111-2222-3333-444455556666"
        assert run_tmp.create_run_tmp(key).name == f"rt-{key}"

    def test_two_user_uses_sudo_mkdir(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        with patch("agent_gtd_dispatch.run_tmp.subprocess.run") as run:
            path = run_tmp.create_run_tmp("aaaaaaaaaaab")
        run.assert_called_once_with(
            dispatch._sudo_wrap(["mkdir", "-p", str(path)]), check=True
        )
        assert path in run_tmp.live_paths()

    def test_failure_unregisters_and_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        with (
            patch(
                "agent_gtd_dispatch.run_tmp.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "mkdir"),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            run_tmp.create_run_tmp("aaaaaaaaaaab")
        assert config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab" not in run_tmp.live_paths()

    @pytest.mark.parametrize("key", ["", "a/b", ".", "..", "workspace"])
    def test_invalid_key(self, key) -> None:
        with pytest.raises(ValueError, match="invalid run tmp key"):
            run_tmp.create_run_tmp(key)


# ---------------------------------------------------------------------------
# remove_run_tmp (AC-6)
# ---------------------------------------------------------------------------


class TestRemove:
    def test_removes_nested_tree(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        path = run_tmp.create_run_tmp("aaaaaaaaaaab")
        (path / "a" / "b").mkdir(parents=True)
        (path / "a" / "b" / "f").write_text("x")
        run_tmp.remove_run_tmp(path, kind="build")
        assert not path.exists()
        assert path not in run_tmp.live_paths()

    def test_logs_bytes(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        path = run_tmp.create_run_tmp("aaaaaaaaaaab")
        (path / "f").write_bytes(b"0123456789")
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.run_tmp"):
            run_tmp.remove_run_tmp(path, kind="build")
        removed = [r for r in caplog.records if "run-tmp: removed" in r.getMessage()]
        assert len(removed) == 1
        msg = removed[0].getMessage()
        assert "key=aaaaaaaaaaab" in msg
        assert "kind=build" in msg
        assert "bytes=10" in msg

    def test_refuses_outside_root(self, tmp_path) -> None:
        outside = tmp_path / "rt-aaaaaaaaaaab"
        outside.mkdir()
        run_tmp.remove_run_tmp(outside, kind="build")
        assert outside.is_dir()

    def test_refuses_root_itself(self) -> None:
        root = config.RUN_TMP_ROOT
        (root / "keep").write_text("x")
        run_tmp.remove_run_tmp(root, kind="orphan")
        assert (root / "keep").exists()

    def test_refuses_non_rt_child(self) -> None:
        child = config.RUN_TMP_ROOT / "workspace"
        child.mkdir()
        run_tmp.remove_run_tmp(child, kind="orphan")
        assert child.is_dir()

    def test_two_user_failure_logs_error_and_never_raises(
        self, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        path = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        path.mkdir()
        with (
            patch(
                "agent_gtd_dispatch.run_tmp.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1),
            ) as run,
            patch("agent_gtd_dispatch.run_tmp.shutil.rmtree"),
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.run_tmp"),
        ):
            run_tmp.remove_run_tmp(path, kind="build")
        run.assert_called_once_with(
            dispatch._sudo_wrap(["rm", "-rf", str(path)]), check=False
        )
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "removal FAILED key=aaaaaaaaaaab rm_rc=1" in errors[0].getMessage()
        assert path.exists()

    def test_never_raises_on_unexpected_error(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        path = run_tmp.create_run_tmp("aaaaaaaaaaab")
        with patch(
            "agent_gtd_dispatch.retention._dir_size", side_effect=RuntimeError("x")
        ):
            run_tmp.remove_run_tmp(path, kind="build")
        assert path not in run_tmp.live_paths()


# ---------------------------------------------------------------------------
# sweep_orphans (AC-7)
# ---------------------------------------------------------------------------


class TestSweep:
    def _rt(self, key: str, *, age: int) -> Path:
        path = config.RUN_TMP_ROOT / f"rt-{key}"
        path.mkdir()
        _age(path, age)
        return path

    def test_missing_root_is_zero(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "RUN_TMP_ROOT", tmp_path / "missing")
        assert run_tmp.sweep_orphans(frozenset(), 0, context="startup") == 0

    def test_old_orphan_removed(self) -> None:
        old = self._rt("aaaaaaaaaaab", age=3600)
        assert run_tmp.sweep_orphans(frozenset(), 600, context="startup") == 1
        assert not old.exists()

    def test_live_kept_at_any_age(self) -> None:
        live = self._rt("aaaaaaaaaaab", age=10**6)
        assert run_tmp.sweep_orphans(frozenset({live}), 0, context="startup") == 0
        assert live.is_dir()

    def test_fresh_kept_under_min_age(self) -> None:
        fresh = self._rt("aaaaaaaaaaab", age=0)
        assert run_tmp.sweep_orphans(frozenset(), 600, context="periodic") == 0
        assert fresh.is_dir()

    def test_zero_min_age_removes_every_non_live(self) -> None:
        a = self._rt("aaaaaaaaaaab", age=0)
        b = self._rt("land-aaaaaaaaaaab-item-1", age=0)
        assert run_tmp.sweep_orphans(frozenset(), 0, context="startup") == 2
        assert not a.exists()
        assert not b.exists()

    def test_non_rt_names_survive(self) -> None:
        root = config.RUN_TMP_ROOT
        for name in ("workspace", "run-evidence", ".claude"):
            (root / name).mkdir()
            _age(root / name, 10**6)
        (root / "rt-file").write_text("x")
        run_tmp.sweep_orphans(frozenset(), 0, context="startup")
        for name in ("workspace", "run-evidence", ".claude", "rt-file"):
            assert (root / name).exists()

    def test_periodic_warns_per_orphan(self, caplog) -> None:
        self._rt("aaaaaaaaaaab", age=3600)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.run_tmp"):
            run_tmp.sweep_orphans(frozenset(), 600, context="periodic")
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "orphan swept (teardown missed it) key=aaaaaaaaaaab" in w for w in warnings
        )
        assert "run-tmp: tick orphans_swept=1 live=0" in caplog.text

    def test_startup_summary_line(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.run_tmp"):
            run_tmp.sweep_orphans(frozenset(), 0, context="startup")
        assert "run-tmp: startup sweep removed=0" in caplog.text


# ---------------------------------------------------------------------------
# Launch argv plumbing (AC-9, AC-16)
# ---------------------------------------------------------------------------


class TestLaunchArgv:
    async def test_run_agent_argv_and_env(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        tmp = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        proc = MagicMock()
        proc.returncode = 0
        proc.wait.return_value = 0
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", return_value=proc
        ) as popen:
            await dispatch.run_agent(CLAUDE, tmp_path, "sys", "T", 5, tmp_dir=tmp)
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        assert argv[:4] == [
            "/usr/bin/env",
            f"TMPDIR={tmp}",
            f"TMP={tmp}",
            f"TEMP={tmp}",
        ]
        assert argv[4] == "claude"
        assert not any("ANTHROPIC_API_KEY" in part for part in argv)
        assert "ANTHROPIC_API_KEY" not in env

    async def test_run_agent_without_tmp_dir_unchanged(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        proc = MagicMock()
        proc.returncode = 0
        proc.wait.return_value = 0
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", return_value=proc
        ) as popen:
            await dispatch.run_agent(CLAUDE, tmp_path, "sys", "T", 5)
        assert popen.call_args.args[0][0] == "claude"

    def test_run_gate_command_argv(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        tmp = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        proc = MagicMock()
        proc.returncode = 0
        proc.wait.return_value = 0
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", return_value=proc
        ) as popen:
            dispatch.run_gate_command(tmp_path, "make gate", 30, CLAUDE, tmp_dir=tmp)
        argv = popen.call_args.args[0]
        assert argv[:8] == dispatch._sudo_wrap([])
        assert argv[8:13] == [
            "/usr/bin/env",
            f"TMPDIR={tmp}",
            f"TMP={tmp}",
            f"TEMP={tmp}",
            "/bin/bash",
        ]
        assert argv[-1] == "make gate"
        assert "ANTHROPIC_API_KEY" not in popen.call_args.kwargs["env"]

    def test_run_as_agent_and_run_gate_steps(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        tmp = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        step = gates.GateStep(
            repo_label="r",
            repo_path=tmp_path,
            manager=gates.HookManager.OVERRIDE,
            step=".agent-gtd/setup",
            argv=["./setup"],
            marker="",
        )
        with patch(
            "agent_gtd_dispatch.gates.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b""),
        ) as run:
            assert gates.run_gate_steps([step], 5, run_id="r", tmp_dir=tmp) is None
        assert run.call_args.args[0] == with_tmp_env(["./setup"], tmp)

    def test_build_talos_argv(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        tmp = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        plain = talos.build_talos_argv(tmp_path, "t", 1)
        wrapped = talos.build_talos_argv(tmp_path, "t", 1, tmp_dir=tmp)
        inner = plain[8:]
        assert wrapped == dispatch._sudo_wrap(with_tmp_env(inner, tmp))

    def test_commit_with_retry_wraps_only_commits(self, tmp_path) -> None:
        tmp = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        calls: list[list[str]] = []
        results = iter(
            [
                subprocess.CompletedProcess([], 1, stdout=b"", stderr=b""),
                subprocess.CompletedProcess([], 0, stdout=b" M f\n", stderr=b""),
                subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
                subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            ]
        )

        def _run(argv, **_kw):
            calls.append(argv)
            return next(results)

        with patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_run):
            rc = main._commit_with_retry(str(tmp_path), [], "msg", tmp_dir=tmp)
        assert rc.returncode == 0
        commits = [c for c in calls if "commit" in c]
        others = [c for c in calls if "commit" not in c]
        assert len(commits) == 2
        for c in commits:
            assert c[:4] == with_tmp_env([], tmp)
        for c in others:
            assert c[0] == "git"


# ---------------------------------------------------------------------------
# _dispatch_worker teardown (AC-8, AC-13, AC-14)
# ---------------------------------------------------------------------------


def _completed(rc: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = rc
    return m


class _Worker:
    """Drive _dispatch_worker with the dispatch module mocked wholesale."""

    def __init__(self, tmp_path: Path, item: str) -> None:
        self.tmp_path = tmp_path
        self.item = item
        self.seen: dict[str, Any] = {}

    async def make_run(self) -> Run:
        await db.init_db()
        run = Run(
            item_id=self.item,
            project_name="TestProject",
            branch_name=f"feat/{self.item}",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        return run

    def install(self, mock_gtd, mock_dispatch, run: Run) -> Path:
        from tests.test_post_run_gate import _default_project, _install_common_mocks

        ws = self.tmp_path / f"repos-testproj-{self.item}"
        ws.mkdir()
        _install_common_mocks(
            mock_gtd,
            mock_dispatch,
            item_id=self.item,
            project=_default_project(),
            fake_workspace=ws,
        )
        mock_gtd.set_item_status = AsyncMock()

        def _gate(*args, tmp_dir=None):
            self.seen["gate_tmp_dir"] = tmp_dir
            self.seen["gate_args"] = args
            return GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=1.0
            )

        mock_dispatch.run_gate_command = MagicMock(side_effect=_gate)
        return ws

    def recording_agent(self, outcome: Any) -> AsyncMock:
        async def _agent(*_a, tmp_dir=None, **_kw):
            self.seen["tmp_dir"] = tmp_dir
            self.seen["was_dir"] = tmp_dir is not None and tmp_dir.is_dir()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return AsyncMock(side_effect=_agent)


@pytest.mark.usefixtures("_worker_env")
class TestWorkerTeardown:
    async def _drive(self, tmp_path, item, outcome, *, push_results=None, rescue=None):
        from tests.test_post_run_gate import _pushed

        w = _Worker(tmp_path, item)
        run = await w.make_run()
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            w.install(mock_gtd, mock_dispatch, run)
            mock_dispatch.run_agent = w.recording_agent(outcome)
            mock_dispatch.verify_pushes = MagicMock(
                return_value=push_results or [_pushed(branch=f"feat/{item}")]
            )
            if rescue is not None:
                mock_dispatch.rescue_abandoned_work = MagicMock(return_value=rescue)
            await main._dispatch_worker(run, 50, CLAUDE, 600)
        expected = config.RUN_TMP_ROOT / f"rt-{run.id}"
        assert w.seen["tmp_dir"] == expected
        assert w.seen["was_dir"] is True
        assert not expected.exists()
        assert expected not in run_tmp.live_paths()
        updated = await db.get_run(run.id)
        assert updated is not None
        return updated, w, mock_dispatch, expected

    async def test_exit_zero_and_gate_gets_same_tmp_dir(self, tmp_path) -> None:
        updated, w, _md, expected = await self._drive(tmp_path, "i-ok", _completed(0))
        assert updated.status == RunStatus.succeeded
        assert w.seen["gate_tmp_dir"] == expected
        # Positional order unchanged: (workspace, cmd, timeout, engine, cb).
        assert isinstance(w.seen["gate_args"][2], int)
        assert len(w.seen["gate_args"]) == 5

    async def test_nonzero_exit(self, tmp_path) -> None:
        updated, *_ = await self._drive(tmp_path, "i-nz", _completed(1))
        assert updated.status == RunStatus.failed

    async def test_timeout(self, tmp_path) -> None:
        # The status depends on the linger-success rule; only teardown matters.
        await self._drive(tmp_path, "i-to", subprocess.TimeoutExpired("claude", 1))

    async def test_generic_exception(self, tmp_path) -> None:
        updated, *_ = await self._drive(tmp_path, "i-ex", RuntimeError("boom"))
        assert updated.status == RunStatus.failed

    async def test_rescue_failed_retains_workspace_but_not_tmp(self, tmp_path) -> None:
        rescue = dispatch.RescueResult(
            "repos-testproj",
            attempted=True,
            pushed=False,
            committed=True,
            error="git push failed: no route to host",
        )
        _updated, _w, mock_dispatch, _expected = await self._drive(
            tmp_path, "i-rs", _completed(1), rescue=rescue
        )
        mock_dispatch.cleanup_workspace.assert_not_called()
        assert (tmp_path / "repos-testproj-i-rs").is_dir()

    async def test_talos_gets_tmp_dir(self, tmp_path) -> None:
        w = _Worker(tmp_path, "i-talos")
        run = await w.make_run()
        seen: dict[str, Any] = {}

        def _argv(*_a, tmp_dir=None, **_kw):
            seen["tmp_dir"] = tmp_dir
            seen["was_dir"] = tmp_dir is not None and tmp_dir.is_dir()
            msg = "stop here"
            raise RuntimeError(msg)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch.object(talos, "serialize_task_spec", return_value="{}"),
            patch.object(talos, "build_talos_argv", side_effect=_argv),
        ):
            w.install(mock_gtd, mock_dispatch, run)
            await main._dispatch_worker(run, 50, TALOS_SONNET, 600)
        expected = config.RUN_TMP_ROOT / f"rt-{run.id}"
        assert seen["tmp_dir"] == expected
        assert seen["was_dir"] is True
        assert not expected.exists()

    @pytest.mark.parametrize("how", ["sudo_mkdir", "oserror"])
    async def test_create_failure_fails_run_before_agent(
        self, tmp_path, monkeypatch, how
    ) -> None:
        w = _Worker(tmp_path, f"i-cf-{how}")
        run = await w.make_run()
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            contextlib.ExitStack() as stack,
        ):
            w.install(mock_gtd, mock_dispatch, run)
            mock_dispatch.run_agent = AsyncMock()
            if how == "sudo_mkdir":
                monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
                stack.enter_context(
                    patch(
                        "agent_gtd_dispatch.run_tmp.subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, "mkdir"),
                    )
                )
            else:
                stack.enter_context(
                    patch.object(
                        run_tmp, "create_run_tmp", side_effect=OSError("disk full")
                    )
                )
            await main._dispatch_worker(run, 50, CLAUDE, 600)
        mock_dispatch.run_agent.assert_not_called()
        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status == RunStatus.failed
        assert run_tmp.live_paths() == frozenset()

    async def test_shutdown_cancel_removes_tmp(self, tmp_path) -> None:
        w = _Worker(tmp_path, "i-cancel")
        run = await w.make_run()
        started = asyncio.Event()

        async def _block(*_a, tmp_dir=None, **_kw):
            w.seen["tmp_dir"] = tmp_dir
            started.set()
            await asyncio.Event().wait()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            w.install(mock_gtd, mock_dispatch, run)
            mock_dispatch.run_agent = AsyncMock(side_effect=_block)
            task = asyncio.create_task(main._dispatch_worker(run, 50, CLAUDE, 600))
            await asyncio.wait_for(started.wait(), 10)
            expected = config.RUN_TMP_ROOT / f"rt-{run.id}"
            assert expected.is_dir()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert w.seen["tmp_dir"] == expected
        assert not expected.exists()
        assert expected not in run_tmp.live_paths()


# ---------------------------------------------------------------------------
# Rollout reviewer (AC-10, AC-15)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_worker_env")
class TestReviewer:
    def _ws(self, tmp_path: Path) -> SimpleNamespace:
        root = tmp_path / "slot"
        root.mkdir()
        return SimpleNamespace(
            root=root,
            repo_paths={"r": root / "r"},
            default_branches={"r": "main"},
            workspace_mode=True,
        )

    def _launch(self, ws):
        return main._launch_reviewer(
            "rollout-abc",
            "item-a",
            project={"name": "p"},
            workspace=ws,
            reviewer_engine=SimpleNamespace(name="claude-code-sonnet"),
            token=None,
            attribution=None,
            item={"id": "item-a", "title": "t"},
        )

    async def test_tmp_dir_passed_and_removed(self, tmp_path) -> None:
        from tests.test_manage_recovery import _loop_patch_stack

        seen: dict[str, Any] = {}

        async def _agent(*_a, tmp_dir=None, **_kw):
            seen["tmp_dir"] = tmp_dir
            seen["was_dir"] = tmp_dir is not None and tmp_dir.is_dir()
            return _completed(0)

        with _loop_patch_stack() as ctx:
            ctx.dispatch.run_agent = AsyncMock(side_effect=_agent)
            ctx.dispatch.read_review_verdict = MagicMock(return_value=(None, "absent"))
            ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
            ctx.gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            await self._launch(self._ws(tmp_path))
            run_id = ctx.db.insert_run.await_args.args[0].id
        expected = config.RUN_TMP_ROOT / f"rt-{run_id}"
        assert seen["tmp_dir"] == expected
        assert seen["was_dir"] is True
        assert not expected.exists()
        assert run_tmp.live_paths() == frozenset()

    async def test_cancel_removes_tmp(self, tmp_path) -> None:
        from tests.test_manage_recovery import _loop_patch_stack

        started = asyncio.Event()
        seen: dict[str, Any] = {}

        async def _agent(*_a, tmp_dir=None, **_kw):
            seen["tmp_dir"] = tmp_dir
            started.set()
            await asyncio.Event().wait()

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_active_subprocesses", {}),
        ):
            ctx.dispatch.run_agent = AsyncMock(side_effect=_agent)
            ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
            ctx.gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            task = asyncio.create_task(self._launch(self._ws(tmp_path)))
            waiter = asyncio.create_task(started.wait())
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            assert not task.done(), task.exception()
            assert seen["tmp_dir"].is_dir()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not seen["tmp_dir"].exists()
        assert run_tmp.live_paths() == frozenset()


# ---------------------------------------------------------------------------
# Land (AC-11, AC-15)
# ---------------------------------------------------------------------------


@pytest.fixture
def land_env(monkeypatch):
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
def mono(tmp_path: Path):
    from tests.test_land_gate import _clone, _mono_ws, _origin, _pre, _squash

    origin, _seed = _origin(tmp_path, "repo")
    clone = _clone(origin, tmp_path / "repo")
    ws = _mono_ws(clone)
    pre = _pre(ws)
    _squash(clone)
    return SimpleNamespace(ws=ws, pre=pre)


LOOP_RUN_ID = "100000000001"


async def _land(mono) -> Any:
    from agent_gtd_dispatch.engines import get_engine
    from tests.test_land_gate import ITEM, ROLLOUT, _verdict

    return await main._land_reviewed_merge(
        ROLLOUT,
        ITEM,
        _verdict(),
        project={"name": "P", "git_origin": "git@h:o/repo.git", "gate_command": "true"},
        workspace=mono.ws,
        pre_review_origin=mono.pre,
        reviewer_engine=get_engine("claude-code-sonnet"),
        loop_run_id=LOOP_RUN_ID,
        token=None,
        attribution=None,
    )


def _land_tmp() -> Path:
    from tests.test_land_gate import ITEM

    return config.RUN_TMP_ROOT / f"rt-land-{LOOP_RUN_ID}-{ITEM}"


@pytest.mark.usefixtures("land_env")
class TestLand:
    async def test_success_passes_tmp_dir_and_removes_it(self, mono) -> None:
        seen: dict[str, Any] = {}
        real_gate = dispatch.run_gate_command

        def _gate(*args, tmp_dir=None):
            seen["gate"] = tmp_dir
            seen["gate_was_dir"] = tmp_dir is not None and tmp_dir.is_dir()
            return real_gate(*args, tmp_dir=tmp_dir)

        def _steps(*_a, tmp_dir=None, **_kw):
            seen["install"] = tmp_dir
            return None

        with (
            patch.object(gates, "detect_gate_steps", return_value=[MagicMock()]),
            patch.object(gates, "run_gate_steps", side_effect=_steps),
            patch.object(dispatch, "run_gate_command", side_effect=_gate),
        ):
            result = await _land(mono)
        assert isinstance(result, main.ReviewVerdict)
        assert seen["gate"] == _land_tmp()
        assert seen["install"] == _land_tmp()
        assert seen["gate_was_dir"] is True
        assert not _land_tmp().exists()
        assert run_tmp.live_paths() == frozenset()

    async def test_halt_removes_tmp(self, mono) -> None:
        failure = gates.GateFailure("repo", "pre-commit install", "boom", "", 3)
        with (
            patch.object(gates, "detect_gate_steps", return_value=[MagicMock()]),
            patch.object(gates, "run_gate_steps", return_value=failure),
        ):
            result = await _land(mono)
        assert result == main._TICK_HALTED
        assert not _land_tmp().exists()
        assert run_tmp.live_paths() == frozenset()

    async def test_create_failure_halts(self, mono, land_env) -> None:
        with patch.object(run_tmp, "create_run_tmp", side_effect=OSError("full")):
            result = await _land(mono)
        assert result == main._TICK_HALTED
        reason = land_env.halt_rollout.await_args.kwargs["reason"]
        assert "could not create private TMPDIR" in reason

    async def test_cancel_during_gate_removes_tmp(self, mono) -> None:
        entered = threading.Event()
        release = threading.Event()

        def _gate(*_a, tmp_dir=None):
            entered.set()
            release.wait(10)
            return GateResult(
                returncode=0, timed_out=False, output="", duration_seconds=0.0
            )

        try:
            with patch.object(dispatch, "run_gate_command", side_effect=_gate):
                task = asyncio.create_task(_land(mono))
                for _ in range(500):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                assert _land_tmp().is_dir()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        assert not _land_tmp().exists()
        assert run_tmp.live_paths() == frozenset()


# ---------------------------------------------------------------------------
# Startup sweep (AC-12)
# ---------------------------------------------------------------------------


class TestLifespanSweep:
    def test_startup_removes_orphans(self, tmp_path, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        for key, value in _REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.delenv("DISPATCH_AGENT_SUBPROCESS_USER", raising=False)
        orphan = config.RUN_TMP_ROOT / "rt-aaaaaaaaaaab"
        orphan.mkdir()
        keep = config.RUN_TMP_ROOT / "workspace"
        keep.mkdir()
        with (
            patch.object(main, "_readopt_running_rollouts", new=AsyncMock()),
            TestClient(main.app),
        ):
            assert not orphan.exists()
        assert keep.is_dir()
