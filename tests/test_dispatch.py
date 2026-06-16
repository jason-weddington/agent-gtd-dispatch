"""Tests for core dispatch logic and engine definitions."""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, call, patch

import aiosqlite
import pytest

from agent_gtd_dispatch import config, db, dispatch
from agent_gtd_dispatch.dispatch import (
    branch_name_for_item,
    build_system_prompt,
    cleanup_workspace,
    init_executor,
    prepare_manage_workspace,
    prepare_manage_workspace_multi,
    prepare_workspace,
    prepare_workspace_multi,
    repo_dir_from_url,
    repo_name_from_origin,
    run_agent,
)
from agent_gtd_dispatch.engines import (
    CLAUDE,
    CLAUDE_HAIKU,
    CLAUDE_OLLAMA,
    CLAUDE_SONNET,
    KIRO,
    build_env,
    get_engine,
)
from agent_gtd_dispatch.models import DispatchRequest, Run


def _dispatch_sudo_available() -> bool:
    """Return True if 'dispatch' system user exists and we can sudo to them."""
    try:
        pwd.getpwnam("dispatch")
    except KeyError:
        return False
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "-u", "dispatch", "true"], capture_output=True
    )
    return result.returncode == 0


_DISPATCH_SUDO_AVAILABLE = _dispatch_sudo_available()


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
        assert get_engine("claude-code") is CLAUDE

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

    def test_model_is_opus(self) -> None:
        cmd = CLAUDE.build_command("sys prompt", "Fix bug", 20, None)
        assert cmd[1] == "--model"
        assert cmd[2] == "opus"


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

    def test_claude_includes_oauth_token(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
        env = build_env(CLAUDE)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-test"  # noqa: S105

    def test_claude_excludes_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard for kb-01512: ANTHROPIC_API_KEY in the subprocess
        # env makes Claude Code prefer API billing over the user's Max
        # subscription, surprising-to-the-tune-of-$300/month.  The planner
        # subroutine must read the key via config.ANTHROPIC_API_KEY in-process
        # and never via the subprocess env.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        env = build_env(CLAUDE)
        assert "ANTHROPIC_API_KEY" not in env

    def test_excludes_other_engine_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "kiro-test")
        env = build_env(CLAUDE)
        assert "KIRO_API_KEY" not in env

    def test_kiro_includes_own_key(self, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "kiro-test")
        env = build_env(KIRO)
        assert env["KIRO_API_KEY"] == "kiro-test"

    def test_kiro_excludes_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard for kb-01512 (Kiro should never see it either)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        env = build_env(KIRO)
        assert "ANTHROPIC_API_KEY" not in env

    def test_claude_manage_mode_excludes_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard for kb-01512: even when manage-mode adds dispatch
        # URL/key to the executor's env, ANTHROPIC_API_KEY must still be
        # filtered out — the executor itself spawns Claude Code subprocesses.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8100")
        monkeypatch.setenv("DISPATCH_API_KEY", "dispatch-test")
        env = build_env(CLAUDE, mode="manage")
        assert "ANTHROPIC_API_KEY" not in env
        assert env["DISPATCH_LOCAL_URL"] == "http://localhost:8100"

    def test_path_starts_with_local_bin_for_agent_user(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        mock_pw = MagicMock()
        mock_pw.pw_dir = "/home/dispatch"
        with patch("pwd.getpwnam", return_value=mock_pw):
            env = build_env(CLAUDE)
        assert env["PATH"].startswith("/home/dispatch/.local/bin:")

    def test_path_fallback_when_user_unset(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = build_env(CLAUDE)
        expected_prefix = str(Path.home() / ".local" / "bin") + ":"
        assert env["PATH"].startswith(expected_prefix)

    def test_path_fallback_when_user_not_found_keyerror(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "nonexistent-user-xyz")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        with patch("pwd.getpwnam", side_effect=KeyError):
            env = build_env(CLAUDE)
        expected_prefix = str(Path.home() / ".local" / "bin") + ":"
        assert env["PATH"].startswith(expected_prefix)

    def test_path_no_duplication_when_already_present(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        local_bin = str(Path.home() / ".local" / "bin")
        monkeypatch.setenv("PATH", f"{local_bin}:/usr/bin:/bin")
        env = build_env(CLAUDE)
        parts = env["PATH"].split(":")
        assert parts.count(local_bin) == 1


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

    def test_includes_item_id_branch_and_max_turns(self) -> None:
        prompt = self._prompt()
        assert "abc12345-0000-0000-0000-000000000000" in prompt
        assert self._branch in prompt
        assert "42" in prompt

    def test_get_item_instruction_present(self) -> None:
        prompt = self._prompt()
        assert "get_item" in prompt
        assert "abc12345-0000-0000-0000-000000000000" in prompt

    def test_description_not_embedded(self) -> None:
        prompt = self._prompt()
        assert "Users cannot log in with OAuth." not in prompt

    def test_no_description_fallback_text(self) -> None:
        item_no_desc = {
            "id": "abc12345-0000-0000-0000-000000000000",
            "title": "Fix the login bug",
        }
        prompt = self._prompt(item=item_no_desc)
        assert "No description provided" not in prompt

    def test_no_researching_codebase_milestone(self) -> None:
        prompt = self._prompt()
        assert "Researching codebase" not in prompt

    def test_says_already_on_branch(self) -> None:
        prompt = self._prompt()
        assert "already on branch" in prompt

    def test_is_engine_agnostic(self) -> None:
        prompt = self._prompt()
        assert "headless coding agent" in prompt
        assert "Claude Code" not in prompt

    def test_verify_remote_ref_guidance_present(self) -> None:
        prompt = self._prompt()
        assert "verify the remote ref advanced" in prompt or "git ls-remote" in prompt

    def test_no_op_guidance_present(self) -> None:
        prompt = self._prompt()
        assert any(
            phrase in prompt
            for phrase in [
                "no changes needed",
                "already satisfied",
                "do not set status to review",
                "Do NOT set item status to `review`",
                "No-Op Case",
            ]
        )


class TestBuildPlanPrompt:
    _item: ClassVar[dict] = {
        "id": "abc12345-0000-0000-0000-000000000000",
        "title": "Fix the login bug",
        "description": "Users cannot log in with OAuth.",
    }
    _project: ClassVar[dict] = {"name": "my-cool-project"}

    def _prompt(self) -> str:
        return build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=30,
            mode="plan",
        )

    def test_rubric_does_not_include_ollama_routing(self) -> None:
        assert "Route to `claude-code-ollama`" not in self._prompt()

    def test_includes_rubric_haiku_criteria(self) -> None:
        assert "Route to `claude-code-haiku` when" in self._prompt()

    def test_includes_rubric_sonnet_criteria(self) -> None:
        assert "Route to `claude-code-sonnet` when" in self._prompt()

    def test_includes_rubric_anthropic_criteria(self) -> None:
        assert (
            "Route to `claude-code` (default Opus) when ANY of these hold"
            in self._prompt()
        )

    def test_includes_engine_selection_step(self) -> None:
        prompt = self._prompt()
        assert "build_engine" in prompt

    def test_structured_fields_update_item_call(self) -> None:
        prompt = self._prompt()
        assert "acceptance_criteria" in prompt
        assert "files_to_modify" in prompt
        assert "scope_out" in prompt

    def test_no_markdown_section_write_instructions(self) -> None:
        prompt = self._prompt()
        # Prompt must NOT instruct the agent to write Markdown sections into description
        assert "## Acceptance Criteria" not in prompt
        assert "## Files to Modify" not in prompt
        assert "## Out of scope" not in prompt

    def test_no_build_engine_dual_write_to_description(self) -> None:
        prompt = self._prompt()
        # Prompt must NOT tell the agent to append "Build engine: ..." to description
        assert "Build engine: <engine-name>" not in prompt
        assert "Build engine: claude-code (default)" not in prompt

    def test_legality_validation_note_in_rules(self) -> None:
        prompt = self._prompt()
        assert "Legality validation reads" in prompt

    def test_step_zero_doc_reading_present(self) -> None:
        assert "docs/codebase.md" in self._prompt()

    def test_step_zero_claude_md_fallback_present(self) -> None:
        assert "CLAUDE.md" in self._prompt()

    def test_step_zero_kb_search_present(self) -> None:
        assert "kb_search" in self._prompt()

    def test_step_zero_kb_search_uses_project_ref(self) -> None:
        prompt = self._prompt()
        assert "project_ref=" in prompt
        assert "my-cool-project" in prompt

    def test_step_zero_architectural_magic_strings_present(self) -> None:
        prompt = self._prompt()
        assert any(phrase in prompt for phrase in ["Literal", "enum"])

    def test_step_zero_architectural_duplication_present(self) -> None:
        prompt = self._prompt()
        assert any(
            phrase in prompt for phrase in ["shared", "duplicate", "duplicating"]
        )

    def test_step_zero_architectural_typed_home_present(self) -> None:
        prompt = self._prompt()
        assert any(
            phrase in prompt
            for phrase in ["Pydantic", "TypedDict", "dataclass", "typed home"]
        )

    def test_step_zero_appears_before_what_to_do(self) -> None:
        prompt = self._prompt()
        step_zero_idx = prompt.find("## Before You Begin")
        what_to_do_idx = prompt.find("## What to do")
        assert step_zero_idx != -1
        assert what_to_do_idx != -1
        assert step_zero_idx < what_to_do_idx

    def test_build_mode_does_not_include_rubric(self) -> None:
        prompt = build_system_prompt(
            self._item, self._project, "feat/abc12345", 30, mode="build"
        )
        assert "Route to `claude-code-ollama`" not in prompt

    def test_manage_mode_does_not_include_rubric(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            None,
            30,
            mode="manage",
            rollout_id="wr-test-rollout",
        )
        assert "Route to `claude-code-ollama`" not in prompt


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


def _make_mock_proc(returncode: int = 0) -> object:
    """Create a mock subprocess.Popen process."""
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    return mock_proc


class TestRunAgent:
    async def test_calls_subprocess_with_engine_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            args, _kwargs = mock_popen.call_args
            assert args[0][0] == "claude"
            assert "--dangerously-skip-permissions" in args[0]

    async def test_passes_workspace_as_cwd(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_popen.call_args
            assert kwargs["cwd"] == tmp_path

    async def test_passes_timeout_from_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 42)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            mock_proc.wait.assert_called_once_with(timeout=42)

    async def test_env_filtered_by_engine(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # must be filtered
        monkeypatch.setenv("KIRO_API_KEY", "kiro-secret")
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_popen.call_args
            assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-test"  # noqa: S105
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]  # kb-01512
            assert "KIRO_API_KEY" not in kwargs["env"]

    async def test_agent_name_passes_through_to_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, agent_name="my-agent")
            args, _kwargs = mock_popen.call_args
            cmd = args[0]
            idx = cmd.index("--agent")
            assert cmd[idx + 1] == "my-agent"

    async def test_returns_completed_process(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            result = await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            assert result.returncode == 0
            # stdout/stderr are always "" with Popen streaming to file
            assert result.stdout == ""
            assert result.stderr == ""

    async def test_explicit_timeout_seconds_overrides_config(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 999)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, timeout_seconds=120)
            mock_proc.wait.assert_called_once_with(timeout=120)

    async def test_no_timeout_seconds_falls_back_to_config(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 300)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            mock_proc.wait.assert_called_once_with(timeout=300)

    async def test_kiro_writes_system_prompt_md(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(KIRO, tmp_path, "sys prompt", "Fix bug", 20)
            md = (tmp_path / "system_prompt.md").read_text()
            assert "sys prompt" in md
            assert "Fix bug" in md

    async def test_claude_does_not_write_system_prompt_md(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            assert not (tmp_path / "system_prompt.md").exists()

    async def test_allowed_tools_appended_to_claude_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        tools = ["mcp__agent-gtd__advance_rollout", "Read"]
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, allowed_tools=tools)
            args, _kwargs = mock_popen.call_args
            cmd = args[0]
            assert "--allowedTools" in cmd
            idx = cmd.index("--allowedTools")
            assert cmd[idx + 1] == "mcp__agent-gtd__advance_rollout,Read"
            # Title remains the final argument
            assert cmd[-1] == "Title"
            # --allowedTools must come BEFORE --print, otherwise claude's
            # argparser swallows the positional prompt and errors with
            # "Input must be provided ... when using --print".  This is a
            # regression guard for that specific failure mode.
            assert idx < cmd.index("--print")

    async def test_non_claude_engine_ignores_allowed_tools(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        tools = ["mcp__agent-gtd__advance_rollout", "Read"]
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(KIRO, tmp_path, "sys", "Title", 20, allowed_tools=tools)
            args, _kwargs = mock_popen.call_args
            cmd = args[0]
            assert "--allowedTools" not in cmd

    async def test_popen_streams_to_transcript_file(
        self, tmp_path, monkeypatch
    ) -> None:
        """Verify transcript.txt is opened for writing and Popen receives it as stdout."""
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_popen.call_args
            # stdout= must be a file object (not PIPE/STDOUT)
            assert hasattr(kwargs["stdout"], "write")
            assert kwargs["stderr"] == subprocess.STDOUT

        # transcript.txt is opened in "wb" mode — file exists even though mock wrote nothing
        assert (tmp_path / "transcript.txt").exists()

    async def test_writes_transcript_after_successful_run(
        self, tmp_path, monkeypatch
    ) -> None:
        """transcript.txt is created (opened in wb mode) during the run."""
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)

        transcript_path = tmp_path / "transcript.txt"
        assert transcript_path.exists(), "transcript.txt must be created on run start"

    async def test_attribution_sets_agent_name_env(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(
                CLAUDE,
                tmp_path,
                "sys",
                "Title",
                20,
                attribution="claude-build-abc12345",
            )
            _, kwargs = mock_popen.call_args
            assert kwargs["env"]["AGENT_GTD_AGENT_NAME"] == "claude-build-abc12345"

    async def test_no_attribution_does_not_set_agent_name_env(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_popen.call_args
            assert "AGENT_GTD_AGENT_NAME" not in kwargs["env"]

    async def test_manage_mode_uses_manage_timeout_when_none(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "MANAGE_TIMEOUT_SECONDS", 14400)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 200, mode="manage")
            mock_proc.wait.assert_called_once_with(timeout=14400)

    async def test_build_mode_uses_timeout_seconds_when_none(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 300)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 200, mode="build")
            mock_proc.wait.assert_called_once_with(timeout=300)


class TestSudoWrapping:
    async def test_run_agent_sudo_prefix_when_user_set(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            args, _kwargs = mock_popen.call_args
            cmd = args[0]
            assert cmd[:4] == ["sudo", "-u", "dispatch", "-H"]

    async def test_run_agent_no_sudo_prefix_when_user_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        mock_proc = _make_mock_proc(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            args, _kwargs = mock_popen.call_args
            cmd = args[0]
            assert cmd[0] == "claude"

    def test_prepare_workspace_sudo_prefix_when_user_set(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            prepare_workspace(origin, run_id, branch)
        assert mock_sub.run.call_args_list[0].args[0][:4] == [
            "sudo",
            "-u",
            "dispatch",
            "-H",
        ]

    @pytest.mark.skipif(
        not _DISPATCH_SUDO_AVAILABLE,
        reason="requires 'dispatch' system user and passwordless sudo access",
    )
    def test_sudo_wrap_path_includes_local_bin(self, monkeypatch) -> None:
        from agent_gtd_dispatch.dispatch import _sudo_wrap

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        env = build_env(CLAUDE)
        cmd = _sudo_wrap(["bash", "-c", "printf '%s' \"$PATH\""])
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=10
        )
        assert "/home/dispatch/.local/bin" in result.stdout


class TestBuildManagePrompt:
    _project: ClassVar[dict] = {
        "name": "wave-project",
        "git_origin": "git@host:repos/wp",
    }
    _rollout_id = "wr-abc123"
    _max_turns = 100

    def _prompt(
        self,
        rollout_id: str | None = None,
        project: dict | None = None,
        max_turns: int | None = None,
    ) -> str:
        return build_system_prompt(
            item={"id": "item-1", "title": "ignored for manage mode"},
            project=project or self._project,
            branch_name=None,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            mode="manage",
            rollout_id=rollout_id if rollout_id is not None else self._rollout_id,
        )

    def test_includes_rollout_id(self) -> None:
        prompt = self._prompt()
        assert self._rollout_id in prompt

    def test_includes_project_name(self) -> None:
        prompt = self._prompt()
        assert "wave-project" in prompt

    def test_identifies_executor_role(self) -> None:
        prompt = self._prompt()
        assert "rollout-manager" in prompt or "executor" in prompt

    def test_does_not_include_branch_rules(self) -> None:
        prompt = self._prompt()
        assert "push" not in prompt.lower() or "never" in prompt.lower()
        assert "already on branch" not in prompt

    def test_routed_by_build_system_prompt(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project={"name": "my-project", "git_origin": "git@host:repos/mp"},
            branch_name=None,
            max_turns=50,
            mode="manage",
            rollout_id="wr-123",
        )
        assert "rollout-manager" in prompt or "executor" in prompt

    def test_all_wave_loop_steps_present(self) -> None:
        prompt = self._prompt()
        # Phase 2 has numbered steps
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt
        assert "Step 4" in prompt
        assert "Step 5" in prompt

    def test_advance_rollout_retry_logic_present(self) -> None:
        prompt = self._prompt()
        assert "retry" in prompt.lower()
        assert "3 times" in prompt or "3" in prompt

    def test_no_classifier_references(self) -> None:
        prompt = self._prompt()
        assert "classifier" not in prompt
        assert "squash_merge" not in prompt
        assert "halt_list" not in prompt
        assert "/ci-gate" not in prompt

    def test_max_turns_embedded(self) -> None:
        prompt = self._prompt(max_turns=42)
        assert "42" in prompt

    def test_manage_prompt_says_ignore_launch_item_id(self) -> None:
        prompt = self._prompt()
        lower = prompt.lower()
        assert any(
            phrase in lower
            for phrase in [
                "placeholder",
                "ignore",
                "do not act on",
                "do not mark complete",
            ]
        ), "Prompt must instruct executor to ignore the launch item_id placeholder"

    def test_manage_prompt_dispatch_step_includes_rollout_id(self) -> None:
        prompt = self._prompt()
        # The f-string interpolates rollout_id into the dispatch_item example call
        assert f'rollout_id="{self._rollout_id}"' in prompt, (
            "Step 2 dispatch_item call must include rollout_id "
            "interpolated with the actual value"
        )
        assert "REQUIRED" in prompt or "required" in prompt.lower(), (
            "Prompt must state that rollout_id is required on every child dispatch"
        )

    def test_manage_prompt_halt_targets_offending_item(self) -> None:
        prompt = self._prompt()
        halt_section_idx = prompt.find("Halt path")
        assert halt_section_idx != -1, "Halt path section must be present in the prompt"
        halt_section = prompt[halt_section_idx:]
        assert "offending" in halt_section.lower() or "project_id" in halt_section, (
            "Halt path must target the offending rollout item or project_id, "
            "not the launch placeholder item_id"
        )

    def test_no_lower_coverage_threshold_guardrail(self) -> None:
        prompt = self._prompt()
        assert "fail_under" in prompt, (
            "Guardrails section must reference 'fail_under' (the pyproject.toml key)"
        )
        assert "NEVER lower" in prompt, (
            "Guardrails section must include the exact phrase 'NEVER lower'"
        )

    def test_guardrail_no_verify_and_type_ignore(self) -> None:
        prompt = self._prompt()
        assert "--no-verify" in prompt, (
            "Guardrails section must explicitly prohibit 'git push --no-verify'"
        )
        assert "type: ignore" in prompt, (
            "Guardrails section must explicitly prohibit blanket '# type: ignore' suppressions"
        )

    def test_warm_up_phase_present(self) -> None:
        prompt = self._prompt()
        assert "uv sync" in prompt
        assert "npm install" in prompt
        assert "pre-commit install" in prompt

    def test_sensitive_area_guidance_present(self) -> None:
        prompt = self._prompt()
        assert "auth" in prompt.lower()
        assert "deploy.sh" in prompt or "release.sh" in prompt
        assert ".github" in prompt
        assert "Dockerfile" in prompt or "*.service" in prompt
        assert ".env" in prompt

    def test_squash_merge_instructions_present(self) -> None:
        prompt = self._prompt()
        assert "git merge --squash" in prompt

    def test_complete_in_rollout_actor_and_rule(self) -> None:
        prompt = self._prompt()
        assert "merge_actor" in prompt
        assert "manager-autonomous" in prompt
        assert "decision_rule" in prompt
        assert "agent-judgment" in prompt

    # -----------------------------------------------------------------------
    # Recovery prompt rendering
    # -----------------------------------------------------------------------

    def test_no_recovery_block_at_count_zero(self) -> None:
        """manage_retry_count=0 (default) must NOT inject recovery context."""
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            manage_retry_count=0,
        )
        assert "Recovery Context" not in prompt
        assert "recovery" not in prompt.lower() or "non-recoverable" in prompt.lower()

    def test_recovery_block_at_count_one(self) -> None:
        """manage_retry_count=1 should prepend the recovery context block."""
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            manage_retry_count=1,
        )
        assert "Recovery Context" in prompt
        assert "retry attempt 1 of 2" in prompt
        assert "recovery" in prompt.lower()

    def test_recovery_block_at_count_two(self) -> None:
        """manage_retry_count=2 (max) should show 'attempt 2 of 2'."""
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            manage_retry_count=2,
        )
        assert "Recovery Context" in prompt
        assert "retry attempt 2 of 2" in prompt

    def test_recovery_block_appears_before_main_prompt(self) -> None:
        """Recovery context must appear before Phase 1 content."""
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            manage_retry_count=1,
        )
        recovery_idx = prompt.find("Recovery Context")
        phase1_idx = prompt.find("Phase 1")
        assert recovery_idx != -1
        assert phase1_idx != -1
        assert recovery_idx < phase1_idx, (
            "Recovery Context block must appear before Phase 1 content"
        )

    def test_branch_delete_on_squash_merge(self) -> None:
        prompt = self._prompt()
        assert "git push origin --delete" in prompt, (
            "Step 6 squash-merge sequence must include 'git push origin --delete' "
            "to remove the feature branch from origin after merging"
        )

    def test_step_6_commit_count_guard_present(self) -> None:
        prompt = self._prompt()
        assert any(
            phrase in prompt
            for phrase in [
                "rev-list",
                "no commits",
                "commits beyond",
                "pushed no commits",
            ]
        ), "Step 6 must contain commit-count check language"


# ---------------------------------------------------------------------------
# AC-1.1 — rollout_id schema tests
# ---------------------------------------------------------------------------


class TestRolloutIdSchema:
    def test_dispatch_request_accepts_rollout_id(self) -> None:
        req = DispatchRequest(item_id="abc", max_turns=50, rollout_id="wr-123")
        assert req.rollout_id == "wr-123"

    def test_dispatch_request_rollout_id_defaults_none(self) -> None:
        req = DispatchRequest(item_id="abc", max_turns=50)
        assert req.rollout_id is None

    def test_run_model_accepts_rollout_id(self) -> None:
        run = Run(
            item_id="abc",
            project_name="proj",
            branch_name="feat/x",
            rollout_id="wr-xyz",
        )
        assert run.rollout_id == "wr-xyz"

    def test_run_model_rollout_id_defaults_none(self) -> None:
        run = Run(item_id="abc", project_name="proj", branch_name="feat/x")
        assert run.rollout_id is None


class TestDbRolloutId:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    async def test_runs_table_has_rollout_id_column(self) -> None:
        await db.init_db()
        async with aiosqlite.connect(db.db_path()) as conn:
            cursor = await conn.execute("PRAGMA table_info(runs)")
            cols = {row[1] for row in await cursor.fetchall()}
        assert "rollout_id" in cols

    async def test_list_runs_by_rollout_returns_correct_runs(self) -> None:
        await db.init_db()

        run1 = Run(item_id="i1", project_name="p", branch_name="b", rollout_id="wr-abc")
        run2 = Run(item_id="i2", project_name="p", branch_name="b", rollout_id="wr-abc")
        run3 = Run(item_id="i3", project_name="p", branch_name="b", rollout_id="wr-xyz")

        await db.insert_run(run1)
        await db.insert_run(run2)
        await db.insert_run(run3)

        results = await db.list_runs_by_rollout("wr-abc")
        assert len(results) == 2
        result_ids = {r.id for r in results}
        assert run1.id in result_ids
        assert run2.id in result_ids
        assert run3.id not in result_ids

    async def test_list_runs_by_rollout_returns_empty_for_unknown_rollout(self) -> None:
        await db.init_db()
        results = await db.list_runs_by_rollout("wr-nonexistent")
        assert results == []

    async def test_rollout_id_persisted_and_retrieved(self) -> None:
        await db.init_db()
        run = Run(
            item_id="i1", project_name="p", branch_name="b", rollout_id="wr-persist"
        )
        await db.insert_run(run)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.rollout_id == "wr-persist"

    async def test_branch_name_nullable_in_schema(self) -> None:
        await db.init_db()
        async with aiosqlite.connect(db.db_path()) as conn:
            cursor = await conn.execute("PRAGMA table_info(runs)")
            cols = await cursor.fetchall()
        branch_col = next(c for c in cols if c[1] == "branch_name")
        assert branch_col[3] == 0, (
            f"branch_name should be nullable (notnull=0), got {branch_col[3]}"
        )

    async def test_insert_run_with_null_branch_name(self) -> None:
        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name=None, mode="manage")
        await db.insert_run(run)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.branch_name is None


class TestDbWorkspacePath:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    async def test_workspace_path_column_exists(self) -> None:
        await db.init_db()
        async with aiosqlite.connect(db.db_path()) as conn:
            cursor = await conn.execute("PRAGMA table_info(runs)")
            cols = {row[1] for row in await cursor.fetchall()}
        assert "workspace_path" in cols

    async def test_workspace_path_none_by_default(self) -> None:
        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name="b")
        await db.insert_run(run)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.workspace_path is None

    async def test_workspace_path_persisted_via_update(self) -> None:
        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name="b")
        await db.insert_run(run)
        await db.update_run(run.id, workspace_path="/tmp/ws-abc")  # noqa: S108
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.workspace_path == "/tmp/ws-abc"  # noqa: S108

    async def test_workspace_path_set_on_insert(self) -> None:
        await db.init_db()
        run = Run(
            item_id="i1",
            project_name="p",
            branch_name="b",
            workspace_path="/tmp/ws-xyz",  # noqa: S108
        )
        await db.insert_run(run)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.workspace_path == "/tmp/ws-xyz"  # noqa: S108

    async def test_update_run_with_exit_code_and_error(self) -> None:
        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name="b")
        await db.insert_run(run)
        await db.update_run(run.id, exit_code=1, error="something went wrong")
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.exit_code == 1
        assert fetched.error == "something went wrong"

    async def test_update_run_noop_when_no_fields(self) -> None:
        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name="b")
        await db.insert_run(run)
        # Should not raise when called with no keyword args
        await db.update_run(run.id)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.status.value == "pending"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestManageEnvKeys:
    def test_build_env_includes_dispatch_keys_for_manage_claude(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8080")
        monkeypatch.setenv("DISPATCH_API_KEY", "mgr-key")
        env = build_env(CLAUDE, mode="manage")
        assert "DISPATCH_LOCAL_URL" in env
        assert "DISPATCH_API_KEY" in env

    def test_build_env_excludes_dispatch_keys_for_build_claude(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8080")
        env = build_env(CLAUDE, mode="build")
        assert "DISPATCH_LOCAL_URL" not in env

    def test_build_env_excludes_dispatch_keys_for_manage_kiro(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8080")
        env = build_env(KIRO, mode="manage")
        assert "DISPATCH_LOCAL_URL" not in env

    async def test_manage_mode_passed_to_run_agent_env(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8080")
        monkeypatch.setenv("DISPATCH_API_KEY", "mgr-key")
        mock_proc = _make_mock_proc(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, mode="manage")
            _, kwargs = mock_popen.call_args
            assert kwargs["env"].get("DISPATCH_LOCAL_URL") == "http://localhost:8080"


class TestPrepareManageWorkspace:
    @pytest.fixture
    def workspace_root(self, tmp_path, monkeypatch) -> object:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        return tmp_path

    def test_workspace_path_uses_run_id(self, workspace_root, tmp_path) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return _completed(0, stdout="origin/main\n")
            return _completed(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
        ):
            result = prepare_manage_workspace(origin, run_id)

        expected = tmp_path / f"repos-{run_id}"
        assert result == expected

    def test_calls_correct_git_commands(self, workspace_root, tmp_path) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        expected_workspace = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return _completed(0, stdout="origin/main\n")
            return _completed(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace(origin, run_id)

        assert mock_sub.call_count == 4
        calls = mock_sub.call_args_list

        # 1. git clone --depth=50
        assert calls[0] == call(
            ["git", "clone", "--depth=50", origin, str(expected_workspace)],
            check=True,
            capture_output=True,
        )
        # 2. git remote set-head origin --auto (non-fatal: check=False)
        assert calls[1] == call(
            ["git", "remote", "set-head", "origin", "--auto"],
            cwd=expected_workspace,
            check=False,
            capture_output=True,
        )
        # 3. git symbolic-ref (with text=True; check=False so failure is handled)
        assert calls[2] == call(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=expected_workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        # 4. git checkout <default_branch>
        assert calls[3] == call(
            ["git", "checkout", "main"],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )

    def test_strips_origin_prefix_from_branch(self, workspace_root, tmp_path) -> None:
        """Verify 'origin/main' stdout → checks out 'main'."""
        origin = "git@host:repos/myrepo"
        run_id = "xyz789"
        expected_workspace = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return _completed(0, stdout="origin/master\n")
            return _completed(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace(origin, run_id)

        checkout_call = mock_sub.call_args_list[3]
        assert checkout_call == call(
            ["git", "checkout", "master"],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )

    def test_creates_workspace_root_if_missing(self, tmp_path, monkeypatch) -> None:
        nested_root = tmp_path / "nonexistent" / "workspace"
        monkeypatch.setattr(config, "WORKSPACE_ROOT", nested_root)

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return _completed(0, stdout="origin/main\n")
            return _completed(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
        ):
            prepare_manage_workspace("git@host:repos/myrepo", "abc123")

        assert nested_root.exists()

    def test_set_head_auto_failure_falls_back_to_branch_probe(
        self, workspace_root, tmp_path
    ) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        expected_workspace = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "set-head" in cmd:
                return _completed(1)
            if "symbolic-ref" in cmd:
                return _completed(1)
            if "branch" in cmd and "-r" in cmd:
                return _completed(0, stdout="origin/main\norigin/HEAD\n")
            return _completed(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace(origin, run_id)

        checkout_call = mock_sub.call_args_list[-1]
        assert checkout_call == call(
            ["git", "checkout", "main"],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )

    def test_set_head_auto_failure_falls_back_to_master(
        self, workspace_root, tmp_path
    ) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        expected_workspace = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "set-head" in cmd:
                return _completed(1)
            if "symbolic-ref" in cmd:
                return _completed(1)
            if "branch" in cmd and "-r" in cmd:
                return _completed(0, stdout="origin/master\n")
            return _completed(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace(origin, run_id)

        checkout_call = mock_sub.call_args_list[-1]
        assert checkout_call == call(
            ["git", "checkout", "master"],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )

    def test_set_head_auto_nonfatal_does_not_raise(
        self, workspace_root, tmp_path
    ) -> None:
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        expected_workspace = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "set-head" in cmd:
                return _completed(1)  # set-head fails, but must not raise
            if "symbolic-ref" in cmd:
                return _completed(0, stdout="origin/main\n")
            return _completed(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            # Must not raise despite set-head returning non-zero
            prepare_manage_workspace(origin, run_id)

        checkout_call = mock_sub.call_args_list[-1]
        assert checkout_call == call(
            ["git", "checkout", "main"],
            cwd=expected_workspace,
            check=True,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Executor sizing tests (AC2, AC5, AC6)
# ---------------------------------------------------------------------------


@pytest.fixture
def _env(tmp_path, monkeypatch):
    """Set required env vars and use tmp path for workspace/db.

    Mirrors the pattern from test_api.py so config.load() succeeds.
    """
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


class TestInitExecutor:
    def test_creates_executor_with_default_max_workers(self, _env) -> None:
        """init_executor() with default config uses MAX_CONCURRENT_RUNS=32."""
        init_executor()
        assert dispatch._executor is not None
        assert dispatch._executor._max_workers == 32

    def test_creates_executor_with_configured_max_workers(
        self, tmp_path, monkeypatch
    ) -> None:
        """init_executor() respects DISPATCH_MAX_CONCURRENT_RUNS env var."""
        env = {
            "DISPATCH_API_KEY": "test-key",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "test-gtd-key",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "DISPATCH_MAX_CONCURRENT_RUNS": "3",
        }
        with patch.dict(os.environ, env):
            config.load()
            init_executor()
        assert dispatch._executor is not None
        assert dispatch._executor._max_workers == 3

    def test_shuts_down_old_executor_on_reinit(self, _env) -> None:
        """Calling init_executor() twice replaces the old executor."""
        init_executor()
        first_executor = dispatch._executor
        init_executor()
        second_executor = dispatch._executor
        assert second_executor is not first_executor

    def test_executor_set_after_init(self, _env) -> None:
        """_executor module-level var is None before init, set after."""
        import agent_gtd_dispatch.dispatch as dispatch_module

        dispatch_module._executor = None
        init_executor()
        assert dispatch_module._executor is not None


class TestMaxConcurrentRunsConfig:
    def test_default_is_32(self, _env) -> None:
        """MAX_CONCURRENT_RUNS defaults to 32 when env var is absent."""
        assert config.MAX_CONCURRENT_RUNS == 32

    def test_reads_from_env_var(self, tmp_path) -> None:
        """MAX_CONCURRENT_RUNS reads DISPATCH_MAX_CONCURRENT_RUNS from env."""
        env = {
            "DISPATCH_API_KEY": "test-key",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "test-gtd-key",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "DISPATCH_MAX_CONCURRENT_RUNS": "9",
        }
        with patch.dict(os.environ, env):
            config.load()
        assert config.MAX_CONCURRENT_RUNS == 9

    def test_executor_wired_to_config(self, tmp_path) -> None:
        """End-to-end: env var → config.load() → init_executor() → _executor._max_workers."""
        env = {
            "DISPATCH_API_KEY": "test-key",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "test-gtd-key",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "DISPATCH_MAX_CONCURRENT_RUNS": "9",
        }
        with patch.dict(os.environ, env):
            config.load()
            init_executor()
        assert config.MAX_CONCURRENT_RUNS == 9
        assert dispatch._executor is not None
        assert dispatch._executor._max_workers == 9


class TestClaudeOllamaEngine:
    def test_engine_registered(self) -> None:
        assert get_engine("claude-code-ollama") is CLAUDE_OLLAMA

    def test_binary_is_claude(self) -> None:
        assert CLAUDE_OLLAMA.binary == "claude"

    def test_env_injects_base_url(self, monkeypatch) -> None:
        monkeypatch.setattr(
            config, "OLLAMA_BASE_URL", "http://10.0.0.5:11434"
        )  # no /v1
        monkeypatch.setattr(config, "OLLAMA_API_KEY", "ollama")
        monkeypatch.setattr(config, "OLLAMA_DEFAULT_MODEL", "qwen3.5:35b")
        env = build_env(CLAUDE_OLLAMA)
        assert env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:11434"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"  # noqa: S105
        assert (
            "ANTHROPIC_MODEL" not in env
        )  # AC-3: model comes from --model flag, not env

    def test_env_does_not_include_oauth_token(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "should-not-appear")
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://10.0.0.5:11434")
        monkeypatch.setattr(config, "OLLAMA_API_KEY", "ollama")
        monkeypatch.setattr(config, "OLLAMA_DEFAULT_MODEL", "qwen3.5:35b")
        env = build_env(CLAUDE_OLLAMA)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_env_does_not_include_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard: ANTHROPIC_API_KEY must never appear even for Ollama engine
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://10.0.0.5:11434")
        monkeypatch.setattr(config, "OLLAMA_API_KEY", "ollama")
        monkeypatch.setattr(config, "OLLAMA_DEFAULT_MODEL", "qwen3.5:35b")
        env = build_env(CLAUDE_OLLAMA)
        assert "ANTHROPIC_API_KEY" not in env

    def test_command_builder_same_structure_as_claude(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "OLLAMA_DEFAULT_MODEL", "qwen3.5:35b")
        cmd = CLAUDE_OLLAMA.build_command("sys", "Fix bug", 20, None)
        assert cmd[0] == "claude"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "qwen3.5:35b"
        assert "--dangerously-skip-permissions" in cmd
        assert "--print" in cmd
        assert cmd[-1] == "Fix bug"

    def test_vanilla_claude_command_uses_opus_model(self) -> None:
        cmd = CLAUDE.build_command("sys", "Fix bug", 20, None)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"


class TestClaudeSonnetEngine:
    def test_engine_registered(self) -> None:
        assert get_engine("claude-code-sonnet") is CLAUDE_SONNET

    def test_binary_is_claude(self) -> None:
        assert CLAUDE_SONNET.binary == "claude"

    def test_command_builder_has_model_flag(self) -> None:
        cmd = CLAUDE_SONNET.build_command("sys", "Fix bug", 20, None)
        assert cmd[0] == "claude"
        assert cmd[1] == "--model"
        assert cmd[2] == "sonnet"
        assert "--dangerously-skip-permissions" in cmd
        assert "--print" in cmd
        assert cmd[-1] == "Fix bug"

    def test_env_includes_oauth_token(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        env = build_env(CLAUDE_SONNET)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-token"  # noqa: S105

    def test_env_excludes_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard: ANTHROPIC_API_KEY must never appear — see kb-01512
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        env = build_env(CLAUDE_SONNET)
        assert "ANTHROPIC_API_KEY" not in env

    def test_no_extra_env_fn(self) -> None:
        assert CLAUDE_SONNET.extra_env_fn is None

    def test_no_ollama_env_injected(self, monkeypatch) -> None:
        env = build_env(CLAUDE_SONNET)
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env


class TestClaudeHaikuEngine:
    def test_engine_registered(self) -> None:
        assert get_engine("claude-code-haiku") is CLAUDE_HAIKU

    def test_binary_is_claude(self) -> None:
        assert CLAUDE_HAIKU.binary == "claude"

    def test_command_builder_has_model_flag(self) -> None:
        cmd = CLAUDE_HAIKU.build_command("sys", "Fix bug", 20, None)
        assert cmd[0] == "claude"
        assert cmd[1] == "--model"
        assert cmd[2] == "haiku"
        assert "--dangerously-skip-permissions" in cmd
        assert "--print" in cmd
        assert cmd[-1] == "Fix bug"

    def test_env_includes_oauth_token(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        env = build_env(CLAUDE_HAIKU)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-token"  # noqa: S105

    def test_env_excludes_anthropic_api_key(self, monkeypatch) -> None:
        # Regression guard: ANTHROPIC_API_KEY must never appear — see kb-01512
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        env = build_env(CLAUDE_HAIKU)
        assert "ANTHROPIC_API_KEY" not in env

    def test_no_extra_env_fn(self) -> None:
        assert CLAUDE_HAIKU.extra_env_fn is None

    def test_no_ollama_env_injected(self, monkeypatch) -> None:
        env = build_env(CLAUDE_HAIKU)
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env


class TestOllamaConfig:
    def test_defaults(self, _env) -> None:
        assert config.OLLAMA_BASE_URL == ""
        assert config.OLLAMA_API_KEY == "ollama"
        assert config.OLLAMA_DEFAULT_MODEL == "qwen3.6:35b"
        assert config.OLLAMA_TIMEOUT_MULTIPLIER == 2.0

    def test_reads_from_env(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_BASE_URL": "http://10.0.0.5:11434/v1",
            "OLLAMA_API_KEY": "mykey",
            "OLLAMA_DEFAULT_MODEL": "llama3:8b",
            "OLLAMA_TIMEOUT_MULTIPLIER": "3.5",
        }
        with patch.dict(os.environ, env):
            config.load()
        assert config.OLLAMA_BASE_URL == "http://10.0.0.5:11434/v1"
        assert config.OLLAMA_API_KEY == "mykey"
        assert config.OLLAMA_DEFAULT_MODEL == "llama3:8b"
        assert config.OLLAMA_TIMEOUT_MULTIPLIER == 3.5

    def test_empty_ollama_base_url_is_valid(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            # OLLAMA_BASE_URL absent = empty = valid
        }
        with patch.dict(os.environ, env, clear=True):
            config.load()  # must not raise
        assert config.OLLAMA_BASE_URL == ""

    def test_valid_http_url_is_accepted(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_BASE_URL": "http://192.168.1.52:11434",
        }
        with patch.dict(os.environ, env, clear=True):
            config.load()  # must not raise

    def test_valid_https_url_is_accepted(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_BASE_URL": "https://proxy.internal:8080",
        }
        with patch.dict(os.environ, env, clear=True):
            config.load()  # must not raise

    def test_missing_scheme_raises_value_error(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_BASE_URL": "192.168.1.52",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="OLLAMA_BASE_URL"),
        ):
            config.load()

    def test_missing_host_raises_value_error(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_BASE_URL": "http://",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="OLLAMA_BASE_URL"),
        ):
            config.load()


# ---------------------------------------------------------------------------
# repo_dir_from_url tests (AC-2)
# ---------------------------------------------------------------------------


class TestRepoDirFromUrl:
    def test_ssh_with_org_and_slash(self) -> None:
        assert repo_dir_from_url("git@host:org/repo.git") == "repo"

    def test_https_with_org_path(self) -> None:
        assert repo_dir_from_url("https://host/org/repo.git") == "repo"

    def test_ssh_url_multi_segment(self) -> None:
        assert (
            repo_dir_from_url("ssh://git@ubuntu-vm01/~/repos/agent_gtd") == "agent_gtd"
        )

    def test_scp_style_no_slash(self) -> None:
        assert repo_dir_from_url("git@host:repo.git") == "repo"

    def test_trailing_slash_tolerated(self) -> None:
        # URL with one trailing '/' yields same name as without
        assert repo_dir_from_url("git@host:org/repo.git/") == repo_dir_from_url(
            "git@host:org/repo.git"
        )

    def test_empty_basename_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            repo_dir_from_url("git@host:")


# ---------------------------------------------------------------------------
# prepare_workspace_multi tests (AC-3/4/5)
# ---------------------------------------------------------------------------


class TestPrepareWorkspaceMulti:
    @pytest.fixture
    def workspace_root(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        return tmp_path

    def _make_mock_result(self, returncode: int = 0) -> MagicMock:
        result = MagicMock()
        result.returncode = returncode
        result.stderr = b""
        return result

    def test_workspace_root_is_ws_run_id(self, workspace_root, tmp_path) -> None:
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"
        branch = "feat/abc123-fix"

        mock_result = self._make_mock_result(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            result = prepare_workspace_multi(repo_urls, run_id, branch)

        assert result == tmp_path / f"ws-{run_id}"

    def test_clones_repos_in_order(self, workspace_root, tmp_path) -> None:
        repo_urls = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]
        run_id = "abc123"
        branch = "feat/abc123-fix"
        root = tmp_path / f"ws-{run_id}"

        mock_result = self._make_mock_result(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            prepare_workspace_multi(repo_urls, run_id, branch)

        calls = mock_sub.run.call_args_list
        # calls: mkdir, clone-a, checkout-a, clone-b, checkout-b
        # Filter by checking the actual command list, not the string representation
        clone_calls = [c for c in calls if "clone" in c.args[0]]
        assert len(clone_calls) == 2
        assert str(root / "repo-a") in str(clone_calls[0])
        assert str(root / "repo-b") in str(clone_calls[1])

    def test_dir_names_derived_from_urls(self, workspace_root, tmp_path) -> None:
        repo_urls = [
            "https://host/org/my-service.git",
            "ssh://git@server/repos/core-lib",
        ]
        run_id = "xyz999"
        branch = "feat/xyz999-feature"
        root = tmp_path / f"ws-{run_id}"

        mock_result = self._make_mock_result(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            prepare_workspace_multi(repo_urls, run_id, branch)

        calls = mock_sub.run.call_args_list
        clone_cmds = [c.args[0] for c in calls if "clone" in str(c.args[0])]
        assert any(str(root / "my-service") in str(cmd) for cmd in clone_cmds)
        assert any(str(root / "core-lib") in str(cmd) for cmd in clone_cmds)

    def test_checkout_b_called_per_repo(self, workspace_root, tmp_path) -> None:
        repo_urls = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]
        run_id = "abc123"
        branch = "feat/abc123-fix"

        mock_result = self._make_mock_result(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            prepare_workspace_multi(repo_urls, run_id, branch)

        calls = mock_sub.run.call_args_list
        # Filter by checking the actual command list
        checkout_calls = [c for c in calls if "checkout" in c.args[0]]
        assert len(checkout_calls) == 2
        for c in checkout_calls:
            cmd = c.args[0]
            assert "checkout" in cmd
            assert "-b" in cmd
            assert branch in cmd

    def test_duplicate_dir_name_raises_before_subprocess(self, workspace_root) -> None:
        # Both URLs resolve to 'repo' — must raise before any subprocess call
        repo_urls = [
            "git@host:org/repo.git",
            "https://other-host/path/repo.git",
        ]
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub,
            pytest.raises(ValueError, match="repo"),
        ):
            prepare_workspace_multi(repo_urls, "run1", "feat/x")
        mock_sub.run.assert_not_called()

    def test_empty_list_raises_before_subprocess(self, workspace_root) -> None:
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub,
            pytest.raises(ValueError),
        ):
            prepare_workspace_multi([], "run1", "feat/x")
        mock_sub.run.assert_not_called()

    def test_clone_failure_raises_runtime_error_with_clone_and_url(
        self, workspace_root
    ) -> None:
        repo_urls = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]
        run_id = "abc123"
        branch = "feat/abc123-fix"

        success = self._make_mock_result(0)
        failure = self._make_mock_result(1)
        failure.stderr = b"fatal: repository not found\n"

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # mkdir (1), clone-a (2), checkout-a (3), clone-b fails (4)
            if call_count == 4:
                return failure
            return success

        with (
            patch(
                "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            prepare_workspace_multi(repo_urls, run_id, branch)

        msg = str(exc_info.value)
        assert "clone" in msg
        assert "git@host:org/repo-b.git" in msg

    def test_checkout_failure_raises_runtime_error_with_checkout_and_url(
        self, workspace_root
    ) -> None:
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"
        branch = "feat/abc123-fix"

        success = self._make_mock_result(0)
        failure = self._make_mock_result(1)
        failure.stderr = b"error: branch already exists\n"

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # mkdir (1), clone (2) succeeds, checkout (3) fails
            if call_count == 3:
                return failure
            return success

        with (
            patch(
                "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            prepare_workspace_multi(repo_urls, run_id, branch)

        msg = str(exc_info.value)
        assert "checkout" in msg
        assert "git@host:org/repo-a.git" in msg

    def test_sudo_prefix_when_user_set(self, tmp_path, monkeypatch) -> None:
        """All subprocess calls (mkdir, every clone, every checkout) are sudo-wrapped."""
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        repo_urls = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]
        run_id = "abc123"
        branch = "feat/abc123-fix"

        mock_result = self._make_mock_result(0)
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            prepare_workspace_multi(repo_urls, run_id, branch)

        sudo_prefix = ["sudo", "-u", "dispatch", "-H"]
        for c in mock_sub.run.call_args_list:
            cmd = c.args[0]
            assert cmd[:4] == sudo_prefix, f"Expected sudo prefix on call {cmd!r}"


# ---------------------------------------------------------------------------
# Launch-time check-and-clean (crash-orphaned workspace recovery)
# ---------------------------------------------------------------------------


def _make_local_git_repo(path: Path) -> str:
    """Init a git repo at *path* with one commit; return the HEAD SHA.

    The default branch name is whatever git is configured to use locally
    (typically 'main' or 'master') — callers should not assume either.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestPrepareWorkspaceLaunchClean:
    """Verify launch-time check-and-clean for crash-orphaned workspaces."""

    @pytest.fixture
    def workspace_root(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        return tmp_path

    # --- Unit tests (mock-based): stale workspace removed before clone ---

    def test_stale_single_repo_workspace_removed_before_clone(
        self, workspace_root, tmp_path
    ) -> None:
        """Pre-existing workspace from a crashed run is removed before cloning."""
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"
        expected_workspace = tmp_path / f"repos-myrepo-{run_id}"

        # Pre-create a stale workspace simulating a prior crashed run
        expected_workspace.mkdir(parents=True)
        sentinel = expected_workspace / "stale_leftover.txt"
        sentinel.write_text("crash artifact")
        assert sentinel.exists()

        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            prepare_workspace(origin, run_id, branch)

        # Stale workspace (and its contents) should be gone
        assert not sentinel.exists()
        # A fresh git clone should still be invoked
        clone_calls = [c for c in mock_sub.run.call_args_list if "clone" in c.args[0]]
        assert len(clone_calls) == 1

    def test_stale_multi_repo_workspace_removed_before_clone(
        self, workspace_root, tmp_path
    ) -> None:
        """Pre-existing multi-repo workspace root is removed before cloning."""
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"
        branch = "feat/abc123-fix"
        root = tmp_path / f"ws-{run_id}"

        # Pre-create a stale workspace root simulating a prior crashed run
        root.mkdir(parents=True)
        sentinel = root / "stale_leftover.txt"
        sentinel.write_text("crash artifact")
        assert sentinel.exists()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            prepare_workspace_multi(repo_urls, run_id, branch)

        # Stale workspace root should be gone
        assert not sentinel.exists()
        # mkdir + fresh clone should still be invoked
        clone_calls = [
            c for c in mock_sub.run.call_args_list if "clone" in str(c.args[0])
        ]
        assert len(clone_calls) == 1

    def test_no_stale_workspace_single_proceeds_normally(
        self, workspace_root, tmp_path
    ) -> None:
        """When no stale workspace exists, prepare_workspace runs unchanged."""
        origin = "git@host:repos/myrepo"
        run_id = "abc123"
        branch = "feat/abc123-fix-bug"
        expected_workspace = tmp_path / f"repos-myrepo-{run_id}"
        assert not expected_workspace.exists()

        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            result = prepare_workspace(origin, run_id, branch)

        assert result == expected_workspace
        clone_calls = [c for c in mock_sub.run.call_args_list if "clone" in c.args[0]]
        assert len(clone_calls) == 1

    def test_no_stale_workspace_multi_proceeds_normally(
        self, workspace_root, tmp_path
    ) -> None:
        """When no stale workspace root exists, prepare_workspace_multi runs unchanged."""
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"
        branch = "feat/abc123-fix"
        root = tmp_path / f"ws-{run_id}"
        assert not root.exists()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("agent_gtd_dispatch.dispatch.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            result = prepare_workspace_multi(repo_urls, run_id, branch)

        assert result == root
        clone_calls = [
            c for c in mock_sub.run.call_args_list if "clone" in str(c.args[0])
        ]
        assert len(clone_calls) == 1

    # --- Integration tests (real git): crash recovery + merge-base guarantee ---

    def test_crash_relaunch_branch_based_on_current_main(
        self, tmp_path, monkeypatch
    ) -> None:
        """After crash (cleanup skipped) + relaunch, branch is based on the current main tip.

        Verifies AC: branch merge-base equals current origin HEAD, not a stale base.
        """
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        monkeypatch.setattr(config, "WORKSPACE_ROOT", ws_root)

        # Set up a local "remote" repo with an initial commit
        remote = tmp_path / "origin_repo"
        first_sha = _make_local_git_repo(remote)

        origin = f"file://{remote}"
        run_id = "crashtest01"
        branch_name = f"feat/{run_id}-fix"
        name = repo_name_from_origin(origin)
        stale_workspace = ws_root / f"{name}-{run_id}"

        # Simulate crash: manually pre-create a stale workspace
        # (as if a prior run cloned and started working, then was OOM-killed)
        stale_workspace.mkdir()
        stale_marker = stale_workspace / "stale_crash_artifact.txt"
        stale_marker.write_text("orphaned by crash")

        # A sibling item merges in between — adds a new commit to origin/main
        (remote / "sibling_file.txt").write_text("sibling feature")
        subprocess.run(["git", "add", "."], cwd=remote, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "sibling merged"],
            cwd=remote,
            check=True,
            capture_output=True,
        )
        second_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=remote,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert first_sha != second_sha

        # Relaunch: prepare_workspace removes stale and clones fresh from current main
        result = prepare_workspace(origin, run_id, branch_name)

        # Stale content must be gone
        assert not stale_marker.exists(), "Crash-orphaned artifact must be removed"
        # Fresh clone must be present
        assert (result / ".git").exists(), "Fresh clone must have .git directory"
        # Branch is correctly checked out
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=result,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current_branch == branch_name
        # HEAD in the workspace equals the current origin HEAD (second_sha)
        workspace_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=result,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert workspace_head == second_sha, (
            f"Workspace HEAD ({workspace_head}) != current origin HEAD ({second_sha}); "
            "branch was not created from the current main tip"
        )
        # Merge-base of HEAD and origin default branch == second_sha
        # (branch is rooted on current main, not the stale first_sha)
        remote_default = (
            subprocess.run(
                ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                cwd=result,
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "origin/main"
        )
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", remote_default],
            cwd=result,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert merge_base == second_sha, (
            f"merge-base ({merge_base}) != current origin HEAD ({second_sha}); "
            f"branch is rooted on a stale base instead of current main"
        )

    def test_crash_relaunch_multi_repo_produces_clean_workspace(
        self, tmp_path, monkeypatch
    ) -> None:
        """After crash (cleanup skipped) + relaunch, multi-repo workspace is fresh."""
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        monkeypatch.setattr(config, "WORKSPACE_ROOT", ws_root)

        remote = tmp_path / "origin_repo"
        _make_local_git_repo(remote)
        origin = f"file://{remote}"

        run_id = "crashtest02"
        branch_name = f"feat/{run_id}-fix"
        root = ws_root / f"ws-{run_id}"

        # Simulate crash: pre-create stale multi-repo workspace root
        root.mkdir()
        stale_marker = root / "stale_multi_artifact.txt"
        stale_marker.write_text("crash orphan")

        # Add a new commit to origin (happened while the crashed run was orphaned)
        (remote / "new_feature.txt").write_text("new")
        subprocess.run(["git", "add", "."], cwd=remote, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "post-crash"],
            cwd=remote,
            check=True,
            capture_output=True,
        )
        latest_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=remote,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Relaunch
        result = prepare_workspace_multi([origin], run_id, branch_name)

        assert not stale_marker.exists(), "Crash-orphaned artifact must be removed"
        assert result == root
        # Per-repo clone must be present
        dir_name = repo_dir_from_url(origin)
        repo_path = result / dir_name
        assert (repo_path / ".git").exists(), "Fresh clone must have .git directory"
        # Branch is correctly created from current origin HEAD
        repo_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert repo_head == latest_sha, (
            f"Repo HEAD ({repo_head}) != current origin HEAD ({latest_sha})"
        )


# ---------------------------------------------------------------------------
# Workspace prompt tests (AC-8a / AC-8b)
# ---------------------------------------------------------------------------


class TestWorkspacePrompts:
    _item: ClassVar[dict] = {
        "id": "abc12345-0000-0000-0000-000000000000",
        "title": "Fix workspace bug",
        "description": "Something needs fixing.",
    }
    _project: ClassVar[dict] = {"name": "my-cool-project"}
    _branch = "feat/abc12345-fix-workspace-bug"
    _max_turns = 42
    _repo_dirs: ClassVar[list[str]] = ["repo-a", "repo-b"]

    # --- build prompt with workspace_repo_dirs ---

    def test_build_workspace_section_contains_header(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            self._branch,
            self._max_turns,
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "## Workspace Layout" in prompt

    def test_build_workspace_section_contains_both_dir_names(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            self._branch,
            self._max_turns,
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "repo-a" in prompt
        assert "repo-b" in prompt

    def test_build_workspace_section_contains_branch_name(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            self._branch,
            self._max_turns,
            workspace_repo_dirs=self._repo_dirs,
        )
        assert self._branch in prompt

    def test_build_workspace_section_contains_ls_remote(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            self._branch,
            self._max_turns,
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "ls-remote" in prompt

    # --- plan prompt with workspace_repo_dirs ---

    def test_plan_workspace_section_contains_header(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="plan",
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "## Workspace Layout" in prompt

    def test_plan_workspace_section_contains_both_dir_names(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="plan",
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "repo-a" in prompt
        assert "repo-b" in prompt

    def test_plan_workspace_section_no_ls_remote(self) -> None:
        """Plan layout section must NOT include push/verification instructions."""
        prompt = build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="plan",
            workspace_repo_dirs=self._repo_dirs,
        )
        assert "ls-remote" not in prompt

    def test_plan_workspace_section_no_branch_name(self) -> None:
        """Plan layout section must NOT reference a branch name."""
        prompt = build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="plan",
            workspace_repo_dirs=self._repo_dirs,
        )
        # branch_name is not a param of _build_plan_prompt — must not appear
        assert self._branch not in prompt

    # --- None → no workspace section (regression) ---

    def test_build_no_workspace_section_when_none(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            self._branch,
            self._max_turns,
            workspace_repo_dirs=None,
        )
        assert "## Workspace Layout" not in prompt

    def test_plan_no_workspace_section_when_none(self) -> None:
        prompt = build_system_prompt(
            self._item,
            self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="plan",
            workspace_repo_dirs=None,
        )
        assert "## Workspace Layout" not in prompt


# ---------------------------------------------------------------------------
# prepare_manage_workspace_multi tests (AC-10a)
# ---------------------------------------------------------------------------


class TestPrepareManageWorkspaceMulti:
    @pytest.fixture
    def workspace_root(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        return tmp_path

    def _make_mock_result(self, returncode: int = 0, stdout: str = "") -> MagicMock:
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = b""
        return result

    def test_workspace_root_is_repos_run_id(self, workspace_root, tmp_path) -> None:
        """Root is repos-{run_id} (not ws-{run_id})."""
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return self._make_mock_result(0, stdout="origin/main\n")
            return self._make_mock_result(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
        ):
            result = prepare_manage_workspace_multi(repo_urls, run_id)

        assert result == tmp_path / f"repos-{run_id}"

    def test_happy_path_two_repos(self, workspace_root, tmp_path) -> None:
        """Happy path: 2 repos cloned with --depth=50 and default-branch checkout."""
        repo_urls = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]
        run_id = "abc123"
        root = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return self._make_mock_result(0, stdout="origin/main\n")
            return self._make_mock_result(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            result = prepare_manage_workspace_multi(repo_urls, run_id)

        assert result == root

        calls = mock_sub.call_args_list
        clone_calls = [c for c in calls if "clone" in c.args[0]]
        assert len(clone_calls) == 2
        # Each clone uses --depth=50
        for c in clone_calls:
            assert "--depth=50" in c.args[0]
        # Each clone targets the correct destination
        assert str(root / "repo-a") in str(clone_calls[0])
        assert str(root / "repo-b") in str(clone_calls[1])

        checkout_calls = [c for c in calls if "checkout" in c.args[0]]
        assert len(checkout_calls) == 2
        for c in checkout_calls:
            cmd = c.args[0]
            assert "main" in cmd
            # Must NOT be a -b flag (no branch creation)
            assert "-b" not in cmd

    def test_returned_path_is_workspace_root(self, workspace_root, tmp_path) -> None:
        """Function returns the workspace root path, not a sub-repo path."""
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "xyz789"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return self._make_mock_result(0, stdout="origin/main\n")
            return self._make_mock_result(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
        ):
            result = prepare_manage_workspace_multi(repo_urls, run_id)

        assert result == tmp_path / f"repos-{run_id}"
        assert result.name == f"repos-{run_id}"

    def test_empty_list_raises_value_error_before_subprocess(
        self, workspace_root
    ) -> None:
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub,
            pytest.raises(ValueError, match="workspace_repos must not be empty"),
        ):
            prepare_manage_workspace_multi([], "run1")
        mock_sub.assert_not_called()

    def test_duplicate_dir_raises_value_error_before_subprocess(
        self, workspace_root
    ) -> None:
        repo_urls = [
            "git@host:org/repo.git",
            "https://other-host/path/repo.git",
        ]
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub,
            pytest.raises(ValueError, match="Duplicate workspace repo directory"),
        ):
            prepare_manage_workspace_multi(repo_urls, "run1")
        mock_sub.assert_not_called()

    def test_clone_failure_raises_runtime_error(self, workspace_root) -> None:
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"

        failure = self._make_mock_result(1)
        failure.stderr = b"fatal: repository not found\n"

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # mkdir (1), clone fails (2)
            if call_count == 2:
                return failure
            return self._make_mock_result(0)

        with (
            patch(
                "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            prepare_manage_workspace_multi(repo_urls, run_id)

        msg = str(exc_info.value)
        assert "clone" in msg
        assert "git@host:org/repo-a.git" in msg

    def test_checkout_failure_raises_runtime_error(self, workspace_root) -> None:
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"

        success = self._make_mock_result(0, stdout="origin/main\n")
        failure = self._make_mock_result(1)
        failure.stderr = b"error: pathspec 'main' did not match\n"

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return self._make_mock_result(0, stdout="origin/main\n")
            # mkdir (1), clone (2) ok, set-head (3) ok, symbolic-ref → counted above
            # checkout is after detection, fails
            if "checkout" in cmd:
                return failure
            return success

        with (
            patch(
                "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=side_effect
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            prepare_manage_workspace_multi(repo_urls, run_id)

        msg = str(exc_info.value)
        assert "checkout" in msg
        assert "git@host:org/repo-a.git" in msg

    def test_symbolic_ref_fallback_to_master(self, workspace_root, tmp_path) -> None:
        """When symbolic-ref fails and only 'master' is in remote branches, checks out master."""
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"
        root = tmp_path / f"repos-{run_id}"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "set-head" in cmd:
                return self._make_mock_result(1)
            if "symbolic-ref" in cmd:
                return self._make_mock_result(1)
            if "branch" in cmd and "-r" in cmd:
                return self._make_mock_result(0, stdout="origin/master\n")
            return self._make_mock_result(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace_multi(repo_urls, run_id)

        checkout_calls = [c for c in mock_sub.call_args_list if "checkout" in c.args[0]]
        assert len(checkout_calls) == 1
        assert checkout_calls[0] == call(
            ["git", "checkout", "master"],
            cwd=root / "repo-a",
            check=False,
            capture_output=True,
        )

    def test_sudo_prefix_when_user_set(self, tmp_path, monkeypatch) -> None:
        """All subprocess calls (mkdir, clone, detection, checkout) are sudo-wrapped."""
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        repo_urls = ["git@host:org/repo-a.git"]
        run_id = "abc123"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "symbolic-ref" in cmd:
                return self._make_mock_result(0, stdout="origin/main\n")
            return self._make_mock_result(0)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_sub:
            mock_sub.side_effect = side_effect
            prepare_manage_workspace_multi(repo_urls, run_id)

        sudo_prefix = ["sudo", "-u", "dispatch", "-H"]
        for c in mock_sub.call_args_list:
            cmd = c.args[0]
            assert cmd[:4] == sudo_prefix, f"Expected sudo prefix on call {cmd!r}"


# ---------------------------------------------------------------------------
# Workspace manage-prompt tests (AC-9)
# ---------------------------------------------------------------------------


class TestWorkspaceManagePrompt:
    _project: ClassVar[dict] = {
        "name": "wave-project",
        "id": "proj-abc123",
        "git_origin": "git@host:repos/wp",
    }
    _rollout_id = "wr-abc123"
    _max_turns = 100
    _repo_dirs: ClassVar[list[str]] = ["agent_gtd", "agent-gtd-dispatch"]

    def _ws_prompt(
        self,
        repo_dirs: list[str] | None = None,
        manage_retry_count: int = 0,
    ) -> str:
        return build_system_prompt(
            item={"id": "item-1", "title": "ignored for manage mode"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            workspace_repo_dirs=repo_dirs if repo_dirs is not None else self._repo_dirs,
            manage_retry_count=manage_retry_count,
        )

    def _mono_prompt(self) -> str:
        return build_system_prompt(
            item={"id": "item-1", "title": "ignored for manage mode"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
        )

    # --- AC-2: monorepo unchanged — workspace tokens absent ---

    def test_monorepo_lacks_workspace_tokens(self) -> None:
        prompt = self._mono_prompt()
        for tok in [
            "Workspace Repos",
            "workspace root",
            "Per-repo state:",
            "merged+pushed",
            "untouched",
            "ls-remote",
        ]:
            assert tok not in prompt, f"Monorepo prompt must not contain {tok!r}"

    def test_monorepo_has_git_origin(self) -> None:
        prompt = self._mono_prompt()
        assert "**Git Origin:**" in prompt

    def test_monorepo_has_git_clone_sentence(self) -> None:
        prompt = self._mono_prompt()
        assert "git clone of the project's default branch" in prompt

    # --- AC-3: workspace header ---

    def test_workspace_has_workspace_repos_header(self) -> None:
        prompt = self._ws_prompt()
        assert "**Workspace Repos:**" in prompt

    def test_workspace_repo_bullets_in_order(self) -> None:
        prompt = self._ws_prompt()
        assert "- `agent_gtd/`" in prompt
        assert "- `agent-gtd-dispatch/`" in prompt
        assert prompt.index("agent_gtd") < prompt.index("agent-gtd-dispatch")

    def test_workspace_lacks_git_origin(self) -> None:
        prompt = self._ws_prompt()
        assert "**Git Origin:**" not in prompt

    def test_workspace_pinned_sentence_present(self) -> None:
        prompt = self._ws_prompt()
        assert "workspace root" in prompt

    def test_workspace_monorepo_sentence_absent(self) -> None:
        prompt = self._ws_prompt()
        assert "git clone of the project's default branch" not in prompt

    # --- AC-4: per-repo warm-up ---

    def test_per_repo_default_branch_recording(self) -> None:
        prompt = self._ws_prompt()
        assert "rev-parse --abbrev-ref HEAD" in prompt

    def test_per_repo_install_conditionals(self) -> None:
        prompt = self._ws_prompt()
        assert "uv sync" in prompt
        assert "npm install" in prompt

    def test_per_repo_precommit_install(self) -> None:
        prompt = self._ws_prompt()
        assert "pre-commit install" in prompt

    def test_no_test_command_fallback_none(self) -> None:
        prompt = self._ws_prompt()
        # 'none' fallback for repos with no discoverable test command
        assert "none" in prompt.lower()

    def test_warmup_halt_template(self) -> None:
        prompt = self._ws_prompt()
        assert "warm-up failure in" in prompt

    # --- AC-5: pushed-repo discovery ---

    def test_ls_remote_discovery_present(self) -> None:
        prompt = self._ws_prompt()
        assert "ls-remote" in prompt
        assert "refs/heads/" in prompt

    def test_build_comment_crosscheck_both_directions(self) -> None:
        prompt = self._ws_prompt()
        # Both disagreement directions must be named
        assert "build comment" in prompt.lower() or "build agent" in prompt.lower()
        # Direction 1: comment claims but ls-remote disagrees
        assert "NOT confirm" in prompt or "does not confirm" in prompt.lower()
        # Direction 2: ls-remote shows but comment didn't list
        assert "did NOT list" in prompt or "not list" in prompt.lower()

    def test_no_push_verification_via_get_run_status(self) -> None:
        prompt = self._ws_prompt()
        # Must explicitly say NOT to use get_run_status for push verification
        assert "Do NOT use" in prompt
        push_section_idx = prompt.find("Do NOT use")
        push_section = prompt[push_section_idx : push_section_idx + 300]
        assert "get_run_status" in push_section

    # --- AC-6: review-all-then-merge ordering ---

    def test_review_all_before_merge(self) -> None:
        prompt = self._ws_prompt()
        assert "ALL" in prompt or "all pushed repos" in prompt.lower()
        # Review section appears before merge section
        review_idx = prompt.find("quality gates")
        merge_idx = prompt.find("Squash merge")
        assert review_idx != -1
        assert merge_idx != -1
        assert review_idx < merge_idx

    def test_per_repo_commit_count_guard(self) -> None:
        prompt = self._ws_prompt()
        assert "commit_count" in prompt
        assert "rev-list" in prompt
        # Per-repo default branch (not a global value)
        assert (
            "repo_default_branch" in prompt
            or "THAT repo" in prompt
            or "that repo" in prompt.lower()
        )

    # --- AC-7: inline-fix phase boundary + halt template ---

    def test_inline_fix_allowed_before_first_merge(self) -> None:
        prompt = self._ws_prompt()
        lower = prompt.lower()
        assert "inline" in lower
        assert "zero repos" in lower or "0 repos" in lower or "zero" in lower

    def test_inline_fix_prohibited_after_first_merge(self) -> None:
        prompt = self._ws_prompt()
        lower = prompt.lower()
        # Must say no inline fixes after first merge
        assert "no inline fix" in lower or "halt immediately" in lower

    def test_halt_template_per_repo_state_literal(self) -> None:
        prompt = self._ws_prompt()
        assert "Per-repo state:" in prompt
        assert "merged+pushed" in prompt
        assert "FAILED" in prompt
        assert "untouched" in prompt

    def test_halt_template_step_enumeration(self) -> None:
        prompt = self._ws_prompt()
        # <step> enumeration must be present
        for step in (
            "fetch",
            "gates",
            "commit-count-guard",
            "squash-merge",
            "push",
            "branch-cleanup",
        ):
            assert step in prompt, f"Step {step!r} must be in halt template enumeration"

    def test_halt_prohibitions(self) -> None:
        prompt = self._ws_prompt()
        lower = prompt.lower()
        assert (
            "do not roll back" in lower
            or "not roll back" in lower
            or "not revert" in lower
        )
        assert "force-push" in lower or "force push" in lower
        assert "do not continue" in lower or "not continue merging" in lower

    # --- AC-8: cleanup ---

    def test_per_repo_feature_branch_deletion(self) -> None:
        prompt = self._ws_prompt()
        assert "git push origin --delete <branch_name>" in prompt or (
            "git push origin --delete" in prompt and "git branch -D" in prompt
        )

    def test_manage_branch_cleanup_exactly_once(self) -> None:
        rollout_id = self._rollout_id
        prompt = self._ws_prompt()
        expected = f"feat/{rollout_id[:8]}-manage"
        assert prompt.count(expected) == 1, (
            f"Manage-branch cleanup string {expected!r} must appear exactly once"
        )

    def test_manage_branch_cleanup_in_first_repo(self) -> None:
        prompt = self._ws_prompt()
        first_repo = self._repo_dirs[0]
        cleanup_idx = prompt.find(f"feat/{self._rollout_id[:8]}-manage")
        assert cleanup_idx != -1
        # The first_repo directory reference should appear near the cleanup
        nearby = prompt[max(0, cleanup_idx - 200) : cleanup_idx + 200]
        assert first_repo in nearby

    # --- Recovery block in workspace mode (AC-9) ---

    def test_recovery_block_renders_in_workspace_mode(self) -> None:
        prompt = self._ws_prompt(manage_retry_count=1)
        assert "Recovery Context" in prompt
        assert "retry attempt 1" in prompt
        assert prompt.find("Recovery Context") < prompt.find("Phase 1"), (
            "Recovery block must appear before Phase 1"
        )

    # --- AC-2 workspace variant must lack monorepo-only tokens ---

    def test_workspace_variant_lacks_all_monorepo_tokens(self) -> None:
        prompt = self._ws_prompt()
        assert "**Git Origin:**" not in prompt
        assert "git clone of the project's default branch" not in prompt

    # --- None/empty → no workspace-only tokens (AC-2 regression) ---

    def test_none_workspace_repo_dirs_is_monorepo_variant(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            workspace_repo_dirs=None,
        )
        assert "**Git Origin:**" in prompt
        assert "workspace root" not in prompt

    def test_empty_workspace_repo_dirs_is_monorepo_variant(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project=self._project,
            branch_name=None,
            max_turns=self._max_turns,
            mode="manage",
            rollout_id=self._rollout_id,
            workspace_repo_dirs=[],
        )
        assert "**Git Origin:**" in prompt
        assert "workspace root" not in prompt
