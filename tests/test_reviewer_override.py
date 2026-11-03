"""Per-tick board reviewer-engine override (AC7 / AC8).

The rollout loop re-reads the board's ``effective_reviewer_engine`` on every
tick so a reviewer-overflow switch reaches live loops; a cleared override
reverts to the base engine the loop resolved at start (the stored
``reviewer_engine``, else ``config.REVIEWER_ENGINE``).  Reviews already in
flight keep their engine; lands that start after the switch use the override
engine.  Transitions are logged exactly once.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent_gtd_dispatch import config, main
from agent_gtd_dispatch.engines import (
    CLAUDE_GLM,
    CLAUDE_SONNET,
    get_engine,
)
from agent_gtd_dispatch.main import ReviewVerdict
from tests.test_concurrent_reviews import (
    _TICK_CONTINUE,
    LOGGER,
    ROLLOUT,
    FakeReviewer,
    _board,
    _cleanup,
    _entry,
    _pending_task,
    _tick,
    _wire,
    _yield,
)
from tests.test_manage_recovery import _loop_patch_stack


@pytest.fixture(autouse=True)
def _env(tmp_path):
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


@pytest.fixture(autouse=True)
def _fresh_host_counter(monkeypatch):
    monkeypatch.setattr(main, "_host_reviews_in_flight", 0)


def _make_cloud_available(monkeypatch) -> None:
    """claude-code-glm available: cloud key set, probe 'valid' (no network)."""
    from agent_gtd_dispatch import cloud_auth

    monkeypatch.setattr(cloud_auth, "_probe_result", None)
    monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "valid")
    monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "cloud")


def _logs(caplog, marker: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


class TestTickReviewerEngineUnit:
    """_tick_reviewer_engine against plain rollout dicts."""

    async def test_no_override_key_leaves_engine_unchanged(self, caplog) -> None:
        """An older board that sends no key behaves exactly as today (AC10)."""
        eng = await main._tick_reviewer_engine({"id": "r1"}, CLAUDE_SONNET)
        assert eng is CLAUDE_SONNET
        assert _logs(caplog, "reviewer-engine") == []

    async def test_blank_override_leaves_engine_unchanged(self, caplog) -> None:
        eng = await main._tick_reviewer_engine(
            {"id": "r1", "effective_reviewer_engine": ""}, CLAUDE_SONNET
        )
        assert eng is CLAUDE_SONNET
        assert _logs(caplog, "reviewer-engine") == []

    async def test_override_equal_to_current_no_switch(self, caplog) -> None:
        eng = await main._tick_reviewer_engine(
            {"id": "r1", "effective_reviewer_engine": "claude-code-sonnet"},
            CLAUDE_SONNET,
        )
        assert eng is CLAUDE_SONNET
        assert _logs(caplog, "reviewer-engine") == []

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("talos-glm", "not-claude-code-family"),
            ("talos-deepseek", "not-claude-code-family"),
            ("kiro", "not-claude-code-family"),
            ("claude-code-bogus", "unknown-engine"),
        ],
    )
    async def test_ignored_override_with_reason(self, name, reason, caplog) -> None:
        rollout = {"id": "r1", "effective_reviewer_engine": name}
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            eng = await main._tick_reviewer_engine(rollout, CLAUDE_SONNET)
        assert eng is CLAUDE_SONNET
        ignored = _logs(caplog, "reviewer-engine-override-ignored")
        assert len(ignored) == 1
        assert "rollout_id=r1" in ignored[0]
        assert f"engine={name}" in ignored[0]
        assert f"reason={reason}" in ignored[0]

    async def test_unavailable_override_ignored(self, monkeypatch, caplog) -> None:
        """claude-code-glm is a valid claude-family engine but its cloud key
        is absent on this host → unavailable-on-host."""
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            eng = await main._tick_reviewer_engine(
                {"id": "r1", "effective_reviewer_engine": "claude-code-glm"},
                CLAUDE_SONNET,
            )
        assert eng is CLAUDE_SONNET
        ignored = _logs(caplog, "reviewer-engine-override-ignored")
        assert len(ignored) == 1
        assert "reason=unavailable-on-host" in ignored[0]

    async def test_available_override_switches(self, monkeypatch, caplog) -> None:
        _make_cloud_available(monkeypatch)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            eng = await main._tick_reviewer_engine(
                {"id": "r1", "effective_reviewer_engine": "claude-code-glm"},
                CLAUDE_SONNET,
            )
        assert eng is CLAUDE_GLM
        switches = _logs(caplog, "reviewer-engine-switch")
        assert len(switches) == 1
        assert "rollout_id=r1" in switches[0]
        assert "from=claude-code-sonnet" in switches[0]
        assert "to=claude-code-glm" in switches[0]

    async def test_engine_object_roundtrip(self, monkeypatch) -> None:
        _make_cloud_available(monkeypatch)
        eng = await main._tick_reviewer_engine(
            {"id": "r1", "effective_reviewer_engine": "claude-code-glm"},
            CLAUDE_SONNET,
        )
        assert get_engine("claude-code-glm") is eng
        assert eng.is_claude_code_family is True


class TestTickReviewerEngineLoopState:
    """Transition-only logging via _RolloutLoopState.applied_reviewer_engine."""

    async def test_same_override_logs_once(self, monkeypatch, caplog) -> None:
        from agent_gtd_dispatch.main import _RolloutLoopState

        ls = _RolloutLoopState()
        _make_cloud_available(monkeypatch)
        rollout = {"id": "r1", "effective_reviewer_engine": "claude-code-glm"}
        with caplog.at_level(logging.INFO, logger=LOGGER):
            for _ in range(3):
                assert (
                    await main._tick_reviewer_engine(rollout, CLAUDE_SONNET, ls)
                    is CLAUDE_GLM
                )
        assert len(_logs(caplog, "reviewer-engine-switch")) == 1
        assert ls.applied_reviewer_engine == "claude-code-glm"

    async def test_revert_logs_once(self, monkeypatch, caplog) -> None:
        from agent_gtd_dispatch.main import _RolloutLoopState

        ls = _RolloutLoopState()
        _make_cloud_available(monkeypatch)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await main._tick_reviewer_engine(
                {"id": "r1", "effective_reviewer_engine": "claude-code-glm"},
                CLAUDE_SONNET,
                ls,
            )
            await main._tick_reviewer_engine(
                {"id": "r1", "effective_reviewer_engine": ""}, CLAUDE_SONNET, ls
            )
        switches = _logs(caplog, "reviewer-engine-switch")
        assert len(switches) == 2
        assert "from=claude-code-sonnet to=claude-code-glm" in switches[0]
        assert "from=claude-code-glm to=claude-code-sonnet" in switches[1]
        assert ls.applied_reviewer_engine == "claude-code-sonnet"

    async def test_newly_ignored_override_publishes_once(
        self, monkeypatch, caplog
    ) -> None:
        from agent_gtd_dispatch.main import _RolloutLoopState

        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, logger=LOGGER),
        ):
            for _ in range(3):
                await main._tick_reviewer_engine(
                    {"id": "r1", "effective_reviewer_engine": "talos-glm"},
                    CLAUDE_SONNET,
                    ls,
                )
            ignored = _logs(caplog, "reviewer-engine-override-ignored")
            assert len(ignored) == 1
            assert "reason=not-claude-code-family" in ignored[0]
            # The ignored override is named in exactly one rollout-phase post.
            posts = ctx.gtd.update_rollout_state.await_args_list
            assert len(posts) == 1
            assert "talos-glm" in str(posts[0])
            assert "not-claude-code-family" in str(posts[0])


def _set_override(ctx, engine: str) -> None:
    """Put the override on the board's rollout dict (AFTER _board, which
    replaces get_rollout's return value each call)."""
    rollout = ctx.gtd.get_rollout.return_value
    rollout["id"] = ROLLOUT
    rollout["effective_reviewer_engine"] = engine


class TestTickIntegration:
    """_rollout_wave_tick re-reads effective_reviewer_engine every tick."""

    async def test_switch_reaches_review_and_land_of_the_tick(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        reviewer = FakeReviewer()
        ls = main._RolloutLoopState()
        b_task = _pending_task()
        ls.reviews["x"] = _entry("x", 2, b_task)
        land_kw: list[dict[str, Any]] = []

        async def _land(rollout_id, item_id, verdict, **kw: Any):
            land_kw.append(kw)
            return verdict

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_land_reviewed_merge", new=_land),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _make_cloud_available(monkeypatch)
            _board(ctx, in_progress=["a"])
            _set_override(ctx, "claude-code-glm")
            assert await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET) == _TICK_CONTINUE
            await _yield()
            # The review started in this tick used the override engine.
            assert reviewer.calls[0][1]["reviewer_engine"] is CLAUDE_GLM
            # The review already in flight keeps its own task (and engine).
            assert ls.reviews["x"].task is b_task
            assert "x" not in reviewer.items()
            # A land queued after the switch constructs with the override engine.
            reviewer.release("a", ReviewVerdict("merge", "r", "n", ""))
            await _yield()
            assert await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET) == _TICK_CONTINUE
            await _yield()
            assert land_kw, "the merge for item a was not landed"
            assert land_kw[0]["reviewer_engine"] is CLAUDE_GLM
            # Transition logging: exactly one switch line for the change.
            switches = _logs(caplog, "reviewer-engine-switch")
            assert len(switches) == 1
            assert "rollout_id=rollout-abc" in switches[0]
            assert "from=claude-code-sonnet to=claude-code-glm" in switches[0]
            await _cleanup(ls)

    async def test_cleared_override_reverts_to_base_engine(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        reviewer = FakeReviewer()
        ls = main._RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _make_cloud_available(monkeypatch)
            _board(ctx, in_progress=["a"])
            _set_override(ctx, "claude-code-glm")
            assert await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET) == _TICK_CONTINUE
            await _yield()
            assert reviewer.calls[0][1]["reviewer_engine"] is CLAUDE_GLM
            reviewer.release("a")  # skip verdict
            await _yield()
            # The board clears the override: the stored reviewer_engine is null
            # on the rollout, so the next review falls back to
            # config.REVIEWER_ENGINE (the base engine the loop resolved at start).
            _board(ctx, in_progress=["b"])
            _set_override(ctx, "")
            assert await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET) == _TICK_CONTINUE
            await _yield()
            assert (
                reviewer.calls[1][1]["reviewer_engine"].name == config.REVIEWER_ENGINE
            )
            switches = _logs(caplog, "reviewer-engine-switch")
            assert len(switches) == 2
            assert "from=claude-code-sonnet to=claude-code-glm" in switches[0]
            assert "from=claude-code-glm to=claude-code-sonnet" in switches[1]
            await _cleanup(ls)

    async def test_same_override_three_ticks_one_switch_log(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        reviewer = FakeReviewer()
        ls = main._RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _make_cloud_available(monkeypatch)
            for item in ("a", "b", "c"):
                _board(ctx, in_progress=[item])
                _set_override(ctx, "claude-code-glm")
                assert (
                    await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET)
                    == _TICK_CONTINUE
                )
                await _yield()
                assert reviewer.calls[-1][1]["reviewer_engine"] is CLAUDE_GLM
                reviewer.release(item)  # skip verdict
                await _yield()
            # Three consecutive ticks, same override: exactly ONE switch line.
            assert len(_logs(caplog, "reviewer-engine-switch")) == 1
            # Clearing the override produces exactly one revert line.
            _board(ctx, in_progress=["d"])
            _set_override(ctx, "")
            assert await _tick(ctx, ls, reviewer_engine=CLAUDE_SONNET) == _TICK_CONTINUE
            await _yield()
            assert (
                reviewer.calls[-1][1]["reviewer_engine"].name == config.REVIEWER_ENGINE
            )
            switches = _logs(caplog, "reviewer-engine-switch")
            assert len(switches) == 2
            assert "from=claude-code-glm to=claude-code-sonnet" in switches[1]
            await _cleanup(ls)
