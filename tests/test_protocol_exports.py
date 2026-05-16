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
            engine="claude",
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

    def test_plan_request(self) -> None:
        from agent_gtd_dispatch_protocol import PlanRequest

        req = PlanRequest(item_ids=["a"])
        assert req.item_ids == ["a"]

    def test_dag_edge(self) -> None:
        from agent_gtd_dispatch_protocol import DagEdge

        edge = DagEdge(from_item_id="a", to_item_id="b")
        assert edge.from_item_id == "a"
        assert edge.to_item_id == "b"

    def test_rollout_plan(self) -> None:
        from agent_gtd_dispatch_protocol import RolloutPlan

        plan = RolloutPlan(nodes=["a"], edges=[], planner_model="m")
        assert plan.nodes == ["a"]
        assert plan.edges == []
        assert plan.planner_model == "m"
