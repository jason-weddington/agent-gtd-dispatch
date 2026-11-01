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

    def sweep(self) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.shim}:{os.environ['PATH']}",
            "DISPATCH_WORKSPACE_ROOT": str(self.root),
            "HOME": str(self.tmp / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
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
