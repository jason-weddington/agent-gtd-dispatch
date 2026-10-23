"""Tests for the agent_gtd_dispatch_protocol package exports."""

from __future__ import annotations

from datetime import UTC, datetime


class TestProtocolExports:
    def test_run_status(self) -> None:
        from agent_gtd_dispatch_protocol import RunStatus

        assert RunStatus("pending") == RunStatus.pending

    def test_dispatch_request(self) -> None:
        from agent_gtd_dispatch_protocol import DispatchRequest

        req = DispatchRequest(max_turns=10)
        assert req.max_turns == 10

    def test_run_response(self) -> None:
        from agent_gtd_dispatch_protocol import RunResponse, RunStatus

        resp = RunResponse(
            id="x",
            item_id=None,
            project_name="p",
            branch_name=None,
            engine="claude-code",
            agent_name=None,
            mode="build",
            rollout_id=None,
            status=RunStatus.pending,
            started_at=None,
            completed_at=None,
            exit_code=None,
            error=None,
            created_at=datetime.now(UTC),
        )
        assert resp.id == "x"

    def test_build_engine(self) -> None:
        from agent_gtd_dispatch_protocol import BuildEngine

        assert [m.value for m in BuildEngine] == [
            "claude-code",
            "claude-code-ollama",
            "claude-code-glm",
            "claude-code-sonnet",
            "claude-code-haiku",
            "kiro",
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
            "talos-qwen",
            "talos-glm",
            "talos-glm-flash",
        ]
        assert BuildEngine("talos-glm-flash") is BuildEngine.TALOS_GLM_FLASH
