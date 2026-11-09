"""Tests for scripts/kb-mcp-refresh.sh and its wiring into deploy.sh and
setup-dispatch-host.sh. Hermetic: a fake argv-recording `claude`, a tmp agent
home, no root.

The script registers tightbeam_kb and agent-gtd as hosted HTTP /mcp servers and
unconditionally retires the legacy personal-kb/team-kb servers.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "kb-mcp-refresh.sh"
DEPLOY = ROOT / "deploy.sh"
PROBE = ROOT / "templates" / "mcp-probe.py"
SETUP = ROOT / "setup-dispatch-host.sh"
ENV_TMPL = ROOT / "templates" / "dispatch-env.tmpl"

USER = pwd.getpwuid(os.getuid()).pw_name
GROUP = grp.getgrgid(os.getgid()).gr_name

TBK = "tbk-SECRET-4"
GK = "gk-SECRET-3"

FAKE_CLAUDE = """#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_CLAUDE_LOG"
exit "${FAKE_CLAUDE_RC:-0}"
"""

# add-json variant: stores the entry in .claude.json, optionally mutated.
FAKE_CLAUDE_STORING = """#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_CLAUDE_LOG"
if [ "$2" = "add-json" ]; then
python3 - "$3" "$4" <<'PY'
import json, os, sys
name, raw = sys.argv[1], sys.argv[2]
entry = json.loads(raw)
mode = os.environ["FAKE_MODE"]
if mode == "leak":
    entry["env"] = {"K": os.environ["FAKE_LEAK"]}
elif mode == "extra":
    entry["extra"] = "x"
p = os.path.join(os.environ["HOME"], ".claude.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("mcpServers", {})[name] = entry
json.dump(d, open(p, "w"))
PY
fi
exit 0
"""

# Stateful variant: remove pops the entry, add-json stores it, and add-json of an
# http entry fails (without storing) when FAKE_FAIL_HTTP=1.
FAKE_CLAUDE_STATEFUL = """#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_CLAUDE_LOG"
python3 - "$@" <<'PY'
import json, os, sys
args = sys.argv[1:]
p = os.path.join(os.environ["HOME"], ".claude.json")
d = json.load(open(p)) if os.path.exists(p) else {}
s = d.setdefault("mcpServers", {})
if args[:2] == ["mcp", "remove"]:
    s.pop(args[2], None)
elif args[:2] == ["mcp", "add-json"]:
    if os.environ.get("FAKE_FAIL_HTTP") == "1" and '"type": "http"' in args[3]:
        sys.exit(1)
    s[args[2]] = json.loads(args[3])
json.dump(d, open(p, "w"))
PY
"""


TIGHTBEAM_HELPER = (
    'printf \'{"Authorization": "Bearer %s"}\' '
    '"${TIGHTBEAM_KB_MCP_TOKEN:-$(cat ~/.tightbeam_kb_key 2>/dev/null)}"'
)

D_TIGHTBEAM = {
    "type": "http",
    "url": "https://kb.example/mcp",
    "headersHelper": TIGHTBEAM_HELPER,
}

D_GTD = {
    "type": "http",
    "url": "https://gtd.example/mcp",
    "headersHelper": 'if [ -n "${AGENT_GTD_AGENT_NAME:-}" ]; then printf \'{"Authorization": "Bearer %s", "X-Agent-Name": "%s"}\' "${AGENT_GTD_API_KEY:-}" "$AGENT_GTD_AGENT_NAME"; else printf \'{"Authorization": "Bearer %s"}\' "${AGENT_GTD_API_KEY:-}"; fi',
}
LEGACY_GTD = {
    "type": "stdio",
    "command": "uvx",
    "args": ["--from", "x", "agent-gtd-mcp"],
    "env": {"AGENT_GTD_URL": "https://gtd.example"},
}


class Env:
    def __init__(self, tmp: Path, claude_body: str = FAKE_CLAUDE) -> None:
        self.home = tmp / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        self.claude = self.home / ".local" / "bin" / "claude"
        self.claude.write_text(claude_body)
        self.claude.chmod(0o755)
        self.log = tmp / "claude.log"
        self.log.write_text("")
        self.envfile = tmp / "service.env"
        self.cj = self.home / ".claude.json"

    def write_env(self, text: str) -> None:
        self.envfile.write_text(text)

    def seed(self, servers: dict[str, object]) -> None:
        self.cj.write_text(json.dumps({"mcpServers": servers}))

    def key_file(self, name: str, content: str, mode: int = 0o600) -> Path:
        p = self.home / name
        p.write_text(content)
        p.chmod(mode)
        return p

    def run(
        self,
        *args: str,
        extra_env: dict[str, str] | None = None,
        use_default: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        e = {
            "PATH": f"{self.home / '.local' / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_CLAUDE_LOG": str(self.log),
            **(extra_env or {}),
        }
        base = [str(SCRIPT)]
        if use_default:
            base += [
                "--env-file", str(self.envfile),
                "--agent-user", USER,
                "--agent-home", str(self.home),
                "--agent-group", GROUP,
                "--claude", str(self.claude),
            ]  # fmt: skip
        return subprocess.run([*base, *args], capture_output=True, text=True, env=e)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def tightbeam_env(e: Env, url: str = "https://kb.example", token: str = TBK) -> None:
    text = f"TIGHTBEAM_KB_MCP_TOKEN={token}\n"
    if url is not None:
        text = f"TIGHTBEAM_KB_MCP_URL={url}\n" + text
    e.write_env(text)


def gtd_env(e: Env, url: str = "https://gtd.example", key: str | None = None) -> None:
    text = f"AGENT_GTD_URL={url}\n"
    if key is not None:
        text += f"AGENT_GTD_API_KEY={key}\n"
    e.write_env(text)


def all_env(e: Env) -> None:
    e.write_env(
        f"TIGHTBEAM_KB_MCP_URL=https://kb.example\n"
        f"TIGHTBEAM_KB_MCP_TOKEN={TBK}\n"
        f"AGENT_GTD_URL=https://gtd.example\nAGENT_GTD_API_KEY={GK}\n"
    )


# --- tightbeam_kb registration -------------------------------------------


def test_registration_exact_calls(env: Env) -> None:
    """AC-1: no pre-existing entry -> remove then add-json with the exact helper."""
    tightbeam_env(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.calls() == [
        ["mcp", "remove", "tightbeam_kb", "--scope", "user"],
        [
            "mcp",
            "add-json",
            "tightbeam_kb",
            json.dumps(D_TIGHTBEAM),
            "--scope",
            "user",
        ],
    ]
    assert "[CHANGED] tightbeam_kb registered as http https://kb.example/mcp" in (
        r.stdout
    )


def test_key_file_perms(env: Env) -> None:
    tightbeam_env(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    kf = env.home / ".tightbeam_kb_key"
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert kf.read_text() == TBK
    assert "reason: absent" in r.stdout


def test_key_file_mode_repair_without_claude_call(env: Env) -> None:
    tightbeam_env(env)
    env.seed({"tightbeam_kb": D_TIGHTBEAM})
    kf = env.key_file(".tightbeam_kb_key", TBK, 0o644)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert f"[CHANGED] {kf} written (mode 0600, reason: mode-644)" in r.stdout
    assert "registration up to date" in r.stdout
    assert env.calls() == []


def test_key_file_content_changed(env: Env) -> None:
    tightbeam_env(env)
    env.seed({"tightbeam_kb": D_TIGHTBEAM})
    kf = env.key_file(".tightbeam_kb_key", "stale\n")
    r = env.run()
    assert f"[CHANGED] {kf} written (mode 0600, reason: content-changed)" in r.stdout
    assert kf.read_text() == TBK
    assert env.calls() == []


def test_unchanged(env: Env) -> None:
    """Second run is a no-op: key file inode/mtime unchanged, no claude call."""
    tightbeam_env(env)
    env.seed({"tightbeam_kb": D_TIGHTBEAM})
    first = env.run()
    assert first.returncode == 0, first.stderr
    kf = env.home / ".tightbeam_kb_key"
    before = (kf.stat().st_ino, kf.stat().st_mtime_ns)
    calls_before = len(env.calls())

    second = env.run()
    assert second.returncode == 0, second.stderr
    assert f"[OK]   {kf} up to date" in second.stdout
    assert "[OK]   tightbeam_kb registration up to date (https://kb.example/mcp)" in (
        second.stdout
    )
    assert len(env.calls()) == calls_before
    assert (kf.stat().st_ino, kf.stat().st_mtime_ns) == before
    assert second.stdout.splitlines()[-1] == (
        "kb-mcp-refresh: tightbeam_kb=ok agent-gtd=skipped agent-gtd-probe=skipped rc=0"
    )


def test_tightbeam_and_gtd_unchanged(env: Env) -> None:
    all_env(env)
    env.seed({"tightbeam_kb": D_TIGHTBEAM, "agent-gtd": D_GTD})
    env.key_file(".tightbeam_kb_key", TBK)
    before = env.cj.read_bytes()
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (
        "[OK]   agent-gtd registration up to date (https://gtd.example/mcp)" in r.stdout
    )
    assert r.stdout.count("registration up to date") == 2
    assert env.calls() == []
    assert env.cj.read_bytes() == before
    assert r.stdout.splitlines()[-1] == (
        "kb-mcp-refresh: tightbeam_kb=ok agent-gtd=ok agent-gtd-probe=skipped rc=0"
    )


def test_missing_key_skip_removes_stale(env: Env) -> None:
    env.write_env("TIGHTBEAM_KB_MCP_URL=https://kb.example\n")
    env.seed({"tightbeam_kb": D_TIGHTBEAM})
    kf = env.key_file(".tightbeam_kb_key", "old")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (
        "[SKIP] tightbeam_kb: missing TIGHTBEAM_KB_MCP_TOKEN — not registered"
        in r.stdout
    )
    assert "[CHANGED] removed stale tightbeam_kb registration" in r.stdout
    assert f"[CHANGED] removed {kf}" in r.stdout
    assert (
        "[WARN] tightbeam_kb: registration removed — agents on this host have no "
        "knowledge base until TIGHTBEAM_KB_MCP_TOKEN is provisioned" in r.stderr
    )
    assert not kf.exists()
    assert env.calls() == [["mcp", "remove", "tightbeam_kb", "--scope", "user"]]


def test_missing_key_no_entry_no_call(env: Env) -> None:
    env.write_env("TIGHTBEAM_KB_MCP_URL=https://kb.example\n")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "[SKIP] tightbeam_kb:" in r.stdout
    assert f"[OK]   {env.home / '.tightbeam_kb_key'} already absent" in r.stdout
    assert env.calls() == []


@pytest.mark.parametrize(
    "text", ["", f"TIGHTBEAM_KB_MCP_URL=\nTIGHTBEAM_KB_MCP_TOKEN={TBK}\n"]
)
def test_missing_url_skip_removes_stale(env: Env, text: str) -> None:
    """AC-4: the URL has no code default; an unset/empty URL means no registration."""
    env.write_env(text)
    if not text:
        env.write_env(f"TIGHTBEAM_KB_MCP_TOKEN={TBK}\n")
    env.seed({"tightbeam_kb": D_TIGHTBEAM})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (
        "[SKIP] tightbeam_kb: missing TIGHTBEAM_KB_MCP_URL — not registered" in r.stdout
    )
    assert "[CHANGED] removed stale tightbeam_kb registration" in r.stdout
    assert not (env.home / ".tightbeam_kb_key").exists()
    assert env.calls() == [["mcp", "remove", "tightbeam_kb", "--scope", "user"]]


def test_both_missing_names_both(env: Env) -> None:
    env.write_env("")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (
        "[SKIP] tightbeam_kb: missing TIGHTBEAM_KB_MCP_URL TIGHTBEAM_KB_MCP_TOKEN "
        "— not registered" in r.stdout
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://kb.example",
        "https://kb.example/",
        "https://kb.example/mcp",
        "https://kb.example/mcp/",
    ],
)
def test_url_normalization(env: Env, url: str) -> None:
    tightbeam_env(env, url)
    r = env.run()
    assert r.returncode == 0, r.stderr
    calls = env.calls()
    assert [c[1] for c in calls] == ["remove", "add-json"]
    assert json.loads(calls[1][3])["url"] == "https://kb.example/mcp"
    assert "mcp/mcp" not in calls[1][3]


def test_no_key_material_leaks(env: Env) -> None:
    tightbeam_env(env, token=TBK)
    r = env.run()
    assert r.returncode == 0, r.stderr
    for text in (env.log.read_text(), r.stdout, r.stderr):
        assert TBK not in text
    found = sorted(
        str(p.relative_to(env.home))
        for p in env.home.rglob("*")
        if p.is_file() and TBK in p.read_text(errors="ignore")
    )
    assert found == [".tightbeam_kb_key"]


def test_leak_tripwire(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    tightbeam_env(e, token=TBK)
    r = e.run(extra_env={"FAKE_MODE": "leak", "FAKE_LEAK": TBK})
    assert r.returncode == 1
    assert (
        "[ERR]  tightbeam_kb: API key found in .claude.json — secret leaked into "
        "registration" in r.stderr
    )


def test_readback_drift(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    tightbeam_env(e, token=TBK)
    r = e.run(extra_env={"FAKE_MODE": "extra"})
    assert r.returncode == 0, r.stderr
    assert "[WARN] tightbeam_kb readback differs" in r.stdout
    assert "extra" in r.stdout


def test_env_quoting_last_wins(env: Env) -> None:
    env.write_env(
        f'TIGHTBEAM_KB_MCP_URL="https://first.example"\n'
        f'TIGHTBEAM_KB_MCP_URL="https://kb.example"\n'
        f"TIGHTBEAM_KB_MCP_TOKEN='{TBK}'\n"
    )
    r = env.run()
    assert r.returncode == 0, r.stderr
    adds = [c for c in env.calls() if c[1] == "add-json"]
    assert json.loads(adds[-1][3])["url"] == "https://kb.example/mcp"
    assert (env.home / ".tightbeam_kb_key").read_text() == TBK


def test_ambient_env_ignored(env: Env) -> None:
    env.write_env("TIGHTBEAM_KB_MCP_URL=https://kb.example\n")
    r = env.run(extra_env={"TIGHTBEAM_KB_MCP_TOKEN": "ambient-should-not-be-used"})
    assert "[SKIP] tightbeam_kb:" in r.stdout
    assert env.calls() == []


def test_unparseable_claude_json(env: Env) -> None:
    tightbeam_env(env)
    env.cj.write_text("{not json")
    r = env.run()
    assert r.returncode == 1
    assert "[ERR]  cannot parse" in r.stderr
    assert env.calls() == []
    assert not (env.home / ".tightbeam_kb_key").exists()


# --- personal-kb / team-kb retirement ------------------------------------


def _seed_retired(e: Env) -> tuple[Path, Path]:
    e.seed(
        {
            "personal-kb": {"type": "http", "url": "https://old.example/mcp"},
            "team-kb": {"type": "http", "url": "https://team.example/mcp"},
        }
    )
    return (
        e.key_file(".personal_kb_hook_key", "old-personal"),
        e.key_file(".team_kb_key", "old-team"),
    )


@pytest.mark.parametrize(
    "text",
    [
        "PERSONAL_KB_URL=https://kb.example\nPERSONAL_KB_API_KEY=pk\n"
        "TEAM_KB_URL=https://team.example\nTEAM_KB_API_KEY=tk\n",
        "",
    ],
)
def test_retirement_removes_registrations_and_key_files(env: Env, text: str) -> None:
    env.write_env(text)
    pf, tf = _seed_retired(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "[CHANGED] removed retired personal-kb registration" in r.stdout
    assert "[CHANGED] removed retired team-kb registration" in r.stdout
    assert f"[CHANGED] removed {pf}" in r.stdout
    assert f"[CHANGED] removed {tf}" in r.stdout
    assert not pf.exists() and not tf.exists()
    assert env.calls() == [
        ["mcp", "remove", "personal-kb", "--scope", "user"],
        ["mcp", "remove", "team-kb", "--scope", "user"],
    ]
    assert not any(c[1] == "add-json" for c in env.calls())


def test_retirement_with_legacy_kb_vars_present(env: Env) -> None:
    env.write_env(
        "PERSONAL_KB_URL=https://kb.example\nPERSONAL_KB_API_KEY=pk\n"
        "TEAM_KB_URL=https://team.example\nTEAM_KB_API_KEY=tk\n"
    )
    r = env.run()
    assert r.returncode == 0, r.stderr
    # Legacy vars are never read: no registration is attempted for them.
    assert "[SKIP] personal-kb:" not in r.stdout
    assert "[SKIP] team-kb:" not in r.stdout
    assert env.calls() == []


def test_retirement_absent_is_noop(env: Env) -> None:
    env.write_env("")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "[OK]   personal-kb registration already absent" in r.stdout
    assert "[OK]   team-kb registration already absent" in r.stdout
    assert f"[OK]   {env.home / '.personal_kb_hook_key'} already absent" in r.stdout
    assert f"[OK]   {env.home / '.team_kb_key'} already absent" in r.stdout
    assert env.calls() == []


def test_retirement_remove_failure(env: Env) -> None:
    env.write_env("")
    env.seed({"personal-kb": {"type": "http", "url": "https://old.example/mcp"}})
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert "[ERR]  claude mcp remove personal-kb failed (rc=1)" in r.stderr
    assert r.stdout.splitlines()[-1].endswith("rc=1")


# --- CLI / static guards --------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [([], 2), (["--bogus"], 2)],
)
def test_usage_errors(env: Env, args: list[str], code: int) -> None:
    r = env.run(*args, use_default=False)
    assert r.returncode == code
    assert env.calls() == []


def test_missing_env_file(env: Env) -> None:
    r = env.run("--env-file", str(env.home / "nope"), use_default=False)
    assert r.returncode == 3
    assert env.calls() == []


def test_unknown_agent_user(env: Env) -> None:
    tightbeam_env(env)
    r = env.run(
        "--env-file",
        str(env.envfile),
        "--agent-user",
        "no-such-user-xyz",
        use_default=False,
    )
    assert r.returncode == 4
    assert env.calls() == []


def test_claude_not_executable(env: Env) -> None:
    tightbeam_env(env)
    env.claude.chmod(0o644)
    r = env.run()
    assert r.returncode == 5
    assert env.calls() == []
    assert not (env.home / ".tightbeam_kb_key").exists()


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


def test_script_static() -> None:
    s = SCRIPT.read_text()
    assert 'sudo -n -u "$AGENT_USER" -H --' in s
    for bad in (
        "bash -c",
        "bash -lc",
        "runuser -l",
    ):
        assert bad not in s
    code = "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith("#"))
    assert "aws-documentation-mcp-server" not in code
    for tok in ("PERSONAL_KB_", "TEAM_KB_"):
        assert tok not in s
    assert "KB_LEAK_PERSONAL" not in s and "KB_LEAK_TEAM" not in s


def test_header_rewritten() -> None:
    lines = SCRIPT.read_text().splitlines()[3:48]
    head = "\n".join(lines)
    assert "personal-kb" not in head and "team-kb" not in head
    assert (
        "#   kb-mcp-refresh: tightbeam_kb=<S> agent-gtd=<S> agent-gtd-probe=<P> rc=<N>"
        in lines
    )
    assert "5  --claude path is not an executable file" in head


def test_env_template_documents_tightbeam_only() -> None:
    text = ENV_TMPL.read_text()
    assert "PERSONAL_KB" not in text
    assert "TEAM_KB_URL" not in text and "TEAM_KB_API_KEY" not in text
    assert text.count("TEAM_KB_DATABASE_URL") == 2
    assert "TIGHTBEAM_KB_MCP_URL=" in text and "TIGHTBEAM_KB_MCP_TOKEN=" in text


def test_deploy_wiring() -> None:
    d = DEPLOY.read_text()
    assert "scripts/kb-mcp-refresh.sh" in d
    assert "personal_kb MCP server" not in d
    i = d.index('if ! sudo bash "${_stage}/kb-mcp-refresh.sh"')
    line = d[d.rindex("\n", 0, i) + 1 : d.index("\n", i)]
    assert (
        "[WARN] MCP refresh failed on ${host} — agent-gtd/KB registrations may be stale"
        in line
    )
    assert "exit 1" not in line
    assert '--probe "${_stage}/mcp-probe.py"' in line
    scp = next(
        ln
        for ln in d.splitlines()
        if "scp -q" in ln and "scripts/kb-mcp-refresh.sh" in ln
    )
    assert "templates/mcp-probe.py" in scp
    sud = d.index("sudoers-refresh.sh", d.index("bash -s <<EOF"))
    rm = d.index('\nrm -rf "${_stage}"', sud) + 1
    assert sud < i < rm


def test_setup_wiring() -> None:
    s = SETUP.read_text()
    assert "scripts/kb-mcp-refresh.sh" in s
    assert "for _known_name in aws-documentation-mcp-server; do" in s
    assert "_agent_gtd_mcp_src" not in s
    assert "AGENT_GTD_MCP_SRC" not in s
    invocation = s.index('"$_kbr" --env-file')
    assert s.index("scripts/kb-mcp-refresh.sh") < invocation
    assert s.index("is not registered as http", invocation) > invocation
    assert "PERSONAL_KB_MCP_SRC" not in s
    assert "bound there to the same" not in s


# --- agent-gtd (HTTP /mcp, URL-only, env-only bearer) ---


def test_gtd_exact_add_json(env: Env) -> None:
    gtd_env(env, "https://gtd.example/")
    env.cj.write_text("{}")
    r = env.run()
    assert r.returncode == 0, r.stderr
    gtd_calls = [c for c in env.calls() if "agent-gtd" in c]
    assert gtd_calls[-1] == [
        "mcp", "add-json", "agent-gtd", json.dumps(D_GTD), "--scope", "user"
    ]  # fmt: skip
    assert (
        "[CHANGED] agent-gtd registered as http https://gtd.example/mcp "
        "(reason: absent)" in r.stdout
    )


def _run_helper(extra: dict[str, str]) -> dict[str, str]:
    e = {"PATH": "/usr/bin:/bin", **extra}
    out = subprocess.run(
        ["/bin/sh", "-c", D_GTD["headersHelper"]],
        capture_output=True,
        text=True,
        env=e,
        check=True,
    )
    parsed: dict[str, str] = json.loads(out.stdout)
    return parsed


def test_gtd_helper_output() -> None:
    assert _run_helper({"AGENT_GTD_API_KEY": "k1"}) == {"Authorization": "Bearer k1"}
    assert _run_helper(
        {"AGENT_GTD_API_KEY": "k1", "AGENT_GTD_AGENT_NAME": "claude-build-abc12345"}
    ) == {"Authorization": "Bearer k1", "X-Agent-Name": "claude-build-abc12345"}


def test_gtd_helper_literal_in_script() -> None:
    line = next(
        ln for ln in SCRIPT.read_text().splitlines() if ln.startswith("HELPER_GTD=")
    )
    # Decode the bash $'...' literal: only \' escapes are used.
    assert line.startswith("HELPER_GTD=$'") and line.endswith("'")
    assert line[len("HELPER_GTD=$'") : -1].replace("\\'", "'") == D_GTD["headersHelper"]
    assert "cat" not in line and "~/" not in line and "/." not in line


def test_tightbeam_helper_reads_key_file(tmp_path: Path) -> None:
    e = Env(tmp_path)
    (e.home / ".tightbeam_kb_key").write_text("TK")
    run = subprocess.run(
        ["/bin/sh", "-c", D_TIGHTBEAM["headersHelper"]],
        env={"PATH": "/usr/bin:/bin", "HOME": str(e.home)},
        capture_output=True,
        text=True,
    )
    assert run.stdout == '{"Authorization": "Bearer TK"}'
    # The env form wins when the token is set (manual/interactive escape hatch).
    run = subprocess.run(
        ["/bin/sh", "-c", D_TIGHTBEAM["headersHelper"]],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(e.home),
            "TIGHTBEAM_KB_MCP_TOKEN": "ENV-TOKEN",
        },
        capture_output=True,
        text=True,
    )
    assert run.stdout == '{"Authorization": "Bearer ENV-TOKEN"}'


def test_gtd_url_only_no_key_file(env: Env) -> None:
    gtd_env(env, key=GK)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "agent-gtd=changed" in r.stdout.splitlines()[-1]
    names = [p.name for p in env.home.rglob("*") if p.name != ".claude.json"]
    assert not [n for n in names if "agent_gtd" in n or "agent-gtd" in n]
    for text in (env.log.read_text(), r.stdout, r.stderr):
        assert GK not in text


def test_gtd_url_only_without_key(env: Env) -> None:
    gtd_env(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "agent-gtd=changed" in r.stdout.splitlines()[-1]


def test_gtd_key_not_in_claude_json(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    gtd_env(e, key=GK)
    r = e.run(extra_env={"FAKE_MODE": "none"})
    assert r.returncode == 0, r.stderr
    assert json.loads(e.cj.read_text())["mcpServers"]["agent-gtd"] == D_GTD
    assert GK not in e.cj.read_text()


def test_gtd_leak_tripwire(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    gtd_env(e, key=GK)
    r = e.run(extra_env={"FAKE_MODE": "leak", "FAKE_LEAK": GK})
    assert r.returncode == 1
    assert (
        "[ERR]  agent-gtd: API key found in .claude.json — secret leaked into "
        "registration" in r.stderr
    )


def test_gtd_legacy_stdio_migration(env: Env) -> None:
    gtd_env(env)
    env.seed({"agent-gtd": LEGACY_GTD})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.calls() == [
        ["mcp", "remove", "agent-gtd", "--scope", "user"],
        ["mcp", "add-json", "agent-gtd", json.dumps(D_GTD), "--scope", "user"],
    ]
    assert (
        "[CHANGED] agent-gtd registered as http https://gtd.example/mcp "
        "(reason: legacy-stdio)" in r.stdout
    )


@pytest.mark.parametrize(
    ("seeded", "reason"),
    [
        ({**D_GTD, "url": "https://old.example/mcp"}, "url-changed"),
        ({**D_GTD, "headersHelper": "old"}, "helper-changed"),
    ],
)
def test_gtd_other_reasons(env: Env, seeded: dict[str, str], reason: str) -> None:
    gtd_env(env)
    env.seed({"agent-gtd": seeded})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert f"(reason: {reason})" in r.stdout
    assert env.calls() == [
        ["mcp", "remove", "agent-gtd", "--scope", "user"],
        ["mcp", "add-json", "agent-gtd", json.dumps(D_GTD), "--scope", "user"],
    ]


@pytest.mark.parametrize("text", ["", "AGENT_GTD_URL=\n"])
def test_gtd_missing_url_removes_stale(env: Env, text: str) -> None:
    env.write_env(text)
    env.seed({"agent-gtd": D_GTD})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "[SKIP] agent-gtd: missing AGENT_GTD_URL — not registered" in r.stdout
    assert "[CHANGED] removed stale agent-gtd registration" in r.stdout
    assert env.calls() == [["mcp", "remove", "agent-gtd", "--scope", "user"]]


def test_gtd_missing_url_no_entry_no_call(env: Env) -> None:
    env.write_env("")
    r = env.run(
        extra_env={"AGENT_GTD_URL": "https://ambient.example", "AGENT_GTD_API_KEY": "a"}
    )
    assert r.returncode == 0, r.stderr
    assert "[SKIP] agent-gtd: missing AGENT_GTD_URL — not registered" in r.stdout
    assert env.calls() == []


def test_gtd_restore_on_failed_add(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STATEFUL)
    gtd_env(e)
    e.seed({"agent-gtd": LEGACY_GTD})
    r = e.run(extra_env={"FAKE_FAIL_HTTP": "1"})
    assert r.returncode == 1
    assert json.loads(e.cj.read_text())["mcpServers"]["agent-gtd"] == LEGACY_GTD
    assert (
        "[ERR]  claude mcp add-json agent-gtd failed (rc=1); restored previous "
        "registration (type=stdio, reason was legacy-stdio)" in r.stderr
    )
    assert "agent-gtd=err" in r.stdout.splitlines()[-1]


def test_gtd_restore_failure_reported(env: Env) -> None:
    gtd_env(env)
    env.seed({"agent-gtd": LEGACY_GTD})
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert (
        "[ERR]  claude mcp add-json agent-gtd failed (rc=1); restore failed (rc=1) "
        "— agent-gtd is NOT registered" in r.stderr
    )
    assert [c[1] for c in env.calls()] == ["remove", "add-json", "add-json"]


def test_probe_arg_must_be_file(env: Env) -> None:
    gtd_env(env)
    r = env.run("--probe", str(env.home / "nope.py"))
    assert r.returncode == 2
    assert env.calls() == []


def test_probe_skipped_when_gtd_skipped(env: Env) -> None:
    env.write_env("")
    r = env.run("--probe", str(PROBE))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1].endswith("agent-gtd-probe=skipped rc=0")


class _Stub(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self) -> None:  # noqa: N802, RUF100 - stdlib name; pre-commit ruff flags it
        length = int(self.headers.get("Content-Length", "0"))
        msg = json.loads(self.rfile.read(length) or b"{}")
        if self.status != 200:
            body = b"denied"
            self.send_response(self.status)
        elif "id" not in msg:
            self.send_response(202)
            body = b""
        else:
            if msg["method"] == "initialize":
                result: dict[str, object] = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "stub", "version": "1"},
                }
            else:
                result = {"tools": [{"name": "add_item"}]}
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub_factory() -> Iterator[object]:
    servers: list[ThreadingHTTPServer] = []

    def make(status: int) -> int:
        handler = type("H", (_Stub,), {"status": status})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return int(srv.server_address[1])

    yield make
    for srv in servers:
        srv.shutdown()


@pytest.mark.parametrize(("status", "probe"), [(200, "ok"), (401, "fail")])
def test_probe_against_stub(
    env: Env, stub_factory: object, status: int, probe: str
) -> None:
    port = stub_factory(status)  # type: ignore[operator]
    base = f"http://127.0.0.1:{port}"
    gtd_env(env, base, key=GK)
    env.seed({"agent-gtd": {**D_GTD, "url": f"{base}/mcp"}})
    r = env.run("--probe", str(PROBE))
    assert r.returncode == 0, r.stderr
    assert env.calls() == []
    last = r.stdout.splitlines()[-1]
    assert f"agent-gtd=ok agent-gtd-probe={probe} rc=0" in last
    if probe == "ok":
        assert f"[OK]   agent-gtd probe passed ({base}/mcp)" in r.stdout
    else:
        assert "[WARN] agent-gtd MCP probe failed (rc=1)" in r.stdout
        assert "reason=http-401" in r.stdout
    assert GK not in r.stdout + r.stderr
