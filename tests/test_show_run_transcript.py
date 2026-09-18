"""Tests for the show_run_transcript operator helper."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, show_run_transcript


@pytest.fixture
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "DISPATCH_EVIDENCE_ROOT": str(tmp_path / "evidence"),
    }
    (tmp_path / "workspace").mkdir()
    (tmp_path / "evidence").mkdir()
    with patch.dict(os.environ, env):
        config.load()
        yield


def _run(run_id: str) -> None:
    with patch.object(show_run_transcript.sys, "argv", ["prog", run_id]):
        show_run_transcript.main()


class TestShowRunTranscript:
    def test_live_workspace_hit_wins(self, _env, tmp_path, capsys) -> None:
        live = tmp_path / "workspace" / "repos-abc123"
        live.mkdir()
        (live / "transcript.txt").write_text("LIVE")
        evidence = tmp_path / "evidence" / "abc123"
        evidence.mkdir()
        (evidence / "transcript.txt").write_text("RETAINED")

        _run("abc123")
        assert capsys.readouterr().out == "LIVE"

    def test_evidence_fallback_after_teardown(self, _env, tmp_path, capsys) -> None:
        evidence = tmp_path / "evidence" / "def456"
        evidence.mkdir()
        (evidence / "transcript.txt").write_text("RETAINED")

        _run("def456")
        assert capsys.readouterr().out == "RETAINED"

    def test_neither_exits_one(self, _env, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            _run("ghi789")
        assert exc.value.code == 1
        assert "No transcript found" in capsys.readouterr().err
