from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_gtd_dispatch import config, engines, main


def _proc(stdout: str) -> MagicMock:
    return MagicMock(stdout=stdout.encode())


class TestBashLimitSelfProbe:
    def test_ok(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        out = "\n".join(f"{k}=600001" for k in engines.CLAUDE_BASH_LIMIT_ENV_KEYS)
        caplog.set_level(logging.INFO)
        with patch.object(main.subprocess, "run", return_value=_proc(out)) as run:
            main._bash_limit_self_probe()
        run.assert_called_once()
        assert (
            "bash-limit-selfprobe: env_keep=ok keys=BASH_MAX_TIMEOUT_MS,"
            "CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS"
        ) in caplog.text

    def test_missing_key(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        caplog.set_level(logging.INFO)
        with patch.object(
            main.subprocess, "run", return_value=_proc("BASH_MAX_TIMEOUT_MS=600001\n")
        ):
            main._bash_limit_self_probe()
        (rec,) = [r for r in caplog.records if "bash-limit-selfprobe" in r.message]
        assert rec.levelno == logging.ERROR
        assert "env_keep=stripped missing=CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS" in (
            rec.message
        )
        assert "run ./deploy.sh to refresh sudoers" in rec.message

    def test_user_unset(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        caplog.set_level(logging.INFO)
        with patch.object(main.subprocess, "run") as run:
            main._bash_limit_self_probe()
        run.assert_not_called()
        assert "bash-limit-selfprobe: env_keep=n/a single_user=1" in caplog.text

    @pytest.mark.parametrize(
        "exc", [RuntimeError("boom"), subprocess.TimeoutExpired("sudo", 10)]
    )
    def test_probe_failure_never_raises(self, monkeypatch, caplog, exc) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        with patch.object(main.subprocess, "run", side_effect=exc):
            main._bash_limit_self_probe()
        assert any(
            r.levelno == logging.ERROR and "env_keep=stripped" in r.message
            for r in caplog.records
        )
