"""Tests for scripts/sudoers-refresh.sh and its wiring into deploy.sh and
setup-dispatch-host.sh. Hermetic: a stub visudo, tmp_path sudoers files, no root.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "sudoers-refresh.sh"
TEMPLATE = ROOT / "templates" / "sudoers-dispatch-svc.tmpl"
DEPLOY = ROOT / "deploy.sh"
SETUP = ROOT / "setup-dispatch-host.sh"

USER = getpass.getuser()
BAD_USER = "nonexistent-sudoers-test-user"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_VISUDO_LOG"
if [ "$1" = "-c" ] && [ "${2:-}" = "-f" ]; then
    echo FAKE_VISUDO_PARSE_ERROR
    exit "${FAKE_VISUDO_F_RC:-0}"
fi
rc="${FAKE_VISUDO_FULL_RC:-0}"
[ "$rc" != 0 ] && echo FAKE_VISUDO_PARSE_ERROR
exit "$rc"
"""


class Env:
    def __init__(self, tmp: Path) -> None:
        self.dir = tmp / "sudoers.d"
        self.dir.mkdir()
        self.live = self.dir / "dispatch-svc"
        self.log = tmp / "visudo.log"
        self.log.write_text("")
        self.stub = tmp / "visudo-stub"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)

    def run(
        self, *extra: str, env: dict[str, str] | None = None, users: str = USER
    ) -> subprocess.CompletedProcess[str]:
        e = {**os.environ, "FAKE_VISUDO_LOG": str(self.log), **(env or {})}
        return subprocess.run(
            [
                str(SCRIPT),
                "--template", str(TEMPLATE),
                "--sudoers-file", str(self.live),
                "--service-user", users,
                "--agent-user", users,
                "--visudo", str(self.stub),
                "--owner", USER,
                "--group", subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip(),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=e,
        )  # fmt: skip

    def render(self) -> str:
        return subprocess.run(
            [str(SCRIPT), "--render-only", "--template", str(TEMPLATE),
             "--service-user", USER, "--agent-user", USER],
            capture_output=True, text=True,
        ).stdout  # fmt: skip

    def visudo_lines(self) -> list[str]:
        return self.log.read_text().splitlines()

    def no_tmp(self) -> bool:
        return not list(self.dir.glob("*.tmp.*"))

    def baks(self) -> list[Path]:
        return list(self.dir.glob("*.bak.*"))


@pytest.fixture
def e(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_unchanged(e: Env) -> None:
    e.live.write_text(e.render())
    st = e.live.stat()
    r = e.run()
    assert r.returncode == 0
    assert re.search(r"\[OK\].*sha256 [0-9a-f]{12}\)", r.stdout)
    st2 = e.live.stat()
    assert (st.st_ino, st.st_mtime_ns) == (st2.st_ino, st2.st_mtime_ns)
    assert not e.baks()
    assert e.visudo_lines() == []
    assert e.no_tmp()


def test_changed(e: Env) -> None:
    e.live.write_text("# old content\n")
    r = e.run()
    assert r.returncode == 0
    assert "[CHANGED]" in r.stdout
    assert e.live.read_text() == e.render()
    assert stat.S_IMODE(e.live.stat().st_mode) == 0o440
    baks = e.baks()
    assert len(baks) == 1
    assert re.search(r"dispatch-svc\.bak\.\d{8}T\d{6}(\.\d+)?$", baks[0].name)
    assert baks[0].read_text() == "# old content\n"
    lines = e.visudo_lines()
    assert len(lines) == 2
    assert "-c -f" in lines[0] and re.search(r"dispatch-svc\.tmp\.", lines[0])
    assert lines[1] == "-c"
    assert e.no_tmp()
    # b2: diff lines
    assert "[DIFF] +" in r.stdout and "[DIFF] -# old content" in r.stdout
    # b3: sha
    sha = hashlib.sha256(e.render().encode()).hexdigest()[:12]
    assert f"sha256 {sha}" in r.stdout


def test_sha_in_ok_matches_render(e: Env) -> None:
    e.live.write_text(e.render())
    sha = hashlib.sha256(e.render().encode()).hexdigest()[:12]
    assert f"sha256 {sha}" in e.run().stdout


def test_visudo_f_rejects(e: Env) -> None:
    e.live.write_text("# old\n")
    r = e.run(env={"FAKE_VISUDO_F_RC": "1"})
    assert r.returncode == 1
    assert "[ERR]" in r.stderr and "FAKE_VISUDO_PARSE_ERROR" in r.stderr
    assert e.live.read_text() == "# old\n"
    assert not e.baks()
    assert e.no_tmp()


def test_post_install_rollback(e: Env) -> None:
    e.live.write_text("# old\n")
    e.live.chmod(0o440)
    r = e.run(env={"FAKE_VISUDO_FULL_RC": "1"})
    assert r.returncode == 1
    assert "FAKE_VISUDO_PARSE_ERROR" in r.stderr and "restored from" in r.stderr
    assert e.live.read_text() == "# old\n"
    assert stat.S_IMODE(e.live.stat().st_mode) == 0o440
    assert not e.baks()
    assert e.no_tmp()


def test_create_rollback_removes(e: Env) -> None:
    r = e.run("--create", env={"FAKE_VISUDO_FULL_RC": "1"})
    assert r.returncode == 1
    assert not e.live.exists()
    assert e.no_tmp()


def test_absent_skip_no_warn(e: Env) -> None:
    r = e.run(users=BAD_USER)
    assert r.returncode == 0
    assert "[SKIP]" in r.stdout and "[WARN]" not in r.stderr
    assert not e.live.exists()
    assert e.visudo_lines() == []


def test_absent_skip_warn(e: Env) -> None:
    r = e.run()
    assert r.returncode == 0
    assert "[SKIP]" in r.stdout and "[WARN]" in r.stderr


def test_create(e: Env) -> None:
    r = e.run("--create")
    assert r.returncode == 0
    assert e.live.read_text() == e.render()
    assert stat.S_IMODE(e.live.stat().st_mode) == 0o440
    assert "backup: none" in r.stdout
    assert e.no_tmp()


def test_render_only() -> None:
    r = subprocess.run(
        [str(SCRIPT), "--render-only", "--template", str(TEMPLATE),
         "--service-user", "svc-x", "--agent-user", "agent-y"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0
    assert "{{" not in r.stdout
    assert "svc-x" in r.stdout and "agent-y" in r.stdout


def test_usage_and_template_errors(tmp_path: Path) -> None:
    r = subprocess.run(
        [str(SCRIPT), "--template", str(tmp_path / "nope")], capture_output=True
    )
    assert r.returncode == 3
    assert subprocess.run([str(SCRIPT), "--bogus"], capture_output=True).returncode == 2
    r2 = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert r2.returncode == 2 and "Usage" in r2.stderr


def test_missing_agent_user(e: Env) -> None:
    e.live.write_text("# old\n")
    r = e.run("--agent-user", BAD_USER)
    assert r.returncode == 4
    assert e.live.read_text() == "# old\n"
    assert e.visudo_lines() == []


def test_script_syntax_and_executable() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    entry = subprocess.run(
        ["git", "ls-files", "-s", "--", str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if not entry.stdout:
        assert os.stat(SCRIPT).st_mode & 0o111
    else:
        assert entry.stdout.startswith("100755 ")


def test_deploy_static() -> None:
    assert subprocess.run(["bash", "-n", str(DEPLOY)]).returncode == 0
    d = DEPLOY.read_text()
    for needle in (
        'ssh "${host}" mktemp -d',
        "scripts/sudoers-refresh.sh",
        "templates/sudoers-dispatch-svc.tmpl",
        "${host}:${_stage}/",
        "could not stage sudoers refresh on",
    ):
        assert needle in d
    h = d.index("bash -s <<EOF")
    m = re.compile(r"sudo bash \S*sudoers-refresh\.sh").search(d, h)
    assert m
    r = m.start()
    c = d.index('rm -rf "${_stage}"', r)
    u = d.index("uv tool install --force --refresh agent-gtd-dispatch")
    assert h < r < c < u
    line = d[d.rindex("\n", 0, r) + 1 : d.index("\n", r)]
    assert "--create" not in line


def test_setup_static() -> None:
    s = SETUP.read_text()
    assert "scripts/sudoers-refresh.sh" in s
    for gone in (
        "_render_sudoers",
        "_install_sudoers",
        "install -m 0440 -o root -g root",
    ):
        assert gone not in s
