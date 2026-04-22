"""Tests for core dispatch logic and engine definitions."""

from __future__ import annotations

import subprocess
from typing import ClassVar
from unittest.mock import call, patch

import pytest

from agent_gtd_dispatch import config
from agent_gtd_dispatch.dispatch import (
    branch_name_for_item,
    build_system_prompt,
    cleanup_workspace,
    prepare_workspace,
    repo_name_from_origin,
    run_agent,
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


class TestPrepareWorkspace:
    @pytest.fixture
    def workspace_root(self, tmp_path, monkeypatch) -> object:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        return tmp_path

    def test_workspace_path_uses_run_id(self, workspace_root, tmp_path) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"

        with patch("agent_gtd_dispatch.dispatch.subprocess"):
            result = prepare_workspace(origin, run_id, branch)

        expected = tmp_path / f"repos-myrepo-{run_id}"
        assert result == expected

    def test_calls_git_clone_then_checkout(self, workspace_root, tmp_path) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"
        expected_workspace = tmp_path / f"repos-myrepo-{run_id}"

        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            prepare_workspace(origin, run_id, branch)

        assert mock_sub.run.call_count == 2
        clone_call, checkout_call = mock_sub.run.call_args_list
        assert clone_call == call(
            ["git", "clone", origin, str(expected_workspace)],
            check=True,
            capture_output=True,
        )
        assert checkout_call == call(
            ["git", "checkout", "-b", branch],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )

    def test_creates_workspace_root_if_missing(self, tmp_path, monkeypatch) -> None:
        nested_root = tmp_path / "nonexistent" / "workspace"
        monkeypatch.setattr(config, "WORKSPACE_ROOT", nested_root)

        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"

        with patch("agent_gtd_dispatch.dispatch.subprocess"):
            prepare_workspace(origin, run_id, branch)

        assert nested_root.exists()


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
        # Short prompt referencing system_prompt.md (written by run_agent)
        assert cmd[-1] == (
            "Use the read tool to open system_prompt.md in this directory, "
            "then follow every instruction inside it."
        )

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


class TestBuildSystemPrompt:
    _item: ClassVar[dict] = {
        "id": "abc12345-0000-0000-0000-000000000000",
        "title": "Fix the login bug",
        "description": "Users cannot log in with OAuth.",
    }
    _project: ClassVar[dict] = {"name": "my-cool-project"}
    _branch = "feat/abc12345-fix-the-login-bug"
    _max_turns = 42

    def _prompt(self, item=None, project=None, branch=None, max_turns=None) -> str:
        return build_system_prompt(
            item or self._item,
            project or self._project,
            branch or self._branch,
            max_turns if max_turns is not None else self._max_turns,
        )

    def test_includes_project_and_item_fields(self) -> None:
        prompt = self._prompt()
        assert "my-cool-project" in prompt
        assert "Fix the login bug" in prompt
        assert "abc12345-0000-0000-0000-000000000000" in prompt
        assert self._branch in prompt
        assert "42" in prompt

    def test_includes_description_when_present(self) -> None:
        prompt = self._prompt()
        assert "Users cannot log in with OAuth." in prompt

    def test_fallback_when_no_description(self) -> None:
        item_no_desc = {
            "id": "abc12345-0000-0000-0000-000000000000",
            "title": "Fix the login bug",
        }
        prompt = self._prompt(item=item_no_desc)
        assert "No description provided" in prompt

    def test_says_already_on_branch(self) -> None:
        prompt = self._prompt()
        assert "already on branch" in prompt

    def test_is_engine_agnostic(self) -> None:
        prompt = self._prompt()
        assert "headless coding agent" in prompt
        assert "Claude Code" not in prompt


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


class TestCleanupWorkspace:
    def test_removes_existing_workspace(self, workspace_root) -> None:
        ws = workspace_root / "my-run-abc123"
        ws.mkdir()
        assert ws.exists()
        cleanup_workspace(ws)
        assert not ws.exists()

    def test_noop_when_missing(self, workspace_root) -> None:
        ws = workspace_root / "nonexistent-run"
        assert not ws.exists()
        # Should not raise
        cleanup_workspace(ws)

    def test_safety_check_refuses_outside_workspace_root(self, workspace_root) -> None:
        outside = workspace_root.parent / "outside-dir"
        outside.mkdir()
        try:
            cleanup_workspace(outside)
            assert outside.exists()
        finally:
            outside.rmdir()


class TestRunAgent:
    async def test_calls_subprocess_with_engine_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            args, _kwargs = mock_run.call_args
            assert args[0][0] == "claude"
            assert "--dangerously-skip-permissions" in args[0]

    async def test_passes_workspace_as_cwd(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_run.call_args
            assert kwargs["cwd"] == tmp_path

    async def test_passes_timeout_from_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 42)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 42

    async def test_env_filtered_by_engine(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("KIRO_API_KEY", "kiro-secret")
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_run.call_args
            assert kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-test"
            assert "KIRO_API_KEY" not in kwargs["env"]

    async def test_agent_name_passes_through_to_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, agent_name="my-agent")
            args, _kwargs = mock_run.call_args
            cmd = args[0]
            idx = cmd.index("--agent")
            assert cmd[idx + 1] == "my-agent"

    async def test_returns_completed_process(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="done", stderr=""
            )
            result = await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            assert result.returncode == 0
            assert result.stdout == "done"

    async def test_kiro_writes_system_prompt_md(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(KIRO, tmp_path, "sys prompt", "Fix bug", 20)
            md = (tmp_path / "system_prompt.md").read_text()
            assert "sys prompt" in md
            assert "Fix bug" in md

    async def test_claude_does_not_write_system_prompt_md(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            assert not (tmp_path / "system_prompt.md").exists()
