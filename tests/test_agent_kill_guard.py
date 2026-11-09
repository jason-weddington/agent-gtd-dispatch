"""The kill-guard shims: pattern kills refused, PIDs untouched, nothing shadowed.

Every build, gate and review run on a dispatch host runs as ONE shared account,
so a `pkill -f <pattern>` from any run matches another run's processes (their
argv carries prompts, branch names and script names) — which is exactly how two
rollout reviewers were killed mid-run (exit 143 and -9) and five concurrent
build gates died on 2026-11-09.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_gtd_dispatch import config, dispatch, engines, run_tmp

REPO = Path(__file__).parent.parent
TMPL_DIR = REPO / "templates" / "agent-gtd-kill-guard"
SETUP = REPO / "setup-dispatch-host.sh"
DEPLOY = REPO / "deploy.sh"
SUDOERS = REPO / "templates" / "sudoers-dispatch-svc.tmpl"

PKILL_LINE = (
    "pkill: refused - a pattern kill can hit another run on this shared account; "
    "kill only a PID you started, e.g. kill $! or the number in your pidfile"
)
KILLALL_LINE = (
    "killall: refused - a pattern kill can hit another run on this shared account; "
    "kill only a PID you started, e.g. kill $! or the number in your pidfile"
)


def _guard_copy(tmp_path: Path) -> Path:
    guard = tmp_path / "guard"
    shutil.copytree(TMPL_DIR, guard)
    for name in ("pkill", "killall"):
        (guard / name).chmod(0o755)
    return guard


def _env(guard: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": f"{guard}:" + os.environ["PATH"]}
    if extra:
        env.update(extra)
    return env


@pytest.fixture
def dummy() -> object:
    """A process the shims must never touch."""
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        time.sleep(0.01)
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _parse_guard_line(line: str) -> dict[str, str]:
    head, _, args = line.partition(" args=")
    fields = dict(field.split("=", 1) for field in head.split(" "))
    fields["args"] = args
    return fields


class TestShimsRefuse:
    @pytest.mark.parametrize(
        "args", [["-f", "gate.sh"], ["gate.sh"], ["-9", "-f", "selftest"]]
    )
    def test_pkill_refuses_every_pattern(self, tmp_path, dummy, args) -> None:
        guard = _guard_copy(tmp_path)
        result = subprocess.run(
            [str(guard / "pkill"), *args],
            env=_env(guard),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == PKILL_LINE + "\n"
        assert dummy.poll() is None

    def test_killall_refuses(self, tmp_path, dummy) -> None:
        guard = _guard_copy(tmp_path)
        result = subprocess.run(
            [str(guard / "killall"), "selftest"],
            env=_env(guard),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == KILLALL_LINE + "\n"
        assert dummy.poll() is None


class TestRepoFiles:
    @pytest.mark.parametrize("name", ["pkill", "killall"])
    def test_mode_is_0755_and_shell_syntax_is_valid(self, name: str) -> None:
        path = TMPL_DIR / name
        # The on-disk mode depends on the checkout's umask (0775 under 002),
        # so assert the property that matters: executable by everyone, never
        # world-writable. The 0755 the repo records is what install(1) sets.
        mode = path.stat().st_mode & 0o777
        assert mode & 0o555 == 0o555
        assert not mode & 0o002
        assert subprocess.run(["sh", "-n", str(path)], check=False).returncode == 0

    def test_first_line_is_the_sh_bang(self) -> None:
        for name in ("pkill", "killall"):
            assert (TMPL_DIR / name).read_text().splitlines()[0] == "#!/bin/sh"

    def test_copy_keeps_dollar_bang_literal(self) -> None:
        """`$!` must survive as two characters, never expand to the shell's pid."""
        for name in ("pkill", "killall"):
            assert "kill $!" in (TMPL_DIR / name).read_text()


class TestNothingElseIsShadowed:
    def test_guard_dir_holds_exactly_the_two_shims(self) -> None:
        assert sorted(p.name for p in TMPL_DIR.iterdir()) == ["killall", "pkill"]

    def test_only_pkill_resolution_changes(self, tmp_path) -> None:
        guard = _guard_copy(tmp_path)
        base = os.environ["PATH"]
        guarded = f"{guard}:" + base
        for name in ("kill", "sleep", "sh", "bash", "git", "pgrep"):
            if shutil.which(name, path=base) is None:
                continue
            assert shutil.which(name, path=guarded) == shutil.which(name, path=base)
        assert shutil.which("pkill", path=guarded) == f"{guard}/pkill"

    def test_numeric_kill_still_works(self, dummy) -> None:
        os.kill(dummy.pid, signal.SIGTERM)
        assert dummy.wait(timeout=5) == -signal.SIGTERM


class TestGuardLog:
    def test_every_refusal_appends_one_structured_line(self, tmp_path, dummy) -> None:
        guard = _guard_copy(tmp_path)
        log = tmp_path / "kg.log"
        env = _env(guard, {run_tmp.GUARD_LOG_ENV: str(log)})
        for args in (["-f", "gate.sh"], ["gate.sh"], ["-9", "-f", "selftest"]):
            subprocess.run(
                [str(guard / "pkill"), *args], env=env, capture_output=True, check=False
            )
        subprocess.run(
            [str(guard / "killall"), "selftest"],
            env=env,
            capture_output=True,
            check=False,
        )
        lines = log.read_text().splitlines()
        assert len(lines) == 4
        parsed = [_parse_guard_line(line) for line in lines]
        assert [p["shim"] for p in parsed] == ["pkill", "pkill", "pkill", "killall"]
        assert parsed[0]["args"] == "-f gate.sh"
        assert parsed[3]["args"] == "selftest"
        for fields in parsed:
            assert int(fields["ts"]) > 0
            assert fields["pid"].isdigit()
            assert fields["ppid"].isdigit()
            assert fields["cwd"]

    def test_unwritable_log_path_never_adds_stderr_or_changes_the_exit(
        self, tmp_path
    ) -> None:
        guard = _guard_copy(tmp_path)
        env = _env(guard, {run_tmp.GUARD_LOG_ENV: str(tmp_path / "no" / "such.log")})
        result = subprocess.run(
            [str(guard / "pkill"), "-f", "x"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert result.stderr == PKILL_LINE + "\n"

    def test_count_guard_refusals_reads_the_log(self, tmp_path) -> None:
        tmp_dir = tmp_path / "rt-aaaaaaaaaaab"
        tmp_dir.mkdir()
        assert run_tmp.count_guard_refusals(tmp_dir) == 0
        run_tmp.guard_log_path(tmp_dir).write_text("a\nb\n")
        assert run_tmp.count_guard_refusals(tmp_dir) == 2
        assert run_tmp.count_guard_refusals(tmp_path / "missing") == 0
        assert run_tmp.count_guard_refusals(None) == 0

    async def test_run_agent_warns_when_the_shims_refused(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        run_tmp_dir = tmp_path / "rt-aaaaaaaaaaab"
        run_tmp_dir.mkdir()
        run_tmp.guard_log_path(run_tmp_dir).write_text("one\ntwo\n")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        with (
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.dispatch"),
            patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value = mock_proc
            await dispatch.run_agent(
                engines.CLAUDE, tmp_path, "sys", "T", 5, tmp_dir=run_tmp_dir
            )
        assert any(
            "kill-guard:" in r.getMessage()
            and "refused_count=2" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_run_agent_debugs_when_nothing_was_refused(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        run_tmp_dir = tmp_path / "rt-aaaaaaaaaaab"
        run_tmp_dir.mkdir()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        with (
            caplog.at_level(logging.DEBUG, logger="agent_gtd_dispatch.dispatch"),
            patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value = mock_proc
            await dispatch.run_agent(
                engines.CLAUDE, tmp_path, "sys", "T", 5, tmp_dir=run_tmp_dir
            )
        assert any(
            "refused_count=0" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )


class TestSudoersIsNotShadowed:
    """AC-12: the WORKER's own session kill must never hit a shim."""

    def test_secure_path_does_not_contain_the_guard_dir(self) -> None:
        line = next(
            ln
            for ln in SUDOERS.read_text().splitlines()
            if ln.lstrip().startswith("Defaults:{{SERVICE_USER}} secure_path=")
        )
        assert "kill-guard" not in line

    def test_nopasswd_still_lists_the_real_pkill(self) -> None:
        text = SUDOERS.read_text()
        nopasswd = next(ln for ln in text.splitlines() if "NOPASSWD:" in ln)
        assert "/usr/bin/pkill" in nopasswd

    def test_pkill_session_argv_is_unshadowed_single_user(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        proc = MagicMock()
        proc.pid = 4242
        assert dispatch._pkill_session_argv(proc, ["-KILL"]) == [
            "pkill",
            "-KILL",
            "-s",
            "4242",
        ]

    def test_pkill_session_argv_is_unshadowed_two_user(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        proc = MagicMock()
        proc.pid = 4242
        assert dispatch._pkill_session_argv(proc, ["-KILL"]) == [
            "sudo",
            "-n",
            "-u",
            "dispatch",
            "pkill",
            "-KILL",
            "-s",
            "4242",
        ]


class TestSetupHostProvisions:
    """AC-18: Step 4.12 installs the shims; no install happens at run time."""

    def test_script_parses(self) -> None:
        result = subprocess.run(["bash", "-n", str(SETUP)], check=False)
        assert result.returncode == 0

    def test_step_4_12_block_exists(self) -> None:
        text = SETUP.read_text()
        assert "agent-gtd-kill-guard/pkill" in text
        assert "agent-gtd-kill-guard/killall" in text
        assert 'install -d -m 0755 -o "${AGENT_USER}" -g "${AGENT_GROUP}"' in text
        assert "cmp -s" in text
        assert "die" in text
        assert "Step 4.12" in text

    def test_relpath_matches_the_python_constant(self) -> None:
        assert engines.AGENT_GUARD_BIN_RELPATH in SETUP.read_text()

    def test_guard_relpath_constant_value(self) -> None:
        assert engines.AGENT_GUARD_BIN_RELPATH == (
            ".local/share/agent-gtd-dispatch/kill-guard"
        )


class TestDeployRefreshes:
    """AC-19: the routine channel refreshes the shims too, non-fatally."""

    def test_script_parses(self) -> None:
        result = subprocess.run(["bash", "-n", str(DEPLOY)], check=False)
        assert result.returncode == 0

    def test_shims_are_staged_and_refreshed(self) -> None:
        text = DEPLOY.read_text()
        assert text.count("templates/agent-gtd-kill-guard/") >= 2
        assert "kill-guard" in text
        assert "[SKIP]" in text
        assert "setup-dispatch-host.sh' (Step 4.12)" in text

    def test_guard_refresh_sits_before_the_stage_cleanup(self) -> None:
        text = DEPLOY.read_text()
        assert text.index("kill-guard") < text.index('rm -rf "${_stage}"')


class TestDocsNameTheRealMechanism:
    """AC-20: docs describe the shipped mechanism, engine-agnostically."""

    ARCH = REPO / "docs" / "architecture.md"
    INSTALL = REPO / "docs" / "install.md"

    def test_architecture_section(self) -> None:
        arch = self.ARCH.read_text()
        assert "kill-guard/pkill" in arch
        assert "kill-guard/killall" in arch
        assert "prepend_agent_bin_dirs" in arch
        assert "--system-prompt-file" in arch
        assert "secure_path" in arch
        assert "with_tmp_env" in arch

    def test_install_section(self) -> None:
        install = self.INSTALL.read_text()
        assert "Step 4.12" in install
        assert "Agent kill guard (Step 4.12)" in install
        assert "sudo ./setup-dispatch-host.sh" in install
        assert "deploy.sh" in install
        assert "unknown option" in install
        assert "[SKIP]" in install
        assert "[DRY]" in install
