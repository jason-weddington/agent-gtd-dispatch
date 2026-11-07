"""The run-lifecycle platform seam: procfs vs portable selection and startup checks."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import psutil
import pytest
from fastapi.testclient import TestClient

from agent_gtd_dispatch import config, dispatch, main, platform_support, portable_procs

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _clear_gate_tool_cache() -> Iterator[None]:
    platform_support.gate_timeout_tool.cache_clear()
    yield
    platform_support.gate_timeout_tool.cache_clear()


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "DISPATCH_AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


def _msgs(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


class TestProcfsUsable:
    def test_linux_with_proc(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        assert platform_support.procfs_usable() is True

    def test_linux_without_proc(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "exists", lambda self: False)
        assert platform_support.procfs_usable() is False

    def test_darwin_never(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        assert platform_support.procfs_usable() is False

    @pytest.mark.parametrize(
        ("flag", "backend"), [(True, "pkill"), (False, "portable")]
    )
    def test_session_backend(self, monkeypatch, flag: bool, backend: str) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", flag)
        assert platform_support.session_backend() == backend


class TestGateTimeoutTool:
    @pytest.mark.parametrize(
        ("found", "tool"),
        [
            ({"timeout", "gtimeout"}, "timeout"),
            ({"gtimeout"}, "gtimeout"),
            (set(), None),
        ],
    )
    def test_choice(self, monkeypatch, found: set[str], tool: str | None) -> None:
        monkeypatch.setattr(
            platform_support.shutil,
            "which",
            lambda name, **_k: f"/x/{name}" if name in found else None,
        )
        assert platform_support.gate_timeout_tool() == tool

    def test_cached_until_cleared(self, monkeypatch) -> None:
        calls: list[str] = []

        def _which(name: str, **_k: object) -> str | None:
            calls.append(name)
            return "/x/timeout"

        monkeypatch.setattr(platform_support.shutil, "which", _which)
        assert platform_support.gate_timeout_tool() == "timeout"
        assert platform_support.gate_timeout_tool() == "timeout"
        assert calls == ["timeout"]
        platform_support.gate_timeout_tool.cache_clear()
        platform_support.gate_timeout_tool()
        assert calls == ["timeout", "timeout"]

    @pytest.mark.parametrize(
        ("tool", "level"),
        [
            ("timeout", logging.INFO),
            ("gtimeout", logging.INFO),
            (None, logging.WARNING),
        ],
    )
    def test_startup_line(self, monkeypatch, caplog, tool, level) -> None:
        monkeypatch.setattr(platform_support, "gate_timeout_tool", lambda: tool)
        caplog.set_level(logging.INFO)
        platform_support.log_gate_timeout_line()
        (rec,) = _msgs(caplog, "gate-timeout:")
        assert rec.getMessage() == f"gate-timeout: tool={tool or 'python'}"
        assert rec.levelno == level


class TestTwoUserRefusal:
    @pytest.mark.usefixtures("_env")
    def test_refuses_off_procfs(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("DISPATCH_AGENT_SUBPROCESS_USER", "dispatch")
        config.load()
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        with pytest.raises(RuntimeError) as exc:
            platform_support.check_two_user_supported()
        for needle in (
            "two-user mode (DISPATCH_AGENT_SUBPROCESS_USER) requires Linux /proc",
            "single-user mode",
            "docs/install.md",
        ):
            assert needle in str(exc.value)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.usefixtures("_env")
    def test_single_user_off_procfs_ok(self, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_AGENT_SUBPROCESS_USER", "")
        config.load()
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        platform_support.check_two_user_supported()

    @pytest.mark.usefixtures("_env")
    def test_two_user_on_procfs_ok(self, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_AGENT_SUBPROCESS_USER", "dispatch")
        config.load()
        monkeypatch.setattr(platform_support, "USE_PROCFS", True)
        platform_support.check_two_user_supported()

    @pytest.mark.usefixtures("_env")
    def test_lifespan_refuses(self, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        with (
            pytest.raises(RuntimeError, match="requires Linux /proc"),
            TestClient(main.app),
        ):
            pass


class TestPlatformLine:
    @pytest.mark.usefixtures("_env")
    @pytest.mark.parametrize(
        ("plat", "procfs", "level"),
        [
            ("linux", True, logging.INFO),
            ("linux", False, logging.WARNING),
            ("darwin", False, logging.INFO),
        ],
    )
    def test_line(self, monkeypatch, caplog, plat, procfs, level) -> None:
        monkeypatch.setattr(sys, "platform", plat)
        monkeypatch.setattr(platform_support, "USE_PROCFS", procfs)
        monkeypatch.setattr(platform_support, "gate_timeout_tool", lambda: "gtimeout")
        caplog.set_level(logging.INFO)
        platform_support.log_platform_line()
        (rec,) = _msgs(caplog, "platform:")
        backend = "pkill" if procfs else "portable"
        assert rec.getMessage() == (
            f"platform: sys_platform={plat} procfs={str(procfs).lower()} "
            f"session_backend={backend} gate_timeout=gtimeout "
            f"psutil={psutil.__version__} two_user=false"
        )
        assert rec.levelno == level

    def test_psutil_missing(self, monkeypatch) -> None:
        def _no() -> None:
            raise ImportError("no psutil")

        monkeypatch.setattr(portable_procs, "_psutil", _no)
        assert platform_support.platform_info()["psutil"] == "missing"

    @pytest.mark.usefixtures("_env")
    def test_info_exposes_platform(self) -> None:
        with TestClient(main.app) as client:
            info = client.get("/info").json()
        assert set(info["platform"]) == {
            "sys_platform",
            "procfs",
            "session_backend",
            "gate_timeout",
            "psutil",
            "two_user",
        }
        assert info["platform"]["sys_platform"] == sys.platform

    @pytest.mark.usefixtures("_env")
    def test_lifespan_logs_platform_and_gate_lines(self, caplog) -> None:
        caplog.set_level(logging.INFO)
        with TestClient(main.app):
            pass
        assert len(_msgs(caplog, "platform:")) == 1
        assert len(_msgs(caplog, "gate-timeout: tool=")) == 1

    @pytest.mark.usefixtures("_env")
    def test_lifespan_logs_gate_budget_line(self, caplog) -> None:
        caplog.set_level(logging.INFO)
        with TestClient(main.app):
            pass
        lines = _msgs(caplog, "post-run gate budget:")
        assert len(lines) == 1
        assert "effective_s=3600 source=post_run" in lines[0].getMessage()


_HELPERS = [
    ("_proc_cmdline", "cmdline", (1,), "c"),
    ("_proc_is_live", "is_live", (1,), True),
    ("_ancestor_pids", "ancestor_pids", (), {7}),
    ("_proc_run_marker", "run_marker", (1,), "rt-x"),
    ("_proc_ppid", "ppid", (1,), 42),
    ("_proc_real_uid", "real_uid", (1,), 4242),
]


class TestSeamRouting:
    @pytest.mark.parametrize(("name", "fn", "args", "value"), _HELPERS)
    def test_portable_branch(self, monkeypatch, name, fn, args, value) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        monkeypatch.setattr(portable_procs, fn, lambda *_a: value)
        assert getattr(dispatch, name)(*args) == value

    @pytest.mark.parametrize(("name", "fn", "args", "value"), _HELPERS)
    def test_procfs_branch(self, monkeypatch, name, fn, args, value) -> None:
        if not platform_support.procfs_usable():
            pytest.skip("needs /proc")

        def _never(*_a: object) -> None:
            raise AssertionError("portable branch used on procfs")

        monkeypatch.setattr(platform_support, "USE_PROCFS", True)
        monkeypatch.setattr(portable_procs, fn, _never)
        getattr(dispatch, name)(*args)

    def test_real_uid_through_seam(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        monkeypatch.setattr(portable_procs, "real_uid", lambda _p: os.getuid() + 1)
        assert dispatch._proc_real_uid(os.getpid()) == os.getuid() + 1

    def test_scans_route(self, monkeypatch, tmp_path: Path) -> None:
        res = portable_procs.ScanResult({5: "x"}, 3, 1)
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        monkeypatch.setattr(portable_procs, "scan_workspace_pids", lambda _w: res)
        monkeypatch.setattr(portable_procs, "scan_marker", lambda _m, _u: res)
        assert dispatch._scan_workspace_pids(tmp_path) == {5: "x"}
        assert dispatch._scan_marker_in_process("rt-x") == {5: "x"}

    def test_scans_procfs_branch(self, monkeypatch, tmp_path: Path) -> None:
        if not platform_support.procfs_usable():
            pytest.skip("needs /proc")

        def _never(*_a: object) -> None:
            raise AssertionError("portable branch used on procfs")

        monkeypatch.setattr(platform_support, "USE_PROCFS", True)
        monkeypatch.setattr(portable_procs, "scan_workspace_pids", _never)
        monkeypatch.setattr(portable_procs, "scan_marker", _never)
        assert dispatch._scan_workspace_pids(tmp_path) == {}
        assert dispatch._scan_marker_in_process("rt-none-such") == {}


@pytest.mark.real_reap
class TestReaperSelfProbe:
    @pytest.mark.usefixtures("_env")
    def test_probe_passes_and_startup_proceeds(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        caplog.set_level(logging.INFO)
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
        (rec,) = _msgs(caplog, "reap-selfprobe:")
        assert rec.getMessage() == (
            "reap-selfprobe: backend=psutil marker_scan=ok found=1"
        )
        assert rec.levelno == logging.INFO

    @pytest.mark.usefixtures("_env")
    def test_probe_fails_closed(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)

        def _deny(self):
            raise psutil.AccessDenied(self.pid)

        monkeypatch.setattr(psutil.Process, "environ", _deny)
        with (
            pytest.raises(RuntimeError) as exc,
            TestClient(main.app),
        ):
            pass
        assert "marker reaping is blind" in str(exc.value)
        assert "docs/install.md" in str(exc.value)
        (rec,) = _msgs(caplog, "reap-selfprobe:")
        assert rec.levelno == logging.ERROR
        assert rec.getMessage().startswith(
            "reap-selfprobe: backend=psutil marker_scan=fail found=0 error="
        )
        assert rec.getMessage().endswith(" - marker reaping is blind on this host")

    def test_probe_not_found(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_support, "USE_PROCFS", False)
        monkeypatch.setattr(main, "_SELF_PROBE_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(
            dispatch, "scan_marker_pids", lambda _m: dispatch.MarkerScan(True, {}, "")
        )
        with pytest.raises(RuntimeError, match="marked child not found"):
            main._reaper_self_probe()

    def test_probe_spawn_failure(self, monkeypatch) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("no sleep")

        monkeypatch.setattr(main.subprocess, "Popen", _boom)
        with pytest.raises(RuntimeError, match="OSError: no sleep"):
            main._reaper_self_probe()
