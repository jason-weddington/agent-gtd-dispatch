"""Tests for core dispatch logic and engine definitions."""

from __future__ import annotations

import subprocess
from typing import ClassVar
from unittest.mock import call, patch

import aiosqlite
import pytest

from agent_gtd_dispatch import config, db
from agent_gtd_dispatch.dispatch import (
    _MANAGE_ALLOWED_TOOLS,
    _ci_gate_prompt_addendum,
    _detect_project_type,
    _heartbeat_prompt_addendum,
    branch_name_for_item,
    build_system_prompt,
    cleanup_workspace,
    prepare_workspace,
    repo_name_from_origin,
    run_agent,
    run_ci_gate,
)
from agent_gtd_dispatch.engines import CLAUDE, KIRO, build_env, get_engine
from agent_gtd_dispatch.models import DispatchRequest, Run


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
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # must be filtered
        monkeypatch.setenv("KIRO_API_KEY", "kiro-secret")
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_run.call_args
            assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-test"  # noqa: S105
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]  # kb-01512
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

    async def test_explicit_timeout_seconds_overrides_config(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 999)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, timeout_seconds=120)
            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 120

    async def test_no_timeout_seconds_falls_back_to_config(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 300)
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(CLAUDE, tmp_path, "sys", "Title", 20)
            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 300

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

    async def test_allowed_tools_appended_to_claude_command(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        tools = ["mcp__agent-gtd__advance_wave", "Read"]
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(
                CLAUDE, tmp_path, "sys", "Title", 20, allowed_tools=tools
            )
            args, _kwargs = mock_run.call_args
            cmd = args[0]
            assert "--allowedTools" in cmd
            idx = cmd.index("--allowedTools")
            assert cmd[idx + 1] == "mcp__agent-gtd__advance_wave,Read"
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
        tools = ["mcp__agent-gtd__advance_wave", "Read"]
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            await run_agent(
                KIRO, tmp_path, "sys", "Title", 20, allowed_tools=tools
            )
            args, _kwargs = mock_run.call_args
            cmd = args[0]
            assert "--allowedTools" not in cmd


class TestBuildManagePrompt:
    _project: ClassVar[dict] = {"name": "wave-project"}
    _wave_run_id = "wr-abc123"
    _max_turns = 100

    def _prompt(
        self,
        wave_run_id: str | None = None,
        project: dict | None = None,
        max_turns: int | None = None,
    ) -> str:
        return build_system_prompt(
            item={"id": "item-1", "title": "ignored for manage mode"},
            project=project or self._project,
            branch_name=None,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            mode="manage",
            wave_run_id=wave_run_id if wave_run_id is not None else self._wave_run_id,
        )

    def test_includes_wave_run_id(self) -> None:
        prompt = self._prompt()
        assert self._wave_run_id in prompt

    def test_includes_project_name(self) -> None:
        prompt = self._prompt()
        assert "wave-project" in prompt

    def test_identifies_executor_role(self) -> None:
        prompt = self._prompt()
        assert "wave-manager" in prompt or "executor" in prompt

    def test_does_not_include_branch_rules(self) -> None:
        prompt = self._prompt()
        assert "push" not in prompt.lower() or "never" in prompt.lower()
        assert "already on branch" not in prompt

    def test_routed_by_build_system_prompt(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project={"name": "my-project"},
            branch_name=None,
            max_turns=50,
            mode="manage",
            wave_run_id="wr-123",
        )
        assert "wave-manager" in prompt or "executor" in prompt

    def test_manage_allowed_tools_constant_is_not_empty(self) -> None:
        assert len(_MANAGE_ALLOWED_TOOLS) > 0
        assert "mcp__agent-gtd__advance_wave" in _MANAGE_ALLOWED_TOOLS
        assert "mcp__agent-gtd__complete_in_wave" in _MANAGE_ALLOWED_TOOLS
        assert "mcp__agent-gtd__halt_wave" in _MANAGE_ALLOWED_TOOLS

    def test_dispatch_item_in_allowed_tools(self) -> None:
        assert "mcp__agent-gtd__dispatch_item" in _MANAGE_ALLOWED_TOOLS

    def test_list_comments_in_allowed_tools(self) -> None:
        assert "mcp__agent-gtd__list_comments" in _MANAGE_ALLOWED_TOOLS

    def test_all_five_loop_steps_present(self) -> None:
        prompt = self._prompt()
        assert "STEP 1" in prompt
        assert "STEP 2" in prompt
        assert "STEP 3" in prompt
        assert "STEP 4" in prompt
        assert "STEP 5" in prompt

    def test_advance_wave_retry_logic_present(self) -> None:
        prompt = self._prompt()
        assert "retry" in prompt.lower()
        assert "3 times" in prompt or "3" in prompt

    def test_classifier_module_path_present(self) -> None:
        prompt = self._prompt()
        assert "wave_manager.classifier" in prompt

    def test_squash_merge_invocation_present(self) -> None:
        prompt = self._prompt()
        assert "squash_merge" in prompt

    def test_classifier_unavailable_fallback_present(self) -> None:
        prompt = self._prompt()
        assert "classifier unavailable" in prompt or "not importable" in prompt

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

    def test_manage_prompt_dispatch_step_includes_wave_run_id(self) -> None:
        prompt = self._prompt()
        # The f-string interpolates wave_run_id into the dispatch_item example call
        assert f'wave_run_id="{self._wave_run_id}"' in prompt, (
            "Step 2 dispatch_item call must include wave_run_id "
            "interpolated with the actual value"
        )
        assert "REQUIRED" in prompt or "required" in prompt.lower(), (
            "Prompt must state that wave_run_id is required on every child dispatch"
        )

    def test_manage_prompt_halt_does_not_target_launch_item(self) -> None:
        prompt = self._prompt()
        halt_idx = prompt.find("STEP 5b")
        assert halt_idx != -1, "STEP 5b must be present in the prompt"
        halt_section = prompt[halt_idx:]
        # The halt comment must target the offending wave item or project,
        # not the launch placeholder
        assert "offending" in halt_section.lower() or "project_id" in halt_section, (
            "STEP 5b halt comment must target the offending wave item or project_id, "
            "not the launch placeholder item_id"
        )


# ---------------------------------------------------------------------------
# AC-1.1 — wave_run_id schema tests
# ---------------------------------------------------------------------------


class TestWaveRunIdSchema:
    def test_dispatch_request_accepts_wave_run_id(self) -> None:
        req = DispatchRequest(item_id="abc", max_turns=50, wave_run_id="wr-123")
        assert req.wave_run_id == "wr-123"

    def test_dispatch_request_wave_run_id_defaults_none(self) -> None:
        req = DispatchRequest(item_id="abc", max_turns=50)
        assert req.wave_run_id is None

    def test_run_model_accepts_wave_run_id(self) -> None:
        run = Run(
            item_id="abc",
            project_name="proj",
            branch_name="feat/x",
            wave_run_id="wr-xyz",
        )
        assert run.wave_run_id == "wr-xyz"

    def test_run_model_wave_run_id_defaults_none(self) -> None:
        run = Run(item_id="abc", project_name="proj", branch_name="feat/x")
        assert run.wave_run_id is None


class TestDbWaveRunId:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    async def test_runs_table_has_wave_run_id_column(self) -> None:
        await db.init_db()
        async with aiosqlite.connect(db.db_path()) as conn:
            cursor = await conn.execute("PRAGMA table_info(runs)")
            cols = {row[1] for row in await cursor.fetchall()}
        assert "wave_run_id" in cols

    async def test_list_runs_by_wave_returns_correct_runs(self) -> None:
        await db.init_db()

        run1 = Run(
            item_id="i1", project_name="p", branch_name="b", wave_run_id="wr-abc"
        )
        run2 = Run(
            item_id="i2", project_name="p", branch_name="b", wave_run_id="wr-abc"
        )
        run3 = Run(
            item_id="i3", project_name="p", branch_name="b", wave_run_id="wr-xyz"
        )

        await db.insert_run(run1)
        await db.insert_run(run2)
        await db.insert_run(run3)

        results = await db.list_runs_by_wave("wr-abc")
        assert len(results) == 2
        result_ids = {r.id for r in results}
        assert run1.id in result_ids
        assert run2.id in result_ids
        assert run3.id not in result_ids

    async def test_list_runs_by_wave_returns_empty_for_unknown_wave(self) -> None:
        await db.init_db()
        results = await db.list_runs_by_wave("wr-nonexistent")
        assert results == []

    async def test_wave_run_id_persisted_and_retrieved(self) -> None:
        await db.init_db()
        run = Run(
            item_id="i1", project_name="p", branch_name="b", wave_run_id="wr-persist"
        )
        await db.insert_run(run)
        fetched = await db.get_run(run.id)
        assert fetched is not None
        assert fetched.wave_run_id == "wr-persist"

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


# ---------------------------------------------------------------------------
# AC-1.2 — Heartbeat prompt addendum
# ---------------------------------------------------------------------------


class TestHeartbeatPromptAddendum:
    def test_mentions_ping_wave_tool(self) -> None:
        text = _heartbeat_prompt_addendum()
        assert "mcp__agent-gtd__ping_wave" in text

    def test_mentions_90_second_cadence(self) -> None:
        text = _heartbeat_prompt_addendum()
        assert "90" in text

    def test_included_in_manage_system_prompt(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project={"name": "wave-project"},
            branch_name=None,
            max_turns=50,
            mode="manage",
            wave_run_id="wr-test",
        )
        assert "mcp__agent-gtd__ping_wave" in prompt
        assert "90" in prompt


# ---------------------------------------------------------------------------
# AC-2 — CI gate
# ---------------------------------------------------------------------------


class TestDetectProjectType:
    def test_python_when_pyproject_toml_exists(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]")
        assert _detect_project_type(tmp_path) == "python"

    def test_frontend_when_package_json_exists(self, tmp_path) -> None:
        (tmp_path / "package.json").write_text('{"name": "test"}')
        assert _detect_project_type(tmp_path) == "frontend"

    def test_unknown_when_no_known_files(self, tmp_path) -> None:
        assert _detect_project_type(tmp_path) == "unknown"

    def test_python_takes_priority_over_frontend(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]")
        (tmp_path / "package.json").write_text('{"name": "test"}')
        assert _detect_project_type(tmp_path) == "python"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestRunCIGate:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)

    async def test_python_all_steps_pass(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "python", 300
            )

        assert result.passed is True
        assert result.project_type == "python"
        assert result.failed_step is None
        assert result.returncode == 0

    async def test_python_fails_at_pytest(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git clone
                _completed(0),  # git checkout
                _completed(1, stderr="FAILED test_foo.py"),  # pytest fails
            ]
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "python", 300
            )

        assert result.passed is False
        assert result.failed_step == "uv run pytest"
        assert result.returncode == 1

    async def test_python_fails_at_ruff(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git clone
                _completed(0),  # git checkout
                _completed(0),  # pytest passes
                _completed(1, stderr="E501 line too long"),  # ruff fails
            ]
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "python", 300
            )

        assert result.passed is False
        assert result.failed_step == "uv run ruff check"

    async def test_python_fails_at_mypy(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git clone
                _completed(0),  # git checkout
                _completed(0),  # pytest passes
                _completed(0),  # ruff passes
                _completed(1, stderr="error: incompatible types"),  # mypy fails
            ]
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "python", 300
            )

        assert result.passed is False
        assert result.failed_step == "uv run mypy src/"

    async def test_frontend_all_steps_pass(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "frontend", 300
            )

        assert result.passed is True
        assert result.project_type == "frontend"

    async def test_frontend_fails_at_build(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git clone
                _completed(0),  # git checkout
                _completed(1, stderr="Build failed"),  # npm run build fails
            ]
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "frontend", 300
            )

        assert result.passed is False
        assert result.failed_step == "npm run build"

    async def test_unknown_type_passes_immediately(self) -> None:
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "unknown", 300
            )

        assert result.passed is True
        assert result.project_type == "unknown"

    async def test_auto_detect_delegates_to_detect_function(self) -> None:
        with (
            patch(
                "agent_gtd_dispatch.dispatch._detect_project_type",
                return_value="python",
            ),
            patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(0)
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", None, 300
            )

        assert result.project_type == "python"

    async def test_timeout_returns_failed_result(self) -> None:
        def raise_timeout(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if "uv" in cmd or "npm" in cmd:
                raise subprocess.TimeoutExpired(cmd, 300)
            return _completed(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=raise_timeout
        ):
            result = await run_ci_gate(
                "git@host:repos/test", "feat/abc", "python", 300
            )

        assert result.passed is False
        assert result.failed_step == "timeout"
        assert result.returncode is None

    async def test_ci_env_excludes_secrets(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        captured_envs: list[dict] = []

        def capture_env(*args, **kwargs):
            env = kwargs.get("env")
            if env is not None:
                captured_envs.append(dict(env))
            return _completed(0)

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.run", side_effect=capture_env
        ):
            await run_ci_gate("git@host:repos/test", "feat/abc", "python", 300)

        # CI steps (not git steps) should not have ANTHROPIC_API_KEY
        # git clone/checkout calls don't pass env= explicitly, so captured_envs
        # contains only CI step envs
        for env in captured_envs:
            assert "ANTHROPIC_API_KEY" not in env


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

    def test_manage_mode_passed_to_run_agent_env(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "TIMEOUT_SECONDS", 60)
        monkeypatch.setenv("DISPATCH_LOCAL_URL", "http://localhost:8080")
        monkeypatch.setenv("DISPATCH_API_KEY", "mgr-key")

        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)

            async def _run() -> None:
                await run_agent(CLAUDE, tmp_path, "sys", "Title", 20, mode="manage")

            import asyncio

            asyncio.get_event_loop().run_until_complete(_run())
            _, kwargs = mock_run.call_args
            assert kwargs["env"].get("DISPATCH_LOCAL_URL") == "http://localhost:8080"


class TestCIGatePromptAddendum:
    def test_mentions_ci_gate_endpoint(self) -> None:
        text = _ci_gate_prompt_addendum()
        assert "/ci-gate" in text

    def test_mentions_halt_on_failure(self) -> None:
        text = _ci_gate_prompt_addendum()
        assert "halt_wave" in text

    def test_included_in_manage_system_prompt(self) -> None:
        prompt = build_system_prompt(
            item={"id": "item-1", "title": "ignored"},
            project={"name": "wave-project"},
            branch_name=None,
            max_turns=50,
            mode="manage",
            wave_run_id="wr-test",
        )
        assert "/ci-gate" in prompt
