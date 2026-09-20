"""Tests for the post-run quality gate (dispatch.run_gate_command + worker wiring)."""

from __future__ import annotations

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
    seed_build_evidence,
)
from tests.worker_mocks import stub_rescue


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

    def test_gate_sees_the_dirty_tree_and_nothing_is_stashed(self, tmp_path) -> None:
        """The gate judges the tree AS THE AGENT LEFT IT.

        The inverse of the behaviour this replaced. ``run_gate_command`` used to
        ``git stash push`` a dirty repo first, so the gate ran against the pristine
        base commit — which is how four agents' uncommitted work was judged
        "already done" and deleted: the gate passed trivially because there was
        nothing left in the tree to fail it.
        """
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

        # `git diff --quiet` exits 1 on a dirty tree. Under the old stashing
        # behaviour this returned 0.
        result = dispatch.run_gate_command(repo, "git diff --quiet", 30, CLAUDE)
        assert result.returncode == 1

        stash_list = subprocess.run(
            ["git", "stash", "list"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert stash_list.stdout.strip() == ""
        assert (repo / "a.txt").read_text() == "two\n"


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
):
    seed_build_evidence(fake_workspace)
    stub_rescue(mock_dispatch)
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
        # No 6th argument: the dirty-repo stash list is gone with the stashing.
        assert len(call.args) == 5

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

    @pytest.mark.asyncio
    async def test_zero_commits_fails_without_ever_running_the_gate(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A commit-less claude-code build fails, and the gate is NOT run.

        This is the central behaviour change. What used to happen: the worker
        stashed the agent's uncommitted work, ran the gate against the resulting
        pristine base commit, got a trivially green result, read that as
        corroboration of a no-op, recorded `already_satisfied`, moved the item to
        review and deleted the clone. Roughly 560 agent-turns of real work, gone,
        reported as success.

        Two assertions carry the fix. The terminal is `failed` — there is no
        longer any route from zero commits to a non-failure status for this
        engine. And the gate is never invoked: an unchanged tree passes trivially,
        so gating one costs ~6 minutes to learn a fact about the base commit.
        """
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-f",
            project_name="TestProject",
            branch_name="feat/f",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-f"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-f",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_no_changes(branch="feat/f")]
            )
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("zero_commits: ")
        mock_dispatch.run_gate_command.assert_not_called()
        # The item must NOT be nudged to review — there is nothing to review.
        mock_gtd.set_item_status.assert_not_called()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("Build run failed (zero_commits)" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_zero_commits_splits_abandoned_work_from_wrote_nothing(
        self, tmp_path
    ) -> None:
        """A dirty tree gets its own terminal, because it needs the opposite response.

        Zero commits covers two opposite situations. A DIRTY tree means the agent
        was part-way through real work and stopped — usually by ending its turn
        while something it started was still running — and the rescue path has put
        those changes on a branch to review. A CLEAN tree means it wrote nothing,
        which is exactly what a correct refusal looks like when an agent checks its
        acceptance criteria, finds the spec no longer matches the repo, and declines
        to guess rather than inventing work.

        Flattening the two makes a fleet look less reliable than it is while hiding
        the real defect, which in the refusal case is a stale spec and not the agent.
        The split is mechanical — `dirty` is observed on the push result, never
        inferred — and neither terminal claims to know WHY the tree was clean.
        """
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-dirty",
            project_name="TestProject",
            branch_name="feat/dirty",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-dirty"
        fake_workspace.mkdir()

        dirty_result = _no_changes(branch="feat/dirty")
        dirty_result.dirty = True

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-dirty",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(return_value=[dirty_result])
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("zero_commits_work_abandoned: ")
        # Still no gate: there are no commits to judge either way.
        mock_dispatch.run_gate_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_commits_fails_even_on_an_ungated_project(
        self, tmp_path
    ) -> None:
        """No gate_command changes nothing: zero commits is decided before the gate."""
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-i",
            project_name="TestProject",
            branch_name="feat/i",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        fake_workspace = tmp_path / "repos-testproj-i"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-i",
                project=_default_project(gate_command=""),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_no_changes(branch="feat/i")]
            )
            mock_dispatch.run_gate_command = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error.startswith("zero_commits: ")
        mock_dispatch.run_gate_command.assert_not_called()

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
    async def test_dirty_repo_is_gated_as_is_and_never_stashed(self, tmp_path) -> None:
        """A dirty working tree is handed to the gate untouched.

        The worker no longer parks uncommitted changes before gating, so it also
        no longer tells the reviewer that it did. What happens to that dirty tree
        now is the rescue path at teardown, which commits and pushes it to the
        run's own branch instead of hiding it.
        """
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
        assert len(call.args) == 5

        comment_texts = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        pass_comments = [t for t in comment_texts if "Post-run gate passed" in t]
        assert len(pass_comments) == 1
        assert "stashed" not in pass_comments[0]

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
            stub_rescue(mock_dispatch)
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
    async def test_posted_from_worker_observation_alone(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Branch, commit counts, push outcomes and gate decision — nothing else.

        The body no longer varies with anything the agent did or did not produce:
        there is exactly one shape, composed from what the worker measured.
        """
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-cc1",
            project_name="TestProject",
            branch_name="feat/cc1",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-cc1"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-cc1",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/cc1")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        bodies = self._completion_bodies(mock_gtd)
        assert len(bodies) == 1
        assert "feat/cc1" in bodies[0]
        assert "1 commit(s) across 1 repo(s)" in bodies[0]
        assert "repos-testproj: pushed" in bodies[0]
        assert "Quality gate: `passed`." in bodies[0]
        # No vocabulary from the deleted artifact contract may survive here.
        assert "artifact" not in bodies[0].lower()
        assert "disposition" not in bodies[0].lower()

    @pytest.mark.asyncio
    async def test_comment_failure_does_not_flip_the_terminal(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-cc3",
            project_name="TestProject",
            branch_name="feat/cc3",
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
                return_value=[_pushed(branch="feat/cc3")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        # The status transition still happened — comment failure is isolated.
        mock_gtd.set_item_status.assert_awaited_once()


class TestBuildCompletionCommentComposition:
    """Unit-level: the comment is composed only from observed facts."""

    def test_no_push_results_still_names_the_branch(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment("run-1", "feat/x", None, None)
        assert "feat/x" in body
        assert "0 commit(s) across 0 repo(s)" in body
        assert "Quality gate" not in body

    def test_unknown_branch_is_labelled(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment("run-1", None, [], "passed")
        assert "`(unknown)`" in body

    def test_per_repo_lines_carry_status_counts_and_dirty_flag(self) -> None:
        from agent_gtd_dispatch.main import build_completion_comment

        body = build_completion_comment(
            "run-1",
            "feat/x",
            [_pushed(repo_name="repo_a", dirty=True), _no_changes(repo_name="repo_b")],
            "passed",
        )
        assert "1 commit(s) across 2 repo(s)" in body
        assert "- repo_a: pushed (1 commit(s), aaa1111)" in body
        assert "[dirty working tree]" in body
        assert "- repo_b: no_changes (0 commit(s), aaa1111)" in body


class TestRescueBeforeTeardown:
    """Worker wiring: the rescue runs before teardown and gates the teardown."""

    _GREEN = GateResult(
        returncode=0, timed_out=False, output="ok", duration_seconds=1.0
    )

    async def _run(self, tmp_path, *, item_id, branch, rescue, push_results):
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id=item_id,
            project_name="TestProject",
            branch_name=branch,
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / f"repos-testproj-{item_id}"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id=item_id,
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(return_value=push_results)
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=rescue)

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        return updated, mock_gtd, mock_dispatch

    @pytest.mark.asyncio
    async def test_successful_rescue_comments_and_still_tears_down(
        self, tmp_path
    ) -> None:
        rescue = dispatch.RescueResult(
            "repos-testproj", attempted=True, pushed=True, committed=True
        )
        _updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r1",
            branch="feat/r1",
            rescue=rescue,
            push_results=[_pushed(branch="feat/r1")],
        )
        mock_dispatch.cleanup_workspace.assert_called_once()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        rescue_comments = [b for b in bodies if "Unreviewed partial work" in b]
        assert len(rescue_comments) == 1
        assert "feat/r1" in rescue_comments[0]
        # The comment must not let a reader mistake this for reviewed work.
        assert "hooks were skipped" in rescue_comments[0]
        assert "passed no quality gate" in rescue_comments[0]

    @pytest.mark.asyncio
    async def test_failed_rescue_retains_the_workspace_and_says_so(
        self, tmp_path
    ) -> None:
        """A rescue that could not push means the clone is the only copy left."""
        rescue = dispatch.RescueResult(
            "repos-testproj",
            attempted=True,
            pushed=False,
            committed=True,
            error="git push failed in repos-testproj: no route to host",
        )
        updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r2",
            branch="feat/r2",
            rescue=rescue,
            push_results=[_pushed(branch="feat/r2")],
        )
        mock_dispatch.cleanup_workspace.assert_not_called()
        assert updated.error is not None
        assert "rescue_push_failed" in updated.error
        assert "RETAINED" in updated.error
        assert "no route to host" in updated.error
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("could NOT rescue" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_nothing_to_rescue_posts_nothing(self, tmp_path) -> None:
        rescue = dispatch.RescueResult(
            "repos-testproj", attempted=False, pushed=False, committed=False
        )
        _updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r3",
            branch="feat/r3",
            rescue=rescue,
            push_results=[_pushed(branch="feat/r3")],
        )
        mock_dispatch.cleanup_workspace.assert_called_once()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert not any("Unreviewed partial work" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_rescue_runs_on_a_failed_run_too(self, tmp_path) -> None:
        """The failure path is exactly where unpushed work is most likely.

        A zero-commit run returns early from the terminal classification; the
        rescue lives in the teardown ``finally``, so it still runs — and that run
        is the one whose working tree most often holds everything the agent did.
        """
        rescue = dispatch.RescueResult(
            "repos-testproj", attempted=True, pushed=True, committed=True
        )
        updated, mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r4",
            branch="feat/r4",
            rescue=rescue,
            push_results=[_no_changes(branch="feat/r4")],
        )
        assert updated.status.value == "failed"
        assert (updated.error or "").startswith("zero_commits: ")
        mock_dispatch.rescue_abandoned_work.assert_called_once()
        bodies = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("Unreviewed partial work" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_rescue_never_touches_a_non_feature_branch(self, tmp_path) -> None:
        """Blast-radius guard: this pushes with hooks off, so `feat/*` only."""
        rescue = dispatch.RescueResult(
            "repos-testproj", attempted=True, pushed=True, committed=True
        )
        _updated, _mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r5",
            branch="main",
            rescue=rescue,
            push_results=[_pushed(branch="main")],
        )
        mock_dispatch.rescue_abandoned_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_rescue_does_not_abort_teardown(self, tmp_path) -> None:
        """The rescue runs inside the teardown ``finally``.

        An exception escaping it would skip ``cleanup_workspace`` and leak the
        clone on every run, so each repo's rescue is individually contained.
        """
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-r6",
            project_name="TestProject",
            branch_name="feat/r6",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-r6"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-r6",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/r6")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)
            mock_dispatch.rescue_abandoned_work = MagicMock(
                side_effect=OSError("git vanished")
            )

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        mock_dispatch.cleanup_workspace.assert_called_once()

    @pytest.mark.asyncio
    async def test_rescue_comment_failure_is_isolated(self, tmp_path) -> None:
        """A GTD outage must not stop the workspace being torn down."""
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-r7",
            project_name="TestProject",
            branch_name="feat/r7",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-r7"
        fake_workspace.mkdir()

        async def _boom(item_id, content, **kwargs):
            if "Unreviewed partial work" in content:
                raise RuntimeError("gtd down")
            return None

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-r7",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_gtd.post_comment = AsyncMock(side_effect=_boom)
            mock_gtd.set_item_status = AsyncMock()
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[_pushed(branch="feat/r7")]
            )
            mock_dispatch.run_gate_command = MagicMock(return_value=self._GREEN)
            mock_dispatch.rescue_abandoned_work = MagicMock(
                return_value=dispatch.RescueResult(
                    "repos-testproj", attempted=True, pushed=True, committed=True
                )
            )

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"
        mock_dispatch.cleanup_workspace.assert_called_once()

    @pytest.mark.asyncio
    async def test_rescue_failure_error_text_appends_to_an_existing_error(
        self, tmp_path
    ) -> None:
        """A failed run already has an error; the rescue note must not clobber it."""
        rescue = dispatch.RescueResult(
            "repos-testproj",
            attempted=True,
            pushed=False,
            committed=True,
            error="git push failed in repos-testproj: remote hung up",
        )
        updated, _mock_gtd, mock_dispatch = await self._run(
            tmp_path,
            item_id="item-r8",
            branch="feat/r8",
            rescue=rescue,
            push_results=[_no_changes(branch="feat/r8")],
        )
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert updated.error.startswith("zero_commits: ")
        assert "rescue_push_failed" in updated.error
        mock_dispatch.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_mode_has_no_repos_so_no_rescue(self, tmp_path) -> None:
        """Only BUILD runs record repos; there is nothing to inspect otherwise."""
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-r9",
            project_name="TestProject",
            branch_name="feat/r9",
            mode=DispatchMode.PLAN,
        )
        await db.insert_run(run)
        fake_workspace = tmp_path / "repos-testproj-r9"
        fake_workspace.mkdir()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            _install_common_mocks(
                mock_gtd,
                mock_dispatch,
                item_id="item-r9",
                project=_default_project(),
                fake_workspace=fake_workspace,
            )
            mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))

            await _dispatch_worker(run, 50, CLAUDE, 600)

        mock_dispatch.rescue_abandoned_work.assert_not_called()


class TestWorkerItemRoutingTolerance:
    """Every GTD call the worker makes after a terminal is best-effort.

    The run row is the source of truth about what happened. A flaky GTD API must
    never flip an already-recorded terminal, and must never leave the worker
    raising inside a teardown path.
    """

    @pytest.mark.asyncio
    async def test_unreadable_item_status_leaves_the_item_untouched(self) -> None:
        from agent_gtd_dispatch.main import _nudge_item_to_review

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.get_item = AsyncMock(side_effect=RuntimeError("gtd down"))
            mock_gtd.set_item_status = AsyncMock()
            await _nudge_item_to_review(
                "item-x", "run-x", callback_token=None, terminal="succeeded"
            )
        # Never guess: a read failure must not become a blind PATCH.
        mock_gtd.set_item_status.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current", ["review", "done"])
    async def test_an_item_already_moved_on_is_not_regressed(self, current) -> None:
        from agent_gtd_dispatch.main import _nudge_item_to_review

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.get_item = AsyncMock(return_value={"status": current})
            mock_gtd.set_item_status = AsyncMock()
            await _nudge_item_to_review(
                "item-x", "run-x", callback_token=None, terminal="succeeded"
            )
        mock_gtd.set_item_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_status_patch_is_swallowed(self) -> None:
        from agent_gtd_dispatch.main import _best_effort_set_item_status

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.set_item_status = AsyncMock(side_effect=RuntimeError("gtd down"))
            # Must not raise.
            await _best_effort_set_item_status(
                "item-x", "review", "run-x", callback_token=None, terminal="succeeded"
            )

    @pytest.mark.asyncio
    async def test_already_satisfied_comment_failure_is_swallowed(self) -> None:
        from agent_gtd_dispatch.main import _route_already_satisfied_item

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.set_item_status = AsyncMock()
            mock_gtd.post_comment = AsyncMock(side_effect=RuntimeError("gtd down"))
            await _route_already_satisfied_item(
                "item-x",
                "run-x",
                "already there at foo.py:12",
                callback_token=None,
                attribution=None,
            )
        # The status set still happened — the comment is the part that failed.
        mock_gtd.set_item_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_satisfied_comment_names_the_reason_and_not_the_agent(
        self,
    ) -> None:
        """Talos VERIFIED the no-op; the wording must not read as an agent claim."""
        from agent_gtd_dispatch.main import _route_already_satisfied_item

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.set_item_status = AsyncMock()
            mock_gtd.post_comment = AsyncMock()
            await _route_already_satisfied_item(
                "item-x",
                "run-x",
                "already there at foo.py:12",
                callback_token=None,
                attribution=None,
            )
        body = str(mock_gtd.post_comment.await_args.args[1])
        assert "already there at foo.py:12" in body
        assert "its own checks passed" in body
        assert "was NOT completed" in body

    @pytest.mark.asyncio
    async def test_rollout_skip_failure_does_not_raise(self) -> None:
        from agent_gtd_dispatch.main import _complete_rollout_item_skipped

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.complete_item_in_rollout = AsyncMock(
                side_effect=RuntimeError("gtd down")
            )
            mock_gtd.post_comment = AsyncMock()
            await _complete_rollout_item_skipped(
                "ro-1", "item-x", "run-x", callback_token=None, attribution=None
            )
        mock_gtd.post_comment.assert_not_awaited()


class TestRescueHelperDirectly:
    """Unit-level coverage of ``_rescue_before_teardown``'s own failure handling."""

    @staticmethod
    def _run(branch="feat/z", item_id="item-z"):
        return Run(
            id="run-z",
            item_id=item_id,
            project_name="TestProject",
            branch_name=branch,
            mode=DispatchMode.BUILD,
        )

    @pytest.mark.asyncio
    async def test_no_repos_returns_none(self, tmp_path) -> None:
        from agent_gtd_dispatch.main import _rescue_before_teardown

        assert await _rescue_before_teardown(self._run(), [], attribution=None) is None

    @pytest.mark.asyncio
    async def test_a_failing_db_write_does_not_raise_into_teardown(
        self, tmp_path
    ) -> None:
        """The error text is best-effort; losing it must not abort cleanup."""
        from agent_gtd_dispatch.main import _rescue_before_teardown

        failed = dispatch.RescueResult(
            "repo-a", attempted=True, pushed=False, committed=True, error="push refused"
        )
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.db") as mock_db,
        ):
            mock_dispatch._executor = None
            mock_dispatch.RescueResult = dispatch.RescueResult
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=failed)
            mock_gtd.post_comment = AsyncMock()
            mock_db.get_run = AsyncMock(side_effect=RuntimeError("db gone"))

            outcome = await _rescue_before_teardown(
                self._run(), [("repo-a", tmp_path, None)], attribution=None
            )

        assert outcome is not None
        assert outcome.ok is False
        # The operator still hears about it via the GTD comment.
        assert mock_gtd.post_comment.await_count == 1

    @pytest.mark.asyncio
    async def test_a_run_with_no_item_still_rescues(self, tmp_path) -> None:
        """Pushing the work matters more than being able to announce it."""
        from agent_gtd_dispatch.main import _rescue_before_teardown

        pushed = dispatch.RescueResult(
            "repo-a", attempted=True, pushed=True, committed=True
        )
        run = self._run()
        run.item_id = None
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        ):
            mock_dispatch._executor = None
            mock_dispatch.RescueResult = dispatch.RescueResult
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=pushed)
            mock_gtd.post_comment = AsyncMock()

            outcome = await _rescue_before_teardown(
                run, [("repo-a", tmp_path, None)], attribution=None
            )

        assert outcome is not None
        assert outcome.ok is True
        mock_dispatch.rescue_abandoned_work.assert_called_once()
        mock_gtd.post_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_partial_multi_repo_rescue_is_not_ok(self, tmp_path) -> None:
        """One repo left behind is enough to retain the workspace.

        A workspace run has many clones; ``ok`` must mean every repo that had work
        got it to origin, not that some did.
        """
        from agent_gtd_dispatch.main import _rescue_before_teardown

        results = {
            "repo-a": dispatch.RescueResult(
                "repo-a", attempted=True, pushed=True, committed=True
            ),
            "repo-b": dispatch.RescueResult(
                "repo-b",
                attempted=True,
                pushed=False,
                committed=True,
                error="no route",
            ),
        }
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.db") as mock_db,
        ):
            mock_dispatch._executor = None
            mock_dispatch.RescueResult = dispatch.RescueResult
            mock_dispatch.rescue_abandoned_work = MagicMock(
                side_effect=lambda name, *_a: results[name]
            )
            mock_gtd.post_comment = AsyncMock()
            mock_db.get_run = AsyncMock(return_value=None)
            mock_db.update_run = AsyncMock()

            outcome = await _rescue_before_teardown(
                self._run(),
                [("repo-a", tmp_path, None), ("repo-b", tmp_path, None)],
                attribution=None,
            )

        assert outcome is not None
        assert outcome.ok is False
        assert len(outcome.attempted) == 2
