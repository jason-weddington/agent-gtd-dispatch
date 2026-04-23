"""Tests for attachment staging and system prompt augmentation."""

from __future__ import annotations

import logging
import os
import stat
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from agent_gtd_dispatch import config
from agent_gtd_dispatch.dispatch import build_system_prompt, stage_attachments


@pytest.fixture(autouse=True)
def _env(tmp_path: Path) -> object:
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


_ITEM: dict = {
    "id": "item-123",
    "title": "My Task",
    "description": "Do the thing",
    "project_id": "proj-456",
}

_PROJECT: dict = {
    "id": "proj-456",
    "name": "MyProject",
    "git_origin": "git@host:repos/myrepo",
}


class TestStageAttachments:
    async def test_empty_list_returns_empty_no_dir_created(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(return_value=[])
            result = await stage_attachments(workspace, "run-abc", "item-123")

        assert result == []
        assert not (workspace / "run-abc-attachments").exists()

    async def test_two_attachments_staged_correctly(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        attachments = [
            {
                "id": "att-1",
                "filename": "spec.md",
                "mime_type": "text/markdown",
                "size_bytes": 1024,
            },
            {
                "id": "att-2",
                "filename": "screenshot.png",
                "mime_type": "image/png",
                "size_bytes": 2048,
            },
        ]

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(return_value=attachments)
            mock_client.download_attachment = AsyncMock(
                side_effect=[b"markdown content", b"png bytes"]
            )
            result = await stage_attachments(workspace, "run-abc", "item-123")

        attach_dir = workspace / "run-abc-attachments"
        assert attach_dir.exists()
        # Mode should be 0700
        mode = stat.S_IMODE(attach_dir.stat().st_mode)
        assert mode == 0o700
        # Both files present with correct content
        assert (attach_dir / "spec.md").read_bytes() == b"markdown content"
        assert (attach_dir / "screenshot.png").read_bytes() == b"png bytes"
        # Returned list matches input
        assert len(result) == 2
        assert result[0]["id"] == "att-1"
        assert result[1]["id"] == "att-2"

    async def test_one_download_fails_other_still_staged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        attachments = [
            {
                "id": "att-1",
                "filename": "good.txt",
                "mime_type": "text/plain",
                "size_bytes": 100,
            },
            {
                "id": "att-2",
                "filename": "bad.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 200,
            },
        ]

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(return_value=attachments)
            mock_client.download_attachment = AsyncMock(
                side_effect=[b"good content", Exception("Network error")]
            )
            with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.dispatch"):
                result = await stage_attachments(workspace, "run-abc", "item-123")

        # Only the successful one returned
        assert len(result) == 1
        assert result[0]["id"] == "att-1"

        attach_dir = workspace / "run-abc-attachments"
        assert (attach_dir / "good.txt").exists()
        assert not (attach_dir / "bad.bin").exists()

        # WARNING was logged with the failed attachment id
        assert "att-2" in caplog.text

    async def test_list_fails_returns_empty_no_dir_created(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(
                side_effect=Exception("API unavailable")
            )
            with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.dispatch"):
                result = await stage_attachments(workspace, "run-abc", "item-123")

        assert result == []
        assert not (workspace / "run-abc-attachments").exists()
        assert "Failed to list attachments" in caplog.text

    async def test_filename_sanitization_strips_path_separators(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        attachments = [
            {
                "id": "att-1",
                "filename": "../../../etc/passwd",
                "mime_type": "text/plain",
                "size_bytes": 50,
            },
        ]

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(return_value=attachments)
            mock_client.download_attachment = AsyncMock(return_value=b"content")
            result = await stage_attachments(workspace, "run-abc", "item-123")

        assert len(result) == 1
        attach_dir = workspace / "run-abc-attachments"
        files = list(attach_dir.iterdir())
        assert len(files) == 1
        # No path separators in the written filename (prevents directory traversal)
        assert "/" not in files[0].name
        assert "\\" not in files[0].name
        # File is inside the attach_dir, not outside
        assert files[0].parent == attach_dir

    async def test_filename_sanitization_removes_exotic_chars(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        attachments = [
            {
                "id": "att-1",
                "filename": "my file (draft) @2026!.txt",
                "mime_type": "text/plain",
                "size_bytes": 10,
            },
        ]

        with patch("agent_gtd_dispatch.dispatch.gtd_client") as mock_client:
            mock_client.list_attachments = AsyncMock(return_value=attachments)
            mock_client.download_attachment = AsyncMock(return_value=b"data")
            await stage_attachments(workspace, "run-abc", "item-123")

        attach_dir = workspace / "run-abc-attachments"
        files = list(attach_dir.iterdir())
        assert len(files) == 1
        # Only safe chars in name
        import re

        assert re.fullmatch(r"[A-Za-z0-9._\-]+", files[0].name)


class TestPromptAugmentation:
    def test_with_attachments_includes_supporting_files_section(self) -> None:
        attachments = [
            {
                "id": "att-1",
                "filename": "spec.md",
                "mime_type": "text/markdown",
                "size_bytes": 3277,
            },
            {
                "id": "att-2",
                "filename": "screenshot.png",
                "mime_type": "image/png",
                "size_bytes": 462848,
            },
        ]
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=attachments,
            run_id="run-xyz",
        )
        assert "## Supporting Files" in prompt
        assert "spec.md" in prompt
        assert "screenshot.png" in prompt
        assert "run-xyz-attachments" in prompt
        assert "text/markdown" in prompt
        assert "image/png" in prompt

    def test_with_none_attachments_no_supporting_files(self) -> None:
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=None,
        )
        assert "## Supporting Files" not in prompt

    def test_with_empty_attachments_no_supporting_files(self) -> None:
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=[],
        )
        assert "## Supporting Files" not in prompt

    def test_build_mode_includes_do_not_commit_rule(self) -> None:
        attachments = [
            {
                "id": "att-1",
                "filename": "spec.md",
                "mime_type": "text/markdown",
                "size_bytes": 1000,
            },
        ]
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=attachments,
            run_id="run-xyz",
        )
        # Rule 7 about not committing the attachments dir
        assert "run-xyz-attachments" in prompt
        assert "git add" in prompt.lower() or "Do not `git add`" in prompt

    def test_plan_mode_with_attachments_includes_supporting_files(self) -> None:
        attachments = [
            {
                "id": "att-1",
                "filename": "spec.md",
                "mime_type": "text/markdown",
                "size_bytes": 1000,
            },
        ]
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="plan",
            attachments=attachments,
            run_id="run-xyz",
        )
        assert "## Supporting Files" in prompt
        assert "spec.md" in prompt
        assert "run-xyz-attachments" in prompt

    def test_plan_mode_without_attachments_no_supporting_files(self) -> None:
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="plan",
            attachments=None,
        )
        assert "## Supporting Files" not in prompt

    def test_size_kb_formatted_correctly(self) -> None:
        attachments = [
            {
                "id": "att-1",
                "filename": "file.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 1536,  # 1.5 KB
            },
        ]
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=attachments,
            run_id="run-xyz",
        )
        assert "1.5 KB" in prompt

    def test_do_not_commit_note_in_supporting_files_section(self) -> None:
        attachments = [
            {
                "id": "att-1",
                "filename": "a.txt",
                "mime_type": "text/plain",
                "size_bytes": 100,
            },
        ]
        prompt = build_system_prompt(
            _ITEM,
            _PROJECT,
            "feat/abc",
            100,
            mode="build",
            attachments=attachments,
            run_id="run-xyz",
        )
        # The "DO NOT commit" note must appear in the prompt
        assert "DO NOT" in prompt
        assert "run-xyz-attachments" in prompt
