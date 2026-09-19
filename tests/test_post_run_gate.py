"""Tests for the post-run quality gate (dispatch.run_gate_command + worker wiring)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, db, dispatch
from agent_gtd_dispatch.dispatch import GateResult
from agent_gtd_dispatch.engines import CLAUDE
from agent_gtd_dispatch.models import (
    DispatchMode,
    PushStatus,
    RepoPushStatus,
    Run,
)
from tests.completion_fixtures import (
    MAX_TURNS_ENVELOPE,
    seed_build_evidence,
    write_artifact,
    write_envelope,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Set required env vars and use tmp path for workspace/db."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


# ---------------------------------------------------------------------------
# _classify_gate_timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "duration_seconds", "timeout_seconds", "expected"),
    [
        (124, 600.2, 600, True),
        (124, 599.5, 600, True),
        (124, 12.0, 600, False),
        (137, 601.0, 600, True),
        (137, 3.0, 600, False),
        (-9, 605.0, 600, True),
        (-9, 0.1, 60, False),
        (0, 700.0, 600, False),
        (1, 5.0, 600, False),
    ],
)
def test_classify_gate_timeout(
    returncode, duration_seconds, timeout_seconds, expected
) -> None:
    assert (
        dispatch._classify_gate_timeout(returncode, duration_seconds, timeout_seconds)
        is expected
    )


# ---------------------------------------------------------------------------
# run_gate_command — mocked subprocess
# ---------------------------------------------------------------------------


def _popen_ok(write_bytes: bytes = b"ok", rc: int = 0):
    def _effect(*args, **kwargs):
        kwargs["stdout"].write(write_bytes)
        proc = MagicMock()
        proc.returncode = rc
        proc.wait = MagicMock(return_value=rc)
        return proc

    return _effect


class TestRunGateCommandMocked:
    def test_argv_no_sudo_when_subprocess_user_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        argv = mock_popen.call_args.args[0]
        assert argv[0] == "/bin/bash"

    def test_argv_sudo_wrap_when_subprocess_user_set(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo done-cmd", 30, CLAUDE)
        argv = mock_popen.call_args.args[0]
        assert argv[:5] == ["sudo", "-u", "dispatch", "-H", "/bin/bash"]
        assert argv[-1] == "echo done-cmd"

    def test_stdin_is_devnull(self, tmp_path) -> None:
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        assert mock_popen.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_env_excludes_secrets_keeps_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-anthropic")
        monkeypatch.setenv("AGENT_GTD_URL", "http://x")
        monkeypatch.setenv("AGENT_GTD_API_KEY", "secret-gtd")
        monkeypatch.setenv("DISPATCH_API_KEY", "secret-dispatch")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-oauth")
        monkeypatch.setenv("KB_DATABASE_URL", "postgresql:///x")
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        env = mock_popen.call_args.kwargs["env"]
        for key in (
            "ANTHROPIC_API_KEY",
            "AGENT_GTD_URL",
            "AGENT_GTD_API_KEY",
            "DISPATCH_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "KB_DATABASE_URL",
        ):
            assert key not in env
        assert "PATH" in env

    def test_env_includes_kb_test_database_url_and_require_flag(
        self, tmp_path, monkeypatch
    ) -> None:
        """KB_TEST_DATABASE_URL/KB_REQUIRE_POSTGRES_TESTS are not secrets (a local
        peer-auth maintenance DSN and a boolean flag) — unlike KB_DATABASE_URL, they
        must reach the gate subprocess so @pytest.mark.postgres suites (e.g. kb-core)
        run instead of silently skipping."""
        monkeypatch.setenv("KB_TEST_DATABASE_URL", "postgresql:///postgres")
        monkeypatch.setenv("KB_REQUIRE_POSTGRES_TESTS", "1")
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        env = mock_popen.call_args.kwargs["env"]
        assert env["KB_TEST_DATABASE_URL"] == "postgresql:///postgres"
        assert env["KB_REQUIRE_POSTGRES_TESTS"] == "1"

    def test_env_path_matches_build_env_path(self, tmp_path) -> None:
        """The gate's PATH is exactly build_env()'s PATH (picks up .cargo/bin etc)."""
        from agent_gtd_dispatch.engines import build_env
        from agent_gtd_dispatch.models import DispatchMode

        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ) as mock_popen:
            dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        env = mock_popen.call_args.kwargs["env"]
        assert env["PATH"] == build_env(CLAUDE, mode=DispatchMode.BUILD)["PATH"]

    def test_output_tail_truncated_to_3000_chars(self, tmp_path) -> None:
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen",
            side_effect=_popen_ok(b"x" * 5000 + b"END"),
        ):
            result = dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        assert len(result.output) == 3000
        assert result.output.endswith("END")

    def test_outer_timeout_kills_and_returns_partial_output(self, tmp_path) -> None:
        created = {}

        def _effect(*args, **kwargs):
            kwargs["stdout"].write(b"partial")
            proc = MagicMock()
            proc.wait = MagicMock(
                side_effect=[subprocess.TimeoutExpired(cmd="x", timeout=1), 0]
            )
            created["proc"] = proc
            return proc

        with patch("agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_effect):
            result = dispatch.run_gate_command(tmp_path, "sleep 999", 1, CLAUDE)

        assert result.returncode is None
        assert result.timed_out is True
        assert result.output == "partial"
        created["proc"].kill.assert_called_once()

    def test_popen_oserror_gives_launch_error(self, tmp_path) -> None:
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen",
            side_effect=FileNotFoundError("sudo"),
        ):
            result = dispatch.run_gate_command(tmp_path, "echo hi", 30, CLAUDE)
        assert result.returncode is None
        assert result.timed_out is False
        assert result.output == "gate launch failed: sudo"

    def test_popen_callback_invoked_once_with_proc(self, tmp_path) -> None:
        callback = MagicMock()
        with patch(
            "agent_gtd_dispatch.dispatch.subprocess.Popen", side_effect=_popen_ok()
        ):
            dispatch.run_gate_command(
                tmp_path, "echo hi", 30, CLAUDE, popen_callback=callback
            )
        callback.assert_called_once()

    def test_stash_failure_short_circuits_before_popen(self, tmp_path) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"boom"
        )
        with (
            patch("agent_gtd_dispatch.dispatch.subprocess.run", return_value=completed),
            patch("agent_gtd_dispatch.dispatch.subprocess.Popen") as mock_popen,
        ):
            result = dispatch.run_gate_command(
                tmp_path, "echo hi", 30, CLAUDE, None, [tmp_path]
            )
        assert result.output.startswith(
            "gate launch failed: could not stash uncommitted changes in"
        )
        mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# run_gate_command — real shell (skipped when timeout(1)/git(1) unavailable)
# ---------------------------------------------------------------------------

_REAL_SHELL_UNAVAILABLE = shutil.which("timeout") is None or shutil.which("git") is None


@pytest.mark.skipif(_REAL_SHELL_UNAVAILABLE, reason="requires timeout(1) and git(1)")
class TestRunGateCommandRealShell:
    @pytest.fixture(autouse=True)
    def _no_sudo(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")

    def test_exit_code_and_output_propagate(self, tmp_path) -> None:
        result = dispatch.run_gate_command(tmp_path, "echo hi; exit 3", 30, CLAUDE)
        assert result.returncode == 3
        assert result.timed_out is False
        assert "hi" in result.output

    def test_timeout_kills_sleeper(self, tmp_path) -> None:
        start = time.monotonic()
        result = dispatch.run_gate_command(tmp_path, "sleep 30", 1, CLAUDE)
        assert result.timed_out is True
        assert time.monotonic() - start < 15

    def test_sigterm_ignored_escalates_to_sigkill(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "CANCEL_GRACE_SECONDS", 1)
        start = time.monotonic()
        result = dispatch.run_gate_command(
            tmp_path, 'trap "" TERM; sleep 30', 1, CLAUDE
        )
        assert result.timed_out is True
        assert time.monotonic() - start < 15

    def test_backgrounded_grandchild_does_not_block_wait(self, tmp_path) -> None:
        result = dispatch.run_gate_command(
            tmp_path, "(sleep 8 &); echo done; exit 0", 30, CLAUDE
        )
        assert result.returncode == 0
        assert "done" in result.output
        assert result.duration_seconds < 5

    def test_dirty_tree_stashed_before_gate_runs(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        (repo / "a.txt").write_text("one\n")
        subprocess.run(
            ["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-m",
                "init",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (repo / "a.txt").write_text("two\n")

        result = dispatch.run_gate_command(
            repo, "git diff --quiet", 30, CLAUDE, None, [repo]
        )
        assert result.returncode == 0

        stash_list = subprocess.run(
            ["git", "stash", "list"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "agent-gtd-post-run-gate" in stash_list.stdout


# ---------------------------------------------------------------------------
# config: POST_RUN_GATE_MIN_SECONDS
# ---------------------------------------------------------------------------


class TestPostRunGateMinSecondsConfig:
    def _env(self, tmp_path, **extra):
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        env.update(extra)
        return env

    def test_default(self, tmp_path) -> None:
        with patch.dict(os.environ, self._env(tmp_path), clear=True):
            config.load()
            assert config.POST_RUN_GATE_MIN_SECONDS == 600

    def test_override(self, tmp_path) -> None:
        env = self._env(tmp_path, DISPATCH_POST_RUN_GATE_MIN_SECONDS="45")
        with patch.dict(os.environ, env, clear=True):
            config.load()
            assert config.POST_RUN_GATE_MIN_SECONDS == 45


# ---------------------------------------------------------------------------
# Build-mode prompt: '## Quality Gate' section
# ---------------------------------------------------------------------------


class TestBuildGateSectionPrompt:
    def test_gate_section_contents(self) -> None:
        item = {"id": "item1"}
        project = {"name": "P", "gate_command": "make gate"}
        prompt = dispatch._build_build_prompt(
            item, project, "feat/x", 50, workspace=Path("/ws")
        )
        assert "## Quality Gate" in prompt
        assert "make gate" in prompt
        assert "the dispatch worker re-runs this exact command" in prompt

    def test_workspace_mode_says_workspace_root(self) -> None:
        item = {"id": "item1"}
        project = {"name": "P", "gate_command": "make gate"}
        prompt = dispatch._build_build_prompt(
            item,
            project,
            "feat/x",
            50,
            workspace=Path("/ws"),
            workspace_repo_dirs=["a", "b"],
        )
        assert "from the workspace root" in prompt

    @pytest.mark.parametrize("gate_command", [None, "", "   "])
    def test_empty_gate_command_byte_identical(self, gate_command) -> None:
        item = {"id": "item1"}
        project_with = {"name": "P", "gate_command": gate_command}
        project_without = {"name": "P"}
        prompt_with = dispatch._build_build_prompt(
            item, project_with, "feat/x", 50, workspace=Path("/ws")
        )
        prompt_without = dispatch._build_build_prompt(
            item, project_without, "feat/x", 50, workspace=Path("/ws")
        )
        assert prompt_with == prompt_without
        assert "## Quality Gate" not in prompt_with


# ---------------------------------------------------------------------------
# Manage-mode prompt: post-run-gate exception paragraph
# ---------------------------------------------------------------------------


class TestManagePromptGateException:
    def _build(self, project, workspace_repo_dirs=None):
        return dispatch.build_system_prompt(
            {},
            project,
            None,
            50,
            mode=DispatchMode.MANAGE,
            rollout_id="wr-1",
            manage_retry_count=0,
            workspace_repo_dirs=workspace_repo_dirs,
        )

    def test_gate_command_present(self) -> None:
        base = {"name": "P", "id": "p1", "gate_command": "make gate"}
        workspace_prompt = self._build(base, ["a", "b"])
        monorepo_prompt = self._build({**base, "git_origin": "git@host:x/y"})

        for prompt in (workspace_prompt, monorepo_prompt):
            assert "starts with `post-run gate`" in prompt
            assert "ALSO run the project gate command" in prompt
            assert "`make gate`" in prompt

        assert "In Step 5b," in workspace_prompt
        assert "In Step 5," in monorepo_prompt
        assert "Step 5b" not in monorepo_prompt

    @pytest.mark.parametrize("gate_command", [None, ""])
    def test_gate_command_absent_byte_identical(self, gate_command) -> None:
        project_with = {
            "name": "P",
            "id": "p1",
            "gate_command": gate_command,
            "git_origin": "git@host:x/y",
        }
        project_without = {"name": "P", "id": "p1", "git_origin": "git@host:x/y"}

        prompt_with = self._build(project_with)
        prompt_without = self._build(project_without)
        assert prompt_with == prompt_without
        assert "post-run gate" not in prompt_with

        workspace_with = self._build(project_with, ["a", "b"])
        workspace_without = self._build(project_without, ["a", "b"])
        assert workspace_with == workspace_without
        assert "post-run gate" not in workspace_with

    def test_multiline_gate_command_dedent_intact(self) -> None:
        project = {
            "name": "P",
            "id": "p1",
            "gate_command": "make a\nmake b",
            "git_origin": "git@host:x/y",
        }
        prompt = self._build(project)
        assert prompt.startswith("You are a headless rollout-manager executor")
        assert "make a\nmake b" in prompt


# ---------------------------------------------------------------------------
# Worker wiring: _dispatch_worker post-run gate integration
# ---------------------------------------------------------------------------


def _completed(rc: int = 0):
    m = MagicMock()
    m.returncode = rc
    return m


def _pushed(repo_name="repos-testproj", branch="feat/x", dirty=False):
    return RepoPushStatus(
        repo_name=repo_name,
        branch=branch,
        status=PushStatus.pushed,
        local_sha="aaa1111",
        remote_sha="aaa1111",
        commits_ahead=1,
        dirty=dirty,
    )


def _no_changes(repo_name="repos-testproj", branch="feat/x"):
    return RepoPushStatus(
        repo_name=repo_name,
        branch=branch,
        status=PushStatus.no_changes,
        local_sha="aaa1111",
        remote_sha="aaa1111",
        commits_ahead=0,
        dirty=False,
    )


def _install_common_mocks(
    mock_gtd,
    mock_dispatch,
    *,
    item_id,
    project,
    fake_workspace,
    disposition="done",
):
    seed_build_evidence(fake_workspace, disposition)
    mock_dispatch.is_zero_commits_run = dispatch.is_zero_commits_run
    mock_gtd.get_item = AsyncMock(
        return_value={"id": item_id, "title": "T", "project_id": "proj1"}
    )
    mock_gtd.get_project = AsyncMock(return_value=project)
    mock_gtd.post_comment = AsyncMock()
    mock_gtd.list_attachments = AsyncMock(return_value=[])
    mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
    mock_dispatch.get_head_sha = MagicMock(return_value="baseshaabc")
    mock_dispatch.repo_name_from_origin = MagicMock(return_value="repos-testproj")
    mock_dispatch.stage_attachments = AsyncMock(return_value=[])
    mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
    mock_dispatch.cleanup_workspace = MagicMock()
    mock_dispatch._executor = None


def _default_project(**extra):
    project = {
        "id": "proj1",
        "name": "TestProject",
        "git_origin": "git@host:repos/testproj",
        "gate_command": "make gate",
    }
    project.update(extra)
    return project


class TestWorkerPostRunGate:
    @pytest.mark.asyncio
    async def test_pass_marks_succeeded_and_posts_comment(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-a",
            project_name="TestProject",
            branch_name="feat/a",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=0, timed_out=False, output="all good", duration_seconds=12.3
        )
        fake_workspace = tmp_path / "repos-testproj-a"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-a",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/a")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"

        mock_dispatch.run_gate_command.assert_called_once()
        call = mock_dispatch.run_gate_command.call_args
        assert call.args[0] == fake_workspace
        assert call.args[1] == "make gate"
        assert isinstance(call.args[2], int)
        assert call.args[3] is CLAUDE
        assert callable(call.args[4])
        assert call.args[5] is None

        comment_texts = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("Post-run gate passed" in t for t in comment_texts)
        assert "decision=passed" in caplog.text

    @pytest.mark.asyncio
    async def test_gate_exit_failure_marks_run_failed(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-b",
            project_name="TestProject",
            branch_name="feat/b",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=1, timed_out=False, output="...boom...", duration_seconds=5.0
        )
        fake_workspace = tmp_path / "repos-testproj-b"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-b",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/b")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error == "post-run gate failed: exit 1"
        assert updated.exit_code == 0
        assert updated.push_results is not None

        comment_texts = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        gate_comments = [t for t in comment_texts if "Post-run gate failed" in t]
        assert len(gate_comments) == 1
        assert "boom" in gate_comments[0]
        assert "````" in gate_comments[0]

        mock_dispatch.cleanup_workspace.assert_called_once_with(fake_workspace)
        assert any(
            r.levelname == "WARNING" and "boom" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_gate_failure_comment_post_error_does_not_change_outcome(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-b2",
            project_name="TestProject",
            branch_name="feat/b2",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=1, timed_out=False, output="...boom...", duration_seconds=5.0
        )
        fake_workspace = tmp_path / "repos-testproj-b2"
        fake_workspace.mkdir()

        async def _raise_for_gate_comment(item_id, content, **kwargs):
            if "Post-run gate" in content:
                raise RuntimeError("comment post failed")
            return None

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-b2",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.post_comment = AsyncMock(side_effect=_raise_for_gate_comment)
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/b2")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert any(
            r.levelname == "WARNING" and "boom" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_timeout_error_string(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-c",
            project_name="TestProject",
            branch_name="feat/c",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=None, timed_out=True, output="stuck", duration_seconds=601.0
        )
        fake_workspace = tmp_path / "repos-testproj-c"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-c",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/c")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        n = mock_dispatch.run_gate_command.call_args.args[2]
        assert updated.error == f"post-run gate timed out after {n}s"

    @pytest.mark.asyncio
    async def test_killed_by_signal_error_string(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-c2",
            project_name="TestProject",
            branch_name="feat/c2",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=-9, timed_out=False, output="killed", duration_seconds=5.0
        )
        fake_workspace = tmp_path / "repos-testproj-c2"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-c2",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/c2")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.error == "post-run gate failed: killed by signal 9"

    @pytest.mark.asyncio
    async def test_launch_error_error_string(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-c3",
            project_name="TestProject",
            branch_name="feat/c3",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=None, timed_out=False, output="oops", duration_seconds=0.1
        )
        fake_workspace = tmp_path / "repos-testproj-c3"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-c3",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/c3")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.error == "post-run gate failed: launch error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("gate_command", [None, "", "   "])
    async def test_skip_no_gate_command(
        self, tmp_path, caplog: pytest.LogCaptureFixture, gate_command
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-d",
            project_name="TestProject",
            branch_name="feat/d",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-d"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-d",
                project=_default_project(gate_command=gate_command),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/d")]
            )
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        mock_dispatch.run_gate_command.assert_not_called()
        comment_texts = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert not any("Post-run gate" in t for t in comment_texts)
        assert "decision=skipped_no_gate_command" in caplog.text

    @pytest.mark.asyncio
    async def test_still_unpushed_after_backstop_skips_gate(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-e",
            project_name="TestProject",
            branch_name="feat/e",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        unpushed_result = RepoPushStatus(
            repo_name="repos-testproj",
            branch="feat/e",
            status=PushStatus.unpushed,
            local_sha=None,
            remote_sha=None,
            commits_ahead=0,
            dirty=False,
        )
        fake_workspace = tmp_path / "repos-testproj-e"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-e",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(return_value=[unpushed_result])
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("push verification failed")
        mock_dispatch.run_gate_command.assert_not_called()

    async def _run_already_satisfied(
        self,
        tmp_path,
        caplog,
        *,
        gate_result,
        project,
        item_id,
    ):
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id=item_id,
            project_name="TestProject",
            branch_name="feat/f",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / f"repos-testproj-{item_id}"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id=item_id,
                project=project,
                fake_workspace=fake_workspace,
            )
            write_artifact(
                fake_workspace,
                "already_satisfied",
                reason="ALREADY THERE at foo.py:12",
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_gtd.complete_item = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_no_changes(branch="feat/f")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        return updated, mock_gtd, mock_dispatch

    @pytest.mark.asyncio
    async def test_already_satisfied_runs_gate_and_lands_already_satisfied(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate_result = GateResult(
            returncode=0, timed_out=False, output="ok", duration_seconds=2.0
        )
        updated, mock_gtd, mock_dispatch = await self._run_already_satisfied(
            tmp_path,
            caplog,
            gate_result=gate_result,
            project=_default_project(),
            item_id="item-f",
        )
        # The gate RUNS even though zero repos were pushed.
        mock_dispatch.run_gate_command.assert_called_once()
        assert "decision=skipped_no_pushed_repo" not in caplog.text
        assert updated.status.value == "already_satisfied"
        assert updated.error is not None
        assert updated.error.startswith("already_satisfied: ")
        assert "ALREADY THERE at foo.py:12" in updated.error
        assert updated.push_results is not None
        assert "outcome=already_satisfied" in caplog.text
        assert "gate_decision=passed" in caplog.text
        mock_gtd.set_item_status.assert_awaited_once()
        assert mock_gtd.set_item_status.await_args.args[:2] == ("item-f", "review")
        mock_gtd.complete_item.assert_not_called()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("already satisfied" in b for b in bodies)
        assert any("ALREADY THERE at foo.py:12" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_already_satisfied_red_gate_fails_run(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate_result = GateResult(
            returncode=1, timed_out=False, output="boom", duration_seconds=2.0
        )
        updated, mock_gtd, _ = await self._run_already_satisfied(
            tmp_path,
            caplog,
            gate_result=gate_result,
            project=_default_project(),
            item_id="item-g",
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("already_satisfied_gate_failed: ")
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any(
            "Build run failed (already_satisfied_gate_failed)" in b for b in bodies
        )

    @pytest.mark.asyncio
    async def test_already_satisfied_gate_timeout_fails_run(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate_result = GateResult(
            returncode=124, timed_out=True, output="slow", duration_seconds=600.0
        )
        updated, _mock_gtd, _ = await self._run_already_satisfied(
            tmp_path,
            caplog,
            gate_result=gate_result,
            project=_default_project(),
            item_id="item-h",
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("already_satisfied_gate_failed: ")
        assert "timed out" in updated.error

    @pytest.mark.asyncio
    async def test_already_satisfied_without_gate_command_still_terminal(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, mock_dispatch = await self._run_already_satisfied(
            tmp_path,
            caplog,
            gate_result=None,
            project=_default_project(gate_command=""),
            item_id="item-i",
        )
        mock_dispatch.run_gate_command.assert_not_called()
        assert updated.status.value == "already_satisfied"
        assert "gate_decision=skipped_no_gate_command" in caplog.text
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("skipped_no_gate_command" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_plan_mode_skips_gate(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-g",
            project_name="TestProject",
            branch_name=None,
            mode=DispatchMode.PLAN,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-g"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-g",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(return_value=[])
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        mock_dispatch.run_gate_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_talos_engine_skips_gate(self, tmp_path) -> None:
        from agent_gtd_dispatch.engines import TALOS_SONNET
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-h",
            project_name="TestProject",
            branch_name="feat/h",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-h"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch(
                "agent_gtd_dispatch.main._run_talos", new_callable=AsyncMock
            ) as mock_run_talos,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-h",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, TALOS_SONNET, 600)

        mock_run_talos.assert_called_once()
        mock_dispatch.run_gate_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_floor_applied(
        self, tmp_path, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        monkeypatch.setattr(config, "POST_RUN_GATE_MIN_SECONDS", 30)
        gate_result = GateResult(
            returncode=0, timed_out=False, output="ok", duration_seconds=1.0
        )

        await db.init_db()

        # Case 1: timeout_seconds=5 -> floor applied, args[2] == 30
        run1 = Run(
            item_id="item-i1",
            project_name="TestProject",
            branch_name="feat/i1",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run1)
        fake_workspace1 = tmp_path / "repos-testproj-i1"
        fake_workspace1.mkdir()
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-i1",
                project=_default_project(),
                fake_workspace=fake_workspace1,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/i1")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run1, 50, CLAUDE, 5)

        assert mock_dispatch.run_gate_command.call_args.args[2] == 30
        assert "floor_applied=True" in caplog.text

        # Case 2: timeout_seconds=600 -> no floor, args[2] roughly 600
        caplog.clear()
        run2 = Run(
            item_id="item-i2",
            project_name="TestProject",
            branch_name="feat/i2",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run2)
        fake_workspace2 = tmp_path / "repos-testproj-i2"
        fake_workspace2.mkdir()
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-i2",
                project=_default_project(),
                fake_workspace=fake_workspace2,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/i2")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run2, 50, CLAUDE, 600)

        assert mock_dispatch.run_gate_command.call_args.args[2] in range(590, 601)

    @pytest.mark.asyncio
    async def test_dirty_repo_stashed_and_noted_in_pass_comment(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-j",
            project_name="TestProject",
            branch_name="feat/j",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        gate_result = GateResult(
            returncode=0, timed_out=False, output="ok", duration_seconds=1.0
        )
        fake_workspace = tmp_path / "repos-testproj-j"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-j",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/j", dirty=True)]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        call = mock_dispatch.run_gate_command.call_args
        assert call.args[5] == [fake_workspace]

        comment_texts = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        pass_comments = [t for t in comment_texts if "Post-run gate passed" in t]
        assert len(pass_comments) == 1
        assert "Uncommitted changes in repos-testproj" in pass_comments[0]

    @pytest.mark.asyncio
    async def test_timeout_expired_linger_success_skips_gate(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-k",
            project_name="TestProject",
            branch_name="feat/k",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-k"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-k",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=600)
            )
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/k")]
            )
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        mock_dispatch.run_gate_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_project_gate_cwd_is_ws_root(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-l",
            project_name="TestProject",
            branch_name="feat/l",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_ws_root = tmp_path / "repos-l"
        fake_ws_root.mkdir()
        (fake_ws_root / "repo_a").mkdir()
        (fake_ws_root / "repo_b").mkdir()

        gate_result = GateResult(
            returncode=0, timed_out=False, output="ok", duration_seconds=1.0
        )

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={"id": "item-l", "title": "T", "project_id": "proj1"}
            )
            mock_gtd.get_project = AsyncMock(
                return_value=_default_project(
                    repo_mode="workspace",
                    workspace_repos=["git@host:org/repo_a", "git@host:org/repo_b"],
                )
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            mock_dispatch.prepare_workspace_multi = MagicMock(return_value=fake_ws_root)
            mock_dispatch.repo_dir_from_url = MagicMock(
                side_effect=lambda u: u.rsplit("/", 1)[-1]
            )
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaabc")
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.cleanup_workspace = MagicMock()
            mock_dispatch._executor = None
            mock_dispatch.is_zero_commits_run = dispatch.is_zero_commits_run
            seed_build_evidence(fake_ws_root)
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(repo_name="repo_a", branch="feat/l")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        call = mock_dispatch.run_gate_command.call_args
        assert call.args[0] == fake_ws_root


# ---------------------------------------------------------------------------
# The unasserted path: absent/unusable completion artifact + pushed commits
# ---------------------------------------------------------------------------


def _drop_artifact(workspace: Path) -> None:
    """Remove the completion artifact a seeded workspace wrote (reason=absent)."""
    (workspace / ".dispatch" / "completion.json").unlink()


def _malform_artifact(workspace: Path) -> None:
    """Leave an artifact that read_completion_artifact rejects (reason=not_json)."""
    (workspace / ".dispatch" / "completion.json").write_text("{ not json at all")


async def _run_build_worker(
    tmp_path,
    caplog,
    *,
    item_id,
    artifact="absent",
    pushed=True,
    gate_result=None,
    project=None,
    envelope=None,
    item_status=None,
    rollout_id=None,
):
    from agent_gtd_dispatch.main import _dispatch_worker

    await db.init_db()
    run = Run(
        item_id=item_id,
        project_name="TestProject",
        branch_name="feat/unasserted",
        mode=DispatchMode.BUILD,
        rollout_id=rollout_id,
    )
    await db.insert_run(run)

    fake_workspace = tmp_path / f"repos-testproj-{item_id}"
    fake_workspace.mkdir()

    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
    ):
        _install_common_mocks(
            mock_gtd,
            mock_dispatch,
            item_id=item_id,
            project=project if project is not None else _default_project(),
            fake_workspace=fake_workspace,
            disposition="done" if artifact in {"absent", "malformed"} else artifact,
        )
        if artifact == "absent":
            _drop_artifact(fake_workspace)
        elif artifact == "malformed":
            _malform_artifact(fake_workspace)
        elif artifact == "already_satisfied":
            write_artifact(
                fake_workspace,
                "already_satisfied",
                reason="ALREADY THERE at foo.py:12",
            )
        elif artifact == "blocked":
            write_artifact(fake_workspace, "blocked", decision_needed="which schema?")
        if envelope is not None:
            write_envelope(fake_workspace, **envelope)
        _item: dict[str, object] = {
            "id": item_id,
            "title": "T",
            "project_id": "proj1",
        }
        if item_status is not None:
            _item["status"] = item_status
        mock_gtd.get_item = AsyncMock(return_value=_item)
        mock_gtd.set_item_status = AsyncMock()
        mock_gtd.complete_item = AsyncMock()
        mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
        mock_dispatch.verify_pushes = MagicMock(
            return_value=[
                (_pushed if pushed else _no_changes)(branch="feat/unasserted")
            ]
        )
        mock_dispatch.run_gate_command = MagicMock(return_value=gate_result)

        await _dispatch_worker(run, 50, CLAUDE, 600)

    updated = await db.get_run(run.id)
    assert updated is not None
    return updated, mock_gtd, mock_dispatch


class TestUnassertedCompletionPath:
    """A pushed, gate-green build run is a success even with no artifact.

    Writing `.dispatch/completion.json` is a cooperative act; some engines skip
    it intermittently. When the mechanical legs (commits pushed + project gate
    green) are satisfied, failing the run is a false negative that halts healthy
    rollout waves — so the gate decides instead.
    """

    async def _run(self, tmp_path, caplog, **kwargs):
        return await _run_build_worker(tmp_path, caplog, **kwargs)

    # --- the new semantics ------------------------------------------------

    @pytest.mark.asyncio
    async def test_absent_artifact_pushed_gate_passed_is_success(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua1",
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "succeeded"
        assert updated.error is None
        # The gate RAN — it is what decides this terminal.
        mock_dispatch.run_gate_command.assert_called_once()
        # Recorded for later per-engine aggregation.
        assert updated.completion is not None
        blob = json.loads(updated.completion)
        assert blob["unasserted"] is True
        assert blob["gate_decision"] == "passed"
        assert blob["artifact"] == "absent"
        # Item nudged to review, because an agent that skipped the artifact
        # write plausibly skipped its own status update too.
        mock_gtd.set_item_status.assert_awaited_once()
        assert mock_gtd.set_item_status.await_args.args[:2] == ("item-ua1", "review")
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        unasserted = [b for b in bodies if "mechanical evidence alone" in b]
        assert len(unasserted) == 1
        assert "feat/unasserted" in unasserted[0]
        assert "decision=`passed`" in unasserted[0]
        assert any(
            r.levelname == "WARNING" and "unasserted build run:" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_warning_log_carries_engine_and_counts(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._run(
            tmp_path,
            caplog,
            item_id="item-ua1b",
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        line = next(
            r.getMessage()
            for r in caplog.records
            if "unasserted build run:" in r.getMessage()
        )
        for fragment in (
            "engine=claude-code",
            "artifact_reject_reason=absent",
            "pushed_repos=1",
            "gate_decision=passed",
            "outcome=succeeded",
        ):
            assert fragment in line

    @pytest.mark.asyncio
    async def test_absent_artifact_pushed_gate_failed_is_failure(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua2",
            gate_result=GateResult(
                returncode=1, timed_out=False, output="boom", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("stopped_without_assertion: ")
        assert "decision=failed" in updated.error
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is True
        mock_gtd.set_item_status.assert_not_awaited()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("stopped_without_assertion" in b for b in bodies)
        assert any("boom" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_absent_artifact_pushed_no_gate_command_is_failure(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua3",
            project=_default_project(gate_command=""),
        )
        mock_dispatch.run_gate_command.assert_not_called()
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("stopped_without_assertion: ")
        assert "decision=skipped_no_gate_command" in updated.error
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        # The operator is told what would make such a run pass.
        assert any("gate_command" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_absent_artifact_zero_commits_unchanged(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The case the contract exists for — must not be weakened."""
        updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua4",
            pushed=False,
        )
        assert updated.status.value == "failed"
        assert updated.error == (
            "stopped_without_assertion: build run did not assert a usable completion"
        )
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is False
        mock_dispatch.run_gate_command.assert_not_called()
        mock_gtd.set_item_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_artifact_pushed_gate_passed_surfaces_reason(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unparseable == absent, but the reject reason still reaches the operator."""
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua5",
            artifact="malformed",
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "succeeded"
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is True
        assert blob["artifact_reject_reason"] == "not_json"
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("Completion artifact rejected: not_json." in b for b in bodies)

    @pytest.mark.asyncio
    async def test_malformed_artifact_gate_failed_surfaces_reason(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua5b",
            artifact="malformed",
            gate_result=GateResult(
                returncode=1, timed_out=False, output="boom", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert "Completion artifact rejected: not_json." in updated.error
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("Completion artifact rejected: not_json." in b for b in bodies)

    @pytest.mark.asyncio
    async def test_bad_envelope_keeps_strict_precedence(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The CLI envelope outranks the new leniency, even with commits + green gate."""
        updated, _mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua6",
            envelope=MAX_TURNS_ENVELOPE,
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("max_turns_exhausted: ")
        mock_dispatch.run_gate_command.assert_not_called()

    # --- guards -----------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_status", ["review", "done"])
    async def test_item_already_reviewed_or_done_is_not_patched(
        self, tmp_path, caplog: pytest.LogCaptureFixture, current_status
    ) -> None:
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id=f"item-ua7-{current_status}",
            item_status=current_status,
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "succeeded"
        mock_gtd.set_item_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_comment_failure_does_not_flip_the_terminal(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-ua8",
            project_name="TestProject",
            branch_name="feat/unasserted",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-ua8"
        fake_workspace.mkdir()

        async def _boom(item_id, content, **kwargs):
            if "mechanical evidence alone" in content:
                raise RuntimeError("comment post failed")
            return None

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-ua8",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            _drop_artifact(fake_workspace)
            mock_gtd.post_comment = AsyncMock(side_effect=_boom)
            mock_gtd.set_item_status = AsyncMock(side_effect=RuntimeError("nope"))
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/unasserted")]
            )
            mock_dispatch.run_gate_command = MagicMock(
                return_value=GateResult(
                    returncode=0, timed_out=False, output="ok", duration_seconds=1.0
                )
            )

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"

    @pytest.mark.asyncio
    async def test_rollout_child_reports_success(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rollouts need no change: the manage-prompt halt rule never sees this run.

        The zero-commit halt rule keys on a FAILED child run; an unasserted child
        that pushed work and passed the gate reports `succeeded`, so the wave
        proceeds exactly as it would for an artifact-present run.
        """
        updated, _mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua9",
            rollout_id="rollout-1",
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.rollout_id == "rollout-1"
        assert updated.status.value == "succeeded"
        assert updated.push_results is not None
        assert not dispatch.is_zero_commits_run(updated.push_results)

    # --- no-regression: every artifact-PRESENT outcome is untouched -------

    @pytest.mark.asyncio
    async def test_present_done_artifact_unchanged(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua10",
            artifact="done",
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "succeeded"
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is False
        assert blob["disposition"] == "done"
        # `done` -> `review`, materialized by the worker (the agent's prompt no
        # longer asks it to set the status).
        mock_gtd.set_item_status.assert_awaited_once()
        assert mock_gtd.set_item_status.await_args.args[:2] == ("item-ua10", "review")
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert not any("mechanical evidence alone" in b for b in bodies)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disposition", ["blocked", "failed"])
    async def test_present_blocked_or_failed_artifact_unchanged(
        self, tmp_path, caplog: pytest.LogCaptureFixture, disposition
    ) -> None:
        updated, _mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            caplog,
            item_id=f"item-ua11-{disposition}",
            artifact=disposition,
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith(f"agent_reported_{disposition}: ")
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is False
        mock_dispatch.run_gate_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_present_done_artifact_zero_commits_unchanged(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, _mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua12",
            artifact="done",
            pushed=False,
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("done_claim_zero_commits: ")

    @pytest.mark.asyncio
    async def test_present_already_satisfied_artifact_unchanged(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, _ = await self._run(
            tmp_path,
            caplog,
            item_id="item-ua13",
            artifact="already_satisfied",
            pushed=False,
            gate_result=GateResult(
                returncode=0, timed_out=False, output="ok", duration_seconds=3.0
            ),
        )
        assert updated.status.value == "already_satisfied"
        blob = json.loads(updated.completion or "{}")
        assert blob["unasserted"] is False
        mock_gtd.set_item_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# Disposition -> item status, and the always-posted completion comment
# ---------------------------------------------------------------------------


class TestDispositionToItemStatus:
    """The worker — not the agent — materializes the item transition.

    Mapping under test: `done` -> `review`; no artifact -> `review`;
    `blocked`/`failed` -> item untouched (the two-signals-one-meaning bug: a
    blocked run must never present itself as ready for review).
    """

    _GREEN = GateResult(
        returncode=0, timed_out=False, output="ok", duration_seconds=1.0
    )

    @pytest.mark.asyncio
    async def test_done_moves_item_to_review(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        updated, mock_gtd, _ = await _run_build_worker(
            tmp_path,
            caplog,
            item_id="item-map-done",
            artifact="done",
            gate_result=self._GREEN,
        )
        assert updated.status.value == "succeeded"
        mock_gtd.set_item_status.assert_awaited_once()
        assert mock_gtd.set_item_status.await_args.args[:2] == (
            "item-map-done",
            "review",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disposition", ["blocked", "failed"])
    async def test_blocked_or_failed_never_moves_item_to_review(
        self, tmp_path, caplog: pytest.LogCaptureFixture, disposition
    ) -> None:
        updated, mock_gtd, _ = await _run_build_worker(
            tmp_path,
            caplog,
            item_id=f"item-map-{disposition}",
            artifact=disposition,
            gate_result=self._GREEN,
        )
        assert updated.status.value == "failed"
        mock_gtd.set_item_status.assert_not_awaited()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert not any("review" in b.lower() for b in bodies)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_status", ["review", "done"])
    async def test_done_artifact_does_not_regress_a_finished_item(
        self, tmp_path, caplog: pytest.LogCaptureFixture, current_status
    ) -> None:
        updated, mock_gtd, _ = await _run_build_worker(
            tmp_path,
            caplog,
            item_id=f"item-map-keep-{current_status}",
            artifact="done",
            item_status=current_status,
            gate_result=self._GREEN,
        )
        assert updated.status.value == "succeeded"
        mock_gtd.set_item_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_set_failure_does_not_flip_the_terminal(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-map-boom",
            project_name="TestProject",
            branch_name="feat/unasserted",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-map-boom"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-map-boom",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock(side_effect=RuntimeError("nope"))
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/unasserted")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"

    @pytest.mark.asyncio
    async def test_item_read_failure_leaves_item_untouched(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-map-readfail",
            project_name="TestProject",
            branch_name="feat/unasserted",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-map-readfail"
        fake_workspace.mkdir()

        calls: list[str] = []

        async def _get_item(item_id, **kwargs):
            calls.append(item_id)
            if len(calls) > 1:  # the terminal-path read, not the dispatch-time one
                raise RuntimeError("gtd down")
            return {"id": item_id, "title": "T", "project_id": "proj1"}

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-map-readfail",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.get_item = AsyncMock(side_effect=_get_item)
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/unasserted")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        mock_gtd.set_item_status.assert_not_awaited()


class TestAlwaysPostCompletionComment:
    """The worker's own account of a successful build run, always posted."""

    _GREEN = GateResult(
        returncode=0, timed_out=False, output="ok", duration_seconds=1.0
    )

    @staticmethod
    def _completion_bodies(mock_gtd) -> list[str]:
        return [
            str(c.args[1])
            for c in mock_gtd.post_comment.call_args_list
            if "finished on branch" in str(c.args[1])
        ]

    @pytest.mark.asyncio
    async def test_posted_without_an_artifact_and_names_branch_and_commits(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _updated, mock_gtd, _ = await _run_build_worker(
            tmp_path,
            caplog,
            item_id="item-cc1",
            artifact="absent",
            gate_result=self._GREEN,
        )
        bodies = self._completion_bodies(mock_gtd)
        assert len(bodies) == 1
        assert "feat/unasserted" in bodies[0]
        assert "1 commit(s) across 1 repo(s)" in bodies[0]
        assert "repos-testproj: pushed" in bodies[0]
        assert "Quality gate: `passed`." in bodies[0]
        assert "wrote no completion artifact" in bodies[0]

    @pytest.mark.asyncio
    async def test_enriched_with_artifact_fields_when_present(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _updated, mock_gtd, _ = await _run_build_worker(
            tmp_path,
            caplog,
            item_id="item-cc2",
            artifact="done",
            gate_result=self._GREEN,
        )
        bodies = self._completion_bodies(mock_gtd)
        assert len(bodies) == 1
        assert "Agent disposition: `done`." in bodies[0]
        assert "wrote no completion artifact" not in bodies[0]

    @pytest.mark.asyncio
    async def test_comment_failure_does_not_flip_the_terminal(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-cc3",
            project_name="TestProject",
            branch_name="feat/unasserted",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-cc3"
        fake_workspace.mkdir()

        async def _boom(item_id, content, **kwargs):
            if "finished on branch" in content:
                raise RuntimeError("comment post failed")
            return None

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-cc3",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.post_comment = AsyncMock(side_effect=_boom)
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/unasserted")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        # The status transition still happened — comment failure is isolated.
        mock_gtd.set_item_status.assert_awaited_once()


class TestBuildCompletionCommentComposition:
    """Unit-level: what the worker can say without the agent's help."""

    @staticmethod
    def _artifact(**kwargs):
        from agent_gtd_dispatch.completion import CompletionArtifact

        return CompletionArtifact(**{"disposition": "done", **kwargs})

    def test_no_push_results_still_names_the_branch(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment("run-1", "feat/x", None, None, None)
        assert "feat/x" in body
        assert "0 commit(s) across 0 repo(s)" in body
        assert "Quality gate" not in body

    def test_unknown_branch_is_labelled(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment("run-1", None, [], "passed", None)
        assert "`(unknown)`" in body

    def test_per_repo_lines_carry_status_counts_and_dirty_flag(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment(
            "run-1",
            "feat/x",
            [_pushed(repo_name="repo_a", dirty=True), _no_changes(repo_name="repo_b")],
            "passed",
            None,
        )
        assert "1 commit(s) across 2 repo(s)" in body
        assert "- repo_a: pushed (1 commit(s), aaa1111)" in body
        assert "[dirty working tree]" in body
        assert "- repo_b: no_changes (0 commit(s), aaa1111)" in body

    def test_artifact_fields_enrich_the_body(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment(
            "run-1",
            "feat/x",
            [_pushed()],
            "passed",
            self._artifact(
                summary="did the thing",
                reason="it was already there",
                decision_needed="which schema?",
            ),
        )
        assert "Agent disposition: `done`." in body
        assert "Summary: did the thing" in body
        assert "Reason: it was already there" in body
        assert "Decision needed: which schema?" in body

    def test_blank_artifact_fields_are_omitted(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment(
            "run-1", "feat/x", [_pushed()], "passed", self._artifact(summary="   ")
        )
        assert "Summary:" not in body
        assert "Reason:" not in body
        assert "Decision needed:" not in body
