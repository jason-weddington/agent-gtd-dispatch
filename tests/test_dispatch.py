"""Tests for core dispatch logic and engine definitions."""

from __future__ import annotations

import pytest

from agent_gtd_dispatch.dispatch import (
    branch_name_for_item,
    repo_name_from_origin,
)
from agent_gtd_dispatch.engines import CLAUDE, KIRO, build_env, get_engine


class TestRepoNameFromOrigin:
    def test_scp_style(self) -> None:
        origin = "git@ubuntu-vm01:repos/agent_gtd"
        assert repo_name_from_origin(origin) == "repos-agent_gtd"

    def test_ssh_url(self) -> None:
        assert (
            repo_name_from_origin("ssh://git@ubuntu-vm01/home/git/repos/agent_gtd")
            == "repos-agent_gtd"
        )

    def test_github_ssh(self) -> None:
        assert (
            repo_name_from_origin("git@github.com:jason-weddington/agent-gtd.git")
            == "jason-weddington-agent-gtd"
        )

    def test_https(self) -> None:
        assert (
            repo_name_from_origin("https://github.com/jason-weddington/agent-gtd.git")
            == "jason-weddington-agent-gtd"
        )

    def test_simple_name_fallback(self) -> None:
        # file:// URLs have a path; the regex picks up tmp/myrepo
        assert repo_name_from_origin("file:///tmp/myrepo") == "tmp-myrepo"


class TestBranchName:
    def test_basic(self) -> None:
        result = branch_name_for_item("abcd1234-5678", "Fix login bug")
        assert result == "feat/abcd1234-fix-login-bug"

    def test_truncates_long_title(self) -> None:
        long_title = "A" * 100
        result = branch_name_for_item("abcd1234", long_title)
        # slug portion should be at most 40 chars
        slug = result.split("/", 1)[1].split("-", 1)[1]
        assert len(slug) <= 40

    def test_special_characters(self) -> None:
        result = branch_name_for_item("abcd1234", "Fix: the @#$ API (broken)")
        assert result == "feat/abcd1234-fix-the-api-broken"

    def test_trailing_hyphens_stripped(self) -> None:
        result = branch_name_for_item("abcd1234", "Fix ---")
        assert not result.endswith("-")


class TestGetEngine:
    def test_claude(self) -> None:
        assert get_engine("claude") is CLAUDE

    def test_kiro(self) -> None:
        assert get_engine("kiro") is KIRO

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown engine"):
            get_engine("gpt-5")


class TestClaudeCommand:
    def test_basic(self) -> None:
        cmd = CLAUDE.build_command("sys prompt", "Fix bug", 20, None)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--system-prompt" in cmd
        assert "--max-turns" in cmd
        assert "20" in cmd
        assert "--print" in cmd
        assert cmd[-1] == "Fix bug"
        assert "--agent" not in cmd

    def test_with_agent(self) -> None:
        cmd = CLAUDE.build_command("sys prompt", "Fix bug", 20, "my-agent")
        idx = cmd.index("--agent")
        assert cmd[idx + 1] == "my-agent"


class TestKiroCommand:
    def test_basic(self) -> None:
        cmd = KIRO.build_command("sys prompt", "Fix bug", 20, None)
        assert cmd[0] == "kiro-cli"
        assert "chat" in cmd
        assert "--no-interactive" in cmd
        assert "--trust-all-tools" in cmd
        assert "--agent" not in cmd
        # System prompt baked into user prompt (last arg)
        assert "sys prompt" in cmd[-1]
        assert "Fix bug" in cmd[-1]

    def test_with_agent(self) -> None:
        cmd = KIRO.build_command("sys prompt", "Fix bug", 20, "my-agent")
        idx = cmd.index("--agent")
        assert cmd[idx + 1] == "my-agent"


class TestBuildEnv:
    def test_includes_common_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("AGENT_GTD_URL", "http://localhost")
        env = build_env(CLAUDE)
        assert "PATH" in env
        assert "AGENT_GTD_URL" in env

    def test_includes_engine_specific(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        env = build_env(CLAUDE)
        assert env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_excludes_other_engine_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "kiro-test")
        env = build_env(CLAUDE)
        assert "KIRO_API_KEY" not in env

    def test_kiro_includes_own_key(self, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "kiro-test")
        env = build_env(KIRO)
        assert env["KIRO_API_KEY"] == "kiro-test"
