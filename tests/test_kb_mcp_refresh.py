"""Tests for scripts/kb-mcp-refresh.sh and its wiring into deploy.sh and
setup-dispatch-host.sh. Hermetic: a fake argv-recording `claude`, a tmp agent
home, no root.
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

USER = pwd.getpwuid(os.getuid()).pw_name
GROUP = grp.getgrgid(os.getgid()).gr_name

PK = "pk-SECRET-1"
TK = "tk-SECRET-2"
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


def desired(url: str, helper_var: str, keyfile: str) -> dict[str, str]:
    return {
        "type": "http",
        "url": url,
        "headersHelper": (
            'printf \'{"Authorization": "Bearer %s"}\' '
            f'"${{{helper_var}:-$(cat ~/{keyfile} 2>/dev/null)}}"'
        ),
    }


D_PERSONAL = desired(
    "https://kb.example/mcp", "PERSONAL_KB_API_KEY", ".personal_kb_hook_key"
)
D_TEAM = desired("https://team.example/mcp", "TEAM_KB_API_KEY", ".team_kb_key")
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


def both_env(e: Env) -> None:
    e.write_env(
        f"PERSONAL_KB_URL=https://kb.example\nPERSONAL_KB_API_KEY={PK}\n"
        f"TEAM_KB_URL=https://team.example/\nTEAM_KB_API_KEY={TK}\n"
    )


def personal_env(e: Env) -> None:
    e.write_env(f"PERSONAL_KB_URL=https://kb.example\nPERSONAL_KB_API_KEY={PK}\n")


def test_unchanged(env: Env) -> None:
    both_env(env)
    env.seed({"personal-kb": D_PERSONAL, "team-kb": D_TEAM})
    pf = env.key_file(".personal_kb_hook_key", PK)
    tf = env.key_file(".team_kb_key", TK)
    before = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (pf, tf)]
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("registration up to date") == 2
    assert (
        "[OK]   personal-kb registration up to date (https://kb.example/mcp)"
        in r.stdout
    )
    assert env.calls() == []
    assert before == [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (pf, tf)]
    assert r.stdout.splitlines()[-1] == (
        "kb-mcp-refresh: personal-kb=ok team-kb=ok agent-gtd=skipped "
        "agent-gtd-probe=skipped rc=0"
    )


def test_changed_url(env: Env) -> None:
    personal_env(env)
    env.seed({"personal-kb": {**D_PERSONAL, "url": "https://old.example/mcp"}})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.calls() == [
        ["mcp", "remove", "personal-kb", "--scope", "user"],
        ["mcp", "add-json", "personal-kb", json.dumps(D_PERSONAL), "--scope", "user"],
    ]
    assert "reason: url-changed" in r.stdout
    assert "[SKIP] team-kb:" in r.stdout


def test_legacy_stdio(env: Env) -> None:
    personal_env(env)
    env.seed(
        {
            "personal-kb": {
                "type": "stdio",
                "command": "uvx",
                "env": {"PERSONAL_KB_API_KEY": "old-legacy-key"},
            }
        }
    )
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert [c[1] for c in env.calls()] == ["remove", "add-json"]
    assert "reason: legacy-stdio" in r.stdout


def test_absent_reason_and_helper_changed(env: Env) -> None:
    personal_env(env)
    r = env.run()
    assert "reason: absent" in r.stdout
    env.seed({"personal-kb": {**D_PERSONAL, "headersHelper": "old"}})
    r = env.run()
    assert "reason: helper-changed" in r.stdout


def test_missing_key_skip_removes_stale(env: Env) -> None:
    env.write_env("PERSONAL_KB_URL=https://kb.example\n")
    env.seed({"personal-kb": D_PERSONAL})
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert any(
        ln.startswith("[SKIP] personal-kb:") and "PERSONAL_KB_API_KEY" in ln
        for ln in r.stdout.splitlines()
    )
    assert "removed stale personal-kb registration" in r.stdout
    assert env.calls() == [["mcp", "remove", "personal-kb", "--scope", "user"]]


def test_missing_key_no_entry_no_call(env: Env) -> None:
    env.write_env("PERSONAL_KB_URL=https://kb.example\n")
    r = env.run()
    assert r.returncode == 0
    assert "[SKIP] personal-kb:" in r.stdout
    assert env.calls() == []


def test_remove_failure(env: Env) -> None:
    env.write_env("PERSONAL_KB_URL=https://kb.example\n")
    env.seed({"personal-kb": D_PERSONAL})
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert "[ERR]  claude mcp remove personal-kb failed (rc=1)" in r.stderr
    assert r.stdout.splitlines()[-1].endswith("rc=1")


def test_key_file_perms(env: Env) -> None:
    personal_env(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    kf = env.home / ".personal_kb_hook_key"
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert kf.read_text() == PK
    assert "reason: absent" in r.stdout


def test_key_file_mode_repair_without_claude_call(env: Env) -> None:
    personal_env(env)
    env.seed({"personal-kb": D_PERSONAL})
    kf = env.key_file(".personal_kb_hook_key", PK, 0o644)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert "reason: mode-644" in r.stdout
    assert "registration up to date" in r.stdout
    assert env.calls() == []


def test_content_changed_key_file(env: Env) -> None:
    personal_env(env)
    env.seed({"personal-kb": D_PERSONAL})
    kf = env.key_file(".personal_kb_hook_key", "stale\n")
    r = env.run()
    assert "reason: content-changed" in r.stdout
    assert kf.read_text() == PK


def test_registration_changed_key_file_current(env: Env) -> None:
    personal_env(env)
    env.key_file(".personal_kb_hook_key", PK)
    r = env.run()
    assert "[OK]" in r.stdout and "personal_kb_hook_key up to date" in r.stdout
    assert [c[1] for c in env.calls()] == ["remove", "add-json"]


def test_no_key_material_leaks(env: Env) -> None:
    both_env(env)
    r = env.run()
    assert r.returncode == 0, r.stderr
    for text in (env.log.read_text(), r.stdout, r.stderr):
        assert PK not in text and TK not in text


def test_protected_servers_never_touched(env: Env) -> None:
    both_env(env)
    env.seed(
        {
            "aws-documentation-mcp-server": {"command": "x"},
            "personal-kb": {"command": "uvx"},
        }
    )
    env.run()
    env.write_env("")
    env.run()
    log = env.log.read_text()
    assert "aws-documentation-mcp-server" not in log


def test_add_json_failure_keeps_key_file(env: Env) -> None:
    personal_env(env)
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert "[ERR]  claude mcp add-json personal-kb failed (rc=1)" in r.stderr
    kf = env.home / ".personal_kb_hook_key"
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert kf.read_text() == PK


def test_team_still_processed_after_personal_failure(env: Env) -> None:
    both_env(env)
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert "add-json team-kb failed" in r.stderr


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
    personal_env(env)
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
    personal_env(env)
    env.claude.chmod(0o644)
    r = env.run()
    assert r.returncode == 5
    assert env.calls() == []
    assert not (env.home / ".personal_kb_hook_key").exists()


def test_env_quoting_last_wins(env: Env) -> None:
    env.write_env(
        f'PERSONAL_KB_URL="https://first.example"\nPERSONAL_KB_URL="https://kb.example"\n'
        f"PERSONAL_KB_API_KEY='{PK}'\n"
    )
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.calls()[-1][3] == json.dumps(D_PERSONAL)
    assert (env.home / ".personal_kb_hook_key").read_text() == PK


def test_ambient_env_ignored(env: Env) -> None:
    env.write_env("PERSONAL_KB_URL=https://kb.example\n")
    r = env.run(extra_env={"PERSONAL_KB_API_KEY": "ambient-should-not-be-used"})
    assert "[SKIP] personal-kb:" in r.stdout
    assert env.calls() == []


def test_unparseable_claude_json(env: Env) -> None:
    personal_env(env)
    env.cj.write_text("{not json")
    r = env.run()
    assert r.returncode == 1
    assert "[ERR]  cannot parse" in r.stderr
    assert env.calls() == []
    assert not (env.home / ".personal_kb_hook_key").exists()


def test_leak_tripwire(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    personal_env(e)
    r = e.run(extra_env={"FAKE_MODE": "leak", "FAKE_LEAK": PK})
    assert r.returncode == 1
    assert "API key found in .claude.json" in r.stderr


def test_readback_drift(tmp_path: Path) -> None:
    e = Env(tmp_path, FAKE_CLAUDE_STORING)
    personal_env(e)
    r = e.run(extra_env={"FAKE_MODE": "extra"})
    assert r.returncode == 0, r.stderr
    assert "[WARN] personal-kb readback differs" in r.stdout
    assert "extra" in r.stdout


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


def gtd_env(e: Env, url: str = "https://gtd.example", key: str | None = None) -> None:
    text = f"AGENT_GTD_URL={url}\n"
    if key is not None:
        text += f"AGENT_GTD_API_KEY={key}\n"
    e.write_env(text)


def all_env(e: Env) -> None:
    e.write_env(
        f"PERSONAL_KB_URL=https://kb.example\nPERSONAL_KB_API_KEY={PK}\n"
        f"TEAM_KB_URL=https://team.example/\nTEAM_KB_API_KEY={TK}\n"
        f"AGENT_GTD_URL=https://gtd.example\nAGENT_GTD_API_KEY={GK}\n"
    )


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


def test_gtd_url_only_no_key_file(env: Env) -> None:
    gtd_env(env, key=GK)
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "agent-gtd=changed" in r.stdout.splitlines()[-1]
    names = [
        p.name for p in env.home.rglob("*") if p.name != ".claude.json"
    ]  # fmt: skip
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


def test_unchanged_all_three(env: Env) -> None:
    all_env(env)
    env.seed({"personal-kb": D_PERSONAL, "team-kb": D_TEAM, "agent-gtd": D_GTD})
    env.key_file(".personal_kb_hook_key", PK)
    env.key_file(".team_kb_key", TK)
    before = env.cj.read_bytes()
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (
        "[OK]   agent-gtd registration up to date (https://gtd.example/mcp)" in r.stdout
    )
    assert r.stdout.count("registration up to date") == 3
    assert env.calls() == []
    assert env.cj.read_bytes() == before
    assert r.stdout.splitlines()[-1] == (
        "kb-mcp-refresh: personal-kb=ok team-kb=ok agent-gtd=ok "
        "agent-gtd-probe=skipped rc=0"
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


def test_gtd_processed_after_kb_failure(env: Env) -> None:
    all_env(env)
    r = env.run(extra_env={"FAKE_CLAUDE_RC": "1"})
    assert r.returncode == 1
    assert "add-json agent-gtd failed" in r.stderr


@pytest.mark.parametrize(
    ("text", "name", "var"),
    [
        ("PERSONAL_KB_URL=https://kb.example\n", "personal-kb", "PERSONAL_KB_API_KEY"),
        ("TEAM_KB_URL=https://team.example\n", "team-kb", "TEAM_KB_API_KEY"),
    ],
)
def test_kb_missing_key_still_skipped(env: Env, text: str, name: str, var: str) -> None:
    env.write_env(text + "AGENT_GTD_URL=https://gtd.example\n")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert f"[SKIP] {name}: missing {var} — not registered" in r.stdout


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
