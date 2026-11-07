"""Base-drift conversion: a conflicting reviewer halt re-dispatches from main."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_gtd_dispatch import config, dispatch, main
from agent_gtd_dispatch.dispatch import BaseDrift, RolloutWorkspace
from agent_gtd_dispatch.main import ReviewVerdict

BRANCH = "feat/aaaaaaaa-x"
_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *_ID, *args], cwd=repo, capture_output=True, check=True, text=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", "/nonexistent-ws")
    monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
    monkeypatch.setattr(main, "MAX_ITEM_REDISPATCHES", 1)
    yield
    main._rollout_redispatches.clear()
    main._rollout_drift_redispatched.clear()


def _setup(
    tmp_path: Path, *, behind: int, depth: int = 50, branch_at_tip: bool = False
) -> RolloutWorkspace:
    """Origin with main + BRANCH cut early; *behind* later main commits."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _commit(seed, "base")
    _git(seed, "push", "-q", "origin", "main")
    if branch_at_tip:
        for i in range(behind):
            _commit(seed, f"m{i}")
    _git(seed, "checkout", "-q", "-b", BRANCH)
    _commit(seed, "feature")
    _git(seed, "push", "-q", "origin", BRANCH)
    _git(seed, "checkout", "-q", "main")
    if not branch_at_tip:
        for i in range(behind):
            _commit(seed, f"m{i}")
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", f"--depth={depth}", f"file://{origin}", str(clone)],
        check=True,
        capture_output=True,
    )
    return RolloutWorkspace(
        root=tmp_path,
        repo_paths={"r": clone},
        default_branches={"r": "main"},
        created=True,
        workspace_mode=True,
    )


class TestMeasure:
    def test_behind(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=3)
        res = dispatch.measure_base_drift(ws, BRANCH, ["r"], 30)
        d = res["r"]
        assert d is not None and d.behind == 3 and len(d.merge_base) == 7

    def test_not_behind(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=3, branch_at_tip=True)
        assert dispatch.measure_base_drift(ws, BRANCH, ["r"], 30) == {"r": None}

    def test_missing_branch(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=1)
        assert dispatch.measure_base_drift(ws, "feat/nope", ["r"], 30) == {"r": None}

    def test_protected(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=1)
        assert dispatch.measure_base_drift(ws, "", ["r"], 30) == {"r": None}
        assert dispatch.measure_base_drift(ws, "main", ["r"], 30) == {"r": None}

    def test_timeout(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        ws = _setup(tmp_path, behind=1)
        with (
            caplog.at_level(logging.WARNING),
            patch.object(
                dispatch, "_land_git", side_effect=dispatch.LandGitTimeout("fetch")
            ),
        ):
            assert dispatch.measure_base_drift(ws, BRANCH, ["r"], 1) == {"r": None}
        assert "base-drift: measure_failed" in caplog.text
        assert "step=fetch_default" in caplog.text

    def test_deepen_shallow_depth1(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=3, depth=1)
        d = dispatch.measure_base_drift(ws, BRANCH, ["r"], 30)["r"]
        assert d is not None and d.behind == 3

    def test_deepen_60_behind(self, tmp_path: Path) -> None:
        ws = _setup(tmp_path, behind=60, depth=50)
        d = dispatch.measure_base_drift(ws, BRANCH, ["r"], 60)["r"]
        assert d is not None and d.behind == 60


def _ws2() -> RolloutWorkspace:
    return RolloutWorkspace(
        root=Path("/x"),
        repo_paths={"a": Path("/x/a"), "b": Path("/x/b")},
        default_branches={"a": "main", "b": "master"},
        created=False,
        workspace_mode=True,
    )


def _verdict(paths: tuple[str, ...] = ("a/x.py", "a/y.py")) -> ReviewVerdict:
    return ReviewVerdict(
        "halt",
        "conflicts",
        "",
        "[conflicts needing judgment: a/x.py, a/y.py]",
        branch_name=BRANCH,
        judgment_conflict_paths=paths,
    )


async def _act(outcome, ws, measure, *, drift=True):
    with (
        patch.object(main, "_redispatch_item", new=AsyncMock()) as red,
        patch.object(
            main, "_halt_rollout_from_loop", new=AsyncMock(return_value="halted")
        ) as halt,
        patch.object(main.gtd_client, "post_comment", new=AsyncMock()) as post,
        patch.object(dispatch, "measure_base_drift", side_effect=measure) as m,
    ):
        res = await main._act_on_verdict(
            "rid",
            "iid",
            outcome,
            token=None,
            attribution=None,
            workspace=ws,
            consider_drift=drift,
        )
    return res, red, halt, post, m


class TestAct:
    async def test_drift_redispatches(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            res, red, halt, post, _ = await _act(
                _verdict(), _ws2(), lambda *a: {"a": BaseDrift("a", "088da08", 412)}
            )
        assert res == main._TICK_CONTINUE
        red.assert_awaited_once()
        halt.assert_not_awaited()
        text = post.await_args.args[1]
        assert (
            "base drift: 2 conflicting paths, merge-base 088da08 is 412 commits "
            "behind origin/main" in text
        )
        assert "[conflicts needing judgment: a/x.py" in text
        assert "drifted_repos=a:412" in text
        assert main._rollout_redispatches["rid"]["iid"] == 1
        assert "decision=redispatch reason=drifted" in caplog.text

    async def test_plain_redispatch_comment_unchanged(self) -> None:
        v = ReviewVerdict("re-dispatch", "why", "", "")
        _, _, _, post, m = await _act(v, None, lambda *a: {})
        assert post.await_args.args[1] == (
            "Rollout `rid`: reviewer verdict `re-dispatch` (1/1) — why"
        )
        m.assert_not_called()

    async def test_not_drifted_halts(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            _, red, halt, _, _ = await _act(_verdict(), _ws2(), lambda *a: {"a": None})
        halt.assert_awaited_once()
        red.assert_not_awaited()
        assert "reason=not_drifted" in caplog.text

    async def test_budget_exhausted(self, caplog: pytest.LogCaptureFixture) -> None:
        main._rollout_redispatches["rid"] = {"iid": main.MAX_ITEM_REDISPATCHES}
        with caplog.at_level(logging.INFO):
            _, red, halt, _, m = await _act(_verdict(), _ws2(), lambda *a: {})
        halt.assert_awaited_once()
        red.assert_not_awaited()
        m.assert_not_called()
        assert "reason=budget_exhausted" in caplog.text
        assert "after base-drift" not in halt.await_args.args[1]

    async def test_repeat_halt_after_conversion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        main._rollout_drift_redispatched["rid"] = {
            "iid": {"merge_base": "088da08", "behind": 5}
        }
        main._rollout_redispatches["rid"] = {"iid": main.MAX_ITEM_REDISPATCHES}
        with caplog.at_level(logging.WARNING):
            _, _, halt, _, _ = await _act(_verdict(), _ws2(), lambda *a: {})
        assert "base-drift-redispatch-ineffective" in caplog.text
        assert halt.await_args.args[1].endswith(
            "(after base-drift re-dispatch: merge-base 088da08 was 5 behind)"
        )

    async def test_two_repos_one_owner(self) -> None:
        v = _verdict(("b/z.py",))
        _, red, _, post, m = await _act(
            v, _ws2(), lambda *a: {"b": BaseDrift("b", "abcdef0", 2)}
        )
        red.assert_awaited_once()
        assert m.call_args.args[2] == ["b"]
        assert "origin/master" in post.await_args.args[1]

    async def test_two_owners_one_not_drifted(self) -> None:
        v = _verdict(("a/x.py", "b/y.py"))
        _, red, halt, _, _ = await _act(
            v, _ws2(), lambda *a: {"a": BaseDrift("a", "abcdef0", 2), "b": None}
        )
        halt.assert_awaited_once()
        red.assert_not_awaited()

    async def test_unowned_path(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            _, _, halt, _, m = await _act(
                _verdict(("zzz/x.py",)), _ws2(), lambda *a: {}
            )
        halt.assert_awaited_once()
        m.assert_not_called()
        assert "reason=unowned_path" in caplog.text

    async def test_no_judgment_paths(self) -> None:
        v = ReviewVerdict("halt", "gate failed", "", "d")
        _, _, halt, _, m = await _act(v, _ws2(), lambda *a: {})
        halt.assert_awaited_once()
        m.assert_not_called()

    async def test_not_consulted_without_flag(self) -> None:
        _, _, halt, _, m = await _act(_verdict(), _ws2(), lambda *a: {}, drift=False)
        halt.assert_awaited_once()
        m.assert_not_called()
