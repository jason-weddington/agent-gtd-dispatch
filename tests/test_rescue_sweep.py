"""Drive ``scripts/rescue-sweep.sh`` against real bare-origin clones."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, dispatch
from agent_gtd_dispatch.models import PushStatus

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "rescue-sweep.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _remote(origin: Path, branch: str) -> str:
    out = subprocess.run(
        ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return out.split("\t")[0] if out else ""


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


class _World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.origin)],
            check=True,
            capture_output=True,
        )
        seed = tmp / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _commit(seed, "base.txt")
        _git(seed, "remote", "add", "origin", str(self.origin))
        _git(seed, "push", "origin", "main")
        self.seed = seed
        self.root = tmp / "workspace"
        self.root.mkdir()
        shim = tmp / "shim"
        shim.mkdir()
        logger = shim / "logger"
        logger.write_text("#!/bin/sh\nexit 0\n")
        logger.chmod(0o755)
        self.shim = shim

    def clone(self, rel: str, branch: str = "feat/x") -> Path:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", str(self.origin), str(dest)],
            check=True,
            capture_output=True,
        )
        _git(dest, "checkout", "-b", branch)
        return dest

    def foreign(self, *branches: str) -> str:
        other = self.tmp / f"foreign{len(list(self.tmp.iterdir()))}"
        subprocess.run(
            ["git", "clone", str(self.origin), str(other)],
            check=True,
            capture_output=True,
        )
        sha = _commit(other, f"foreign-{other.name}")
        _git(other, "push", "origin", *[f"{sha}:refs/heads/{b}" for b in branches])
        return sha

    def run_tmp(self, key: str) -> Path:
        path = self.tmp / "run-tmp" / f"rt-{key}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sweep(
        self, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.shim}:{os.environ['PATH']}",
            "DISPATCH_WORKSPACE_ROOT": str(self.root),
            "HOME": str(self.tmp / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        env.pop("DISPATCH_RUN_TMP_ROOT", None)
        env.pop("AGENT_GTD_RUN_KEY", None)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


CLONE = "repos-p-run1"


class TestSweepFallback:
    def test_foreign_tip_then_reuse_then_idle(self, world: _World) -> None:
        foreign = world.foreign("feat/x")
        clone = world.clone(CLONE)
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        for needle in (
            "RESCUED",
            "branch=feat/x-r2",
            "fallback_from=feat/x",
            "mode=first-free",
            "fellback=1",
        ):
            assert needle in r.stdout
        assert _remote(world.origin, "feat/x") == foreign
        assert _remote(world.origin, "feat/x-r2") == head

        head2 = _commit(clone, "b.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "mode=reuse" in r.stdout
        assert _remote(world.origin, "feat/x-r2") == head2
        assert _remote(world.origin, "feat/x-r3") == ""

        r = world.sweep()
        assert r.returncode == 0
        assert "RESCUED" not in r.stdout and "FAILED" not in r.stdout
        assert "rescued=0 fellback=0 failed=0" in r.stdout
        assert _git(clone, "rev-parse", "refs/remotes/origin/feat/x-r2") == head2

    def test_foreign_r2_goes_to_r3(self, world: _World) -> None:
        world.foreign("feat/x")
        r2 = world.foreign("feat/x-r2")
        clone = world.clone(CLONE)
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "mode=first-free" in r.stdout
        assert _remote(world.origin, "feat/x-r3") == head
        assert _remote(world.origin, "feat/x-r2") == r2

    def test_absent_branch_plain_push(self, world: _World) -> None:
        clone = world.clone(CLONE)
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0
        assert "fallback_from" not in r.stdout
        assert _remote(world.origin, "feat/x") == head

    def test_unreachable_origin_fails(self, world: _World) -> None:
        clone = world.clone(CLONE)
        _commit(clone, "a.py")
        _git(clone, "remote", "set-url", "origin", str(world.tmp / "missing.git"))
        r = world.sweep()
        assert r.returncode != 0
        assert "FAILED to push" in r.stdout

    def test_workspace_layout(self, world: _World) -> None:
        foreign = world.foreign("feat/x")
        clone = world.clone("ws-run2/repoA")
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "branch=feat/x-r2" in r.stdout
        assert _remote(world.origin, "feat/x") == foreign
        assert _remote(world.origin, "feat/x-r2") == head

    def test_main_is_never_pushed(self, world: _World) -> None:
        clone = world.clone(CLONE, "main-work")
        _git(clone, "checkout", "main")
        _commit(clone, "a.py")
        before = _remote(world.origin, "main")
        r = world.sweep()
        assert r.returncode == 0
        assert _remote(world.origin, "main") == before

    def test_tie_rule(self, world: _World) -> None:
        world.foreign("feat/x")
        clone = world.clone(CLONE)
        _git(clone, "push", "origin", "main:refs/heads/feat/x-r2")
        r2 = _remote(world.origin, "feat/x-r2")
        _commit(clone, "a.py")
        _git(clone, "push", "origin", "HEAD:refs/heads/feat/x-r3")
        head = _commit(clone, "b.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, "feat/x-r3") == head
        assert _remote(world.origin, "feat/x-r2") == r2

    def test_decoys_ignored(self, world: _World) -> None:
        world.foreign("feat/x")
        clone = world.clone(CLONE)
        base = _git(clone, "rev-parse", "main")
        decoys = ["feat/x-rfoo", "feat/x-r02", "feat/x-r51"]
        _git(clone, "push", "origin", *[f"main:refs/heads/{d}" for d in decoys])
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, "feat/x-r2") == head
        for d in decoys:
            assert _remote(world.origin, d) == base

    def test_no_free_suffix(self, world: _World) -> None:
        world.foreign("feat/x", *[f"feat/x-r{n}" for n in range(2, 51)])
        clone = world.clone(CLONE)
        _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode != 0
        assert "fallback=none reason=no-free-suffix" in r.stdout

    def test_hook_rejection_does_not_fall_back(self, world: _World) -> None:
        clone = world.clone(CLONE)
        _git(clone, "push", "origin", "main:refs/heads/feat/x")
        _commit(clone, "a.py")
        hook = world.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        r = world.sweep()
        assert r.returncode != 0
        assert "FAILED to push" in r.stdout
        assert "fallback" not in r.stdout
        assert _remote(world.origin, "feat/x-r2") == ""

    def test_sweep_then_worker_agree(self, world: _World, tmp_path: Path) -> None:
        world.foreign("feat/x")
        clone = world.clone(CLONE)
        base = _git(clone, "rev-parse", "main")
        _commit(clone, "a.py")
        assert world.sweep().returncode == 0
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "k",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "AGENT_SUBPROCESS_USER": "",
        }
        with patch.dict(os.environ, env):
            config.load()
            attempt = dispatch.push_unpushed_repo(clone, "feat/x", 60.0)
            assert (attempt.branch, attempt.fell_back) == ("feat/x-r2", True)
            results = dispatch.verify_pushes(
                [("r", clone, base)], "feat/x", {"r": "feat/x-r2"}
            )
        assert results[0].status == PushStatus.pushed
        assert _remote(world.origin, "feat/x-r3") == ""


K1 = "0123456789ab"
K2 = "abcdef012345"
K3 = "fedcba987654"
NOTE = "mid-run snapshot, not the run's result"


def _rescue_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DISPATCH_API_KEY": "k",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "k",
        "ANTHROPIC_API_KEY": "k",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
    }


class TestLiveSnapshots:
    def _live(self, world: _World, key: str) -> tuple[Path, str]:
        clone = world.clone(f"repos-p-{key}")
        world.run_tmp(key)
        return clone, _commit(clone, "a.py")

    def test_live_via_rt_dir(self, world: _World) -> None:
        clone, head = self._live(world, K1)
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head
        assert _remote(world.origin, "feat/x") == ""
        assert _remote(world.origin, "feat/x-r2") == ""
        for needle in (
            "SNAPSHOT",
            f"branch=feat/x-rescue-{K1}",
            "mode=direct",
            NOTE,
            "liveness=live",
            "live_by=rt",
            "snapshots=1",
        ):
            assert needle in r.stdout
        assert (
            _git(clone, "for-each-ref", f"refs/remotes/origin/feat/x-rescue-{K1}") == ""
        )

    @pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="needs /proc")
    def test_live_via_marker_process(self, world: _World) -> None:
        clone = world.clone(f"repos-p-{K2}")
        head = _commit(clone, "a.py")
        proc = subprocess.Popen(
            ["sleep", "60"], env={**os.environ, "AGENT_GTD_RUN_KEY": f"rt-{K2}"}
        )
        try:
            r = world.sweep()
        finally:
            proc.kill()
            proc.wait()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K2}") == head
        assert "live_by=proc" in r.stdout and "snapshots=1" in r.stdout
        assert _remote(world.origin, "feat/x") == ""

    def test_incident_shape(self, world: _World) -> None:
        foreign = world.foreign("feat/x")
        _, head = self._live(world, K1)
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head
        assert _remote(world.origin, "feat/x") == foreign
        assert _remote(world.origin, "feat/x-r2") == ""

    def test_idempotent_then_fast_forward(self, world: _World) -> None:
        clone, _ = self._live(world, K1)
        assert world.sweep().returncode == 0
        r = world.sweep()
        assert "SNAPSHOT" not in r.stdout
        assert "snapshot-current:" in r.stdout
        assert "snapshot_current=1" in r.stdout and "snapshots=0" in r.stdout
        head2 = _commit(clone, "b.py")
        r = world.sweep()
        assert "mode=direct" in r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head2

    def test_rewrite_goes_to_suffix(self, world: _World) -> None:
        clone, head = self._live(world, K1)
        assert world.sweep().returncode == 0
        _git(clone, "reset", "--hard", "main")
        head2 = _commit(clone, "c.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "mode=first-free" in r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}-2") == head2
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head

    def test_live_then_abandoned(self, world: _World) -> None:
        _, head = self._live(world, K1)
        assert world.sweep().returncode == 0
        (world.tmp / "run-tmp" / f"rt-{K1}").rmdir()
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "RESCUED" in r.stdout and "liveness=abandoned" in r.stdout
        assert _remote(world.origin, "feat/x") == head
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head

    def test_valid_key_not_live(self, world: _World) -> None:
        foreign = world.foreign("feat/x")
        clone = world.clone(f"repos-p-{K3}")
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert "liveness=abandoned live_by=none" in r.stdout
        assert _remote(world.origin, "feat/x") == foreign
        assert _remote(world.origin, "feat/x-r2") == head

    def test_workspace_layout_live(self, world: _World) -> None:
        clone = world.clone(f"ws-{K1}/repoA")
        world.run_tmp(K1)
        head = _commit(clone, "a.py")
        r = world.sweep()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head

    def test_sweep_then_worker(self, world: _World, tmp_path: Path) -> None:
        world.foreign("feat/x")
        clone, _ = self._live(world, K1)
        assert world.sweep().returncode == 0
        snap = _remote(world.origin, f"feat/x-rescue-{K1}")
        with patch.dict(os.environ, _rescue_env(tmp_path)):
            config.load()
            attempt = dispatch.push_unpushed_repo(clone, "feat/x", 60.0)
        assert (attempt.branch, attempt.fell_back) == ("feat/x-r2", True)
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == snap

    def test_canonical_suffix_ignores_rescue(self) -> None:
        for ref in (
            f"refs/heads/feat/x-rescue-{K1}",
            f"refs/heads/feat/x-rescue-{K1}-2",
        ):
            assert dispatch._canonical_suffix("feat/x", ref) is None

    def test_slug_decoy_not_excluded(self, world: _World) -> None:
        clone = world.clone(CLONE, "feat/y")
        _commit(clone, "a.py")
        _git(clone, "push", "origin", "HEAD:refs/heads/feat/y-rescue-sweep")
        r = world.sweep()
        # a non-hex slug is not a snapshot ref, so it hides the commit
        assert "unpushed:" not in r.stdout
        assert _remote(world.origin, "feat/y") == ""

    def test_hex_snapshot_ref_still_counts(self, world: _World) -> None:
        clone = world.clone(CLONE, "feat/y")
        head = _commit(clone, "a.py")
        _git(clone, "push", "origin", f"HEAD:refs/heads/feat/y-rescue-{K1}")
        r = world.sweep()
        assert "unpushed:" in r.stdout and "RESCUED" in r.stdout
        assert _remote(world.origin, "feat/y") == head

    def test_run_tmp_root_override(self, world: _World) -> None:
        clone = world.clone(f"repos-p-{K1}")
        head = _commit(clone, "a.py")
        custom = world.tmp / "elsewhere"
        (custom / f"rt-{K1}").mkdir(parents=True)
        r = world.sweep({"DISPATCH_RUN_TMP_ROOT": str(custom)})
        assert r.returncode == 0, r.stdout
        assert "live_by=rt" in r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head

    def test_worker_teardown_after_snapshot(
        self, world: _World, tmp_path: Path
    ) -> None:
        clone, head = self._live(world, K1)
        assert world.sweep().returncode == 0
        _git(clone, "fetch", "origin")
        with patch.dict(os.environ, _rescue_env(tmp_path)):
            config.load()
            assert dispatch._repo_has_unrescued_work(clone, "feat/x") == (True, False)
            res = dispatch.rescue_abandoned_work("r", clone, "feat/x", K1)
        assert res.pushed is True
        assert _remote(world.origin, "feat/x") == head

    def test_degraded(self, world: _World) -> None:
        world.clone(CLONE)
        r = world.sweep({"DISPATCH_RUN_TMP_ROOT": str(world.tmp / "nope")})
        assert r.returncode == 0
        assert "WARN run_tmp_root missing" in r.stdout and "degraded=1" in r.stdout


class TestMarkerLivenessFailClosed:
    """Off procfs the sweep cannot prove a run dead, so it only snapshots (AC10)."""

    @staticmethod
    def _no_proc(world: _World) -> dict[str, str]:
        return {"DISPATCH_PROC_ROOT": str(world.tmp / "no-proc")}

    def test_live_run_without_rt_dir_is_snapshot_only(self, world: _World) -> None:
        clone = world.clone(f"repos-p-{K2}")
        head = _commit(clone, "a.py")
        proc = subprocess.Popen(
            ["sleep", "60"], env={**os.environ, "AGENT_GTD_RUN_KEY": f"rt-{K2}"}
        )
        try:
            r = world.sweep(self._no_proc(world))
        finally:
            proc.kill()
            proc.wait()
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K2}") == head
        assert _remote(world.origin, "feat/x") == ""
        assert _remote(world.origin, "feat/x-r2") == ""
        for needle in (
            "live_by=unverifiable",
            "config: marker_liveness=unavailable",
            "WARN marker liveness unavailable on this platform",
            "marker_liveness=unavailable unverifiable=1",
            "degraded=1",
        ):
            assert needle in r.stdout

    def test_foreign_tip_never_pushes_r2(self, world: _World) -> None:
        foreign = world.foreign("feat/x")
        clone = world.clone(f"repos-p-{K3}")
        head = _commit(clone, "a.py")
        r = world.sweep(self._no_proc(world))
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, "feat/x") == foreign
        assert _remote(world.origin, "feat/x-r2") == ""
        assert _remote(world.origin, f"feat/x-rescue-{K3}") == head

    def test_abandoned_clone_still_only_snapshotted(self, world: _World) -> None:
        clone = world.clone(f"repos-p-{K1}")
        head = _commit(clone, "a.py")
        r = world.sweep(self._no_proc(world))
        assert r.returncode == 0, r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head
        assert _remote(world.origin, "feat/x") == ""
        assert _remote(world.origin, "feat/x-r2") == ""
        assert "RESCUED" not in r.stdout

    def test_non_hex_key_keeps_unknown_liveness(self, world: _World) -> None:
        clone = world.clone(CLONE)
        head = _commit(clone, "a.py")
        r = world.sweep(self._no_proc(world))
        assert r.returncode == 0, r.stdout
        assert "liveness=unknown" in r.stdout and "unverifiable=0" in r.stdout
        assert _remote(world.origin, "feat/x") == head

    @pytest.mark.parametrize("no_proc", [False, True])
    def test_rt_dir_present_behaves_identically(
        self, world: _World, no_proc: bool
    ) -> None:
        world.clone(f"repos-p-{K1}")
        clone = world.root / f"repos-p-{K1}"
        world.run_tmp(K1)
        head = _commit(clone, "a.py")
        r = world.sweep(self._no_proc(world) if no_proc else None)
        assert r.returncode == 0, r.stdout
        assert "live_by=rt" in r.stdout and "unverifiable=0" in r.stdout
        assert _remote(world.origin, f"feat/x-rescue-{K1}") == head
        assert _remote(world.origin, "feat/x") == ""

    @pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="needs /proc")
    def test_default_proc_root_reports_available(self, world: _World) -> None:
        world.clone(CLONE)
        r = world.sweep()
        assert r.returncode == 0
        assert "config: marker_liveness=available" in r.stdout
        assert "marker_liveness=available unverifiable=0" in r.stdout
        assert "WARN marker liveness" not in r.stdout
