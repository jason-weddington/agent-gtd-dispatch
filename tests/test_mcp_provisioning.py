"""Tests for the hosted thin-client KB MCP registration (1b4f224f).

Two concerns:
  1. Hermetic renders of templates/mcp-servers.sh under bash, plus text guards
     over templates/mcp-servers.sh and setup-dispatch-host.sh.
  2. End-to-end exercises of templates/mcp-probe.py against stub MCP servers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
MCP_SERVERS_SH = REPO_ROOT / "templates" / "mcp-servers.sh"
SETUP_SCRIPT = REPO_ROOT / "setup-dispatch-host.sh"
MCP_PROBE = REPO_ROOT / "templates" / "mcp-probe.py"

_RENDER_SCRIPT = 'set -euo pipefail; source templates/mcp-servers.sh; printf "%s\\n" "${MCP_SERVERS[@]}"'


def _render(case_vars: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import os
    import tempfile

    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME") or tempfile.gettempdir(),
        **case_vars,
    }
    return subprocess.run(
        ["bash", "-c", _RENDER_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _entry_names(proc: subprocess.CompletedProcess[str]) -> list[str]:
    lines = [line for line in proc.stdout.splitlines() if line]
    return [line.split("|", 1)[0] for line in lines]


class TestMcpServersRender:
    def test_personal_kb_registered_with_expected_args(self) -> None:
        proc = _render(
            {"PERSONAL_KB_URL": "https://kb.example", "PERSONAL_KB_API_KEY": "pk-test"}
        )
        assert proc.returncode == 0, proc.stderr
        line = next(
            ln for ln in proc.stdout.splitlines() if ln.startswith("personal-kb|")
        )
        assert "-e PERSONAL_KB_URL=https://kb.example" in line
        assert "-e PERSONAL_KB_API_KEY=pk-test" in line
        assert "-e KB_CONTRIBUTOR=jason" in line
        # PEP 508 form: the extra belongs to the PACKAGE NAME, not the URL path.
        # `git+ssh://...personal_kb[postgres]@<ref>` makes git clone a repo
        # literally named "personal_kb[postgres]@<ref>" and fails at launch.
        assert "personal-kb[postgres]@git+ssh://" in line
        assert line.endswith(" personal-kb")
        assert "ANTHROPIC_API_KEY" not in line
        assert "KB_DATABASE_URL" not in line
        assert "KB_INSTANCE_ROLE" not in line

    def test_team_kb_registered_with_expected_args(self) -> None:
        proc = _render(
            {"TEAM_KB_URL": "https://team-kb.example", "TEAM_KB_API_KEY": "tk-test"}
        )
        assert proc.returncode == 0, proc.stderr
        line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("team-kb|"))
        assert "-e PERSONAL_KB_URL=https://team-kb.example" in line
        assert "-e PERSONAL_KB_API_KEY=tk-test" in line
        assert "-e KB_INSTANCE_ROLE=team" in line
        assert "-e KB_TEAM=grit-mile" in line
        assert "KB_CONTRIBUTOR" not in line
        assert "KB_DATABASE_URL" not in line

    def test_no_kb_vars_yields_two_servers(self) -> None:
        proc = _render({})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["agent-gtd", "aws-documentation-mcp-server"]

    def test_personal_kb_url_without_key_is_skipped(self) -> None:
        proc = _render({"PERSONAL_KB_URL": "https://kb.example"})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["agent-gtd", "aws-documentation-mcp-server"]

    def test_team_kb_url_without_key_is_skipped(self) -> None:
        proc = _render({"TEAM_KB_URL": "https://team-kb.example"})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["agent-gtd", "aws-documentation-mcp-server"]

    def test_personal_kb_mcp_src_override(self) -> None:
        proc = _render(
            {
                "PERSONAL_KB_URL": "https://kb.example",
                "PERSONAL_KB_API_KEY": "pk-test",
                "PERSONAL_KB_MCP_SRC": "git+ssh://example/x@deadbeef",
            }
        )
        assert proc.returncode == 0, proc.stderr
        line = next(
            ln for ln in proc.stdout.splitlines() if ln.startswith("personal-kb|")
        )
        assert "--from git+ssh://example/x@deadbeef personal-kb" in line


class TestTextGuards:
    def test_mcp_servers_sh(self) -> None:
        text = MCP_SERVERS_SH.read_text()
        assert "-e ANTHROPIC_API_KEY=" not in text
        assert "KB_ANTHROPIC_API_KEY" not in text
        assert "KB_DATABASE_URL" not in text
        for token in ("warn ", "info ", "die ", "would "):
            assert token not in text

    def test_setup_dispatch_host_sh(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert "mcp list" not in text
        assert "KB_ANTHROPIC_API_KEY" not in text
        assert "TEAM_KB_DATABASE_URL" not in text
        assert "_read_env_var PERSONAL_KB_URL" in text
        assert "_read_env_var PERSONAL_KB_API_KEY" in text
        assert "_read_env_var TEAM_KB_URL" in text
        assert "_read_env_var TEAM_KB_API_KEY" in text
        assert "MCP_PROBE_TIMEOUT:-600" in text
        assert "/api/health" in text
        assert (
            'would "read AGENT_GTD_URL, AGENT_GTD_API_KEY, AGENT_GTD_MCP_SRC, '
            "PERSONAL_KB_URL, PERSONAL_KB_API_KEY, TEAM_KB_URL, TEAM_KB_API_KEY, "
            'PERSONAL_KB_MCP_SRC from ${SERVICE_ENV}"'
        ) in text
        # PERSONAL_KB_MCP_SRC must be readable from the service env, so a host can
        # pin the personal_kb ref; the template's default tracks the branch head.
        assert 'PERSONAL_KB_MCP_SRC="$(_read_env_var PERSONAL_KB_MCP_SRC)"' in text
        assert (
            'would "GET <PERSONAL_KB_URL>/api/health and <TEAM_KB_URL>/api/health '
            '(reachability warn-check)"'
        ) in text
        assert (
            'would "probe each registered MCP server as ${AGENT_USER} with an MCP '
            'initialize + tools/list handshake (timeout ${MCP_PROBE_TIMEOUT}s per server)"'
        ) in text


# --- mcp-probe.py end-to-end tests ---------------------------------------

_STUB_SRC = """\
import json
import os
import sys
import time

mode = os.environ.get("STUB_MODE", "pass")

if mode == "timeout":
    time.sleep(5)
    sys.exit(0)

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        if mode == "no-initialize":
            continue
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"serverInfo": {"name": "stub", "version": "0"}},
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
        if mode == "die-after-init":
            sys.exit(0)
    elif method == "tools/list":
        if mode == "tools-error":
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -1, "message": "boom"}}
        elif mode == "empty-tools":
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": []}}
        elif mode == "secret-echo":
            sys.stderr.write(os.environ.get("PERSONAL_KB_API_KEY", "") + "\\n")
            sys.stderr.flush()
            resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -1, "message": "boom"}}
        else:
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [{"name": "kb_search"}]}}
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
"""


def _write_stub(tmp_path: Path) -> Path:
    stub_path = tmp_path / "stub_mcp.py"
    stub_path.write_text(_STUB_SRC)
    return stub_path


def _write_claude_json(
    tmp_path: Path,
    stub_path: Path,
    name: str = "stub",
    env: dict[str, str] | None = None,
) -> Path:
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {
                        "command": sys.executable,
                        "args": [str(stub_path)],
                        "env": env or {},
                    }
                }
            }
        )
    )
    return claude_json


def _run_probe(
    claude_json: Path,
    name: str,
    expect_tool: str | None = None,
    timeout: float = 5,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(MCP_PROBE),
        "--claude-json",
        str(claude_json),
        "--name",
        name,
        "--timeout",
        str(timeout),
    ]
    if expect_tool is not None:
        cmd += ["--expect-tool", expect_tool]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestMcpProbe:
    def test_well_behaved_stub_passes(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(tmp_path, stub, env={"STUB_MODE": "pass"})
        proc = _run_probe(claude_json, "stub")
        assert proc.returncode == 0
        assert proc.stdout.startswith("PASS stub tools=1")

    def test_stub_that_dies_after_initialize_fails(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(
            tmp_path, stub, env={"STUB_MODE": "die-after-init"}
        )
        proc = _run_probe(claude_json, "stub")
        assert proc.returncode == 1
        assert (
            "reason=no-initialize" in proc.stdout or "reason=tools-error" in proc.stdout
        )
        assert not proc.stdout.startswith("PASS")

    def test_tools_list_error_response_fails(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(
            tmp_path, stub, env={"STUB_MODE": "tools-error"}
        )
        proc = _run_probe(claude_json, "stub")
        assert proc.returncode == 1
        assert "reason=tools-error" in proc.stdout

    def test_empty_tools_list_fails(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(
            tmp_path, stub, env={"STUB_MODE": "empty-tools"}
        )
        proc = _run_probe(claude_json, "stub")
        assert proc.returncode == 1
        assert "reason=empty-tools" in proc.stdout

    def test_missing_expected_tool_fails(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(tmp_path, stub, env={"STUB_MODE": "pass"})
        proc = _run_probe(claude_json, "stub", expect_tool="nope")
        assert proc.returncode == 1
        assert "reason=missing-tool" in proc.stdout

    def test_timeout_kills_the_child(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(tmp_path, stub, env={"STUB_MODE": "timeout"})
        import time

        start = time.monotonic()
        proc = _run_probe(claude_json, "stub", timeout=1)
        elapsed = time.monotonic() - start
        assert proc.returncode == 3
        assert "reason=timeout" in proc.stdout
        # Killed near the 1s budget, not left running for the stub's full 5s sleep.
        assert elapsed < 4

    def test_absent_server_name(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(tmp_path, stub, env={"STUB_MODE": "pass"})
        proc = _run_probe(claude_json, "absent")
        assert proc.returncode == 4
        assert "reason=not-registered" in proc.stdout

    def test_secret_redaction(self, tmp_path: Path) -> None:
        stub = _write_stub(tmp_path)
        claude_json = _write_claude_json(
            tmp_path,
            stub,
            env={
                "STUB_MODE": "secret-echo",
                "PERSONAL_KB_API_KEY": "testonly-not-a-real-secret-value",
            },
        )
        proc = _run_probe(claude_json, "stub")
        assert "***REDACTED***" in proc.stdout
        assert "testonly-not-a-real-secret-value" not in proc.stdout


@pytest.fixture(autouse=True)
def _no_ambient_kb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against the dev box / dispatch hosts exporting KB vars into the
    test process itself — _render() builds its own explicit env dict, so this
    only protects against accidental os.environ leakage elsewhere in this file."""
    for var in (
        "PERSONAL_KB_URL",
        "PERSONAL_KB_API_KEY",
        "TEAM_KB_URL",
        "TEAM_KB_API_KEY",
        "KB_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
