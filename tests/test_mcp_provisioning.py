"""Tests for the hosted thin-client KB MCP registration (1b4f224f).

Two concerns:
  1. Hermetic renders of templates/mcp-servers.sh under bash, plus text guards
     over templates/mcp-servers.sh and setup-dispatch-host.sh.
  2. End-to-end exercises of templates/mcp-probe.py against stub MCP servers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator

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
    def test_kb_servers_not_rendered_even_when_configured(self) -> None:
        proc = _render(
            {
                "PERSONAL_KB_URL": "https://kb.example",
                "PERSONAL_KB_API_KEY": "pk-test",
                "TEAM_KB_URL": "https://team-kb.example",
                "TEAM_KB_API_KEY": "tk-test",
            }
        )
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["aws-documentation-mcp-server"]

    def test_no_kb_vars_yields_aws_only(self) -> None:
        proc = _render({})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["aws-documentation-mcp-server"]

    def test_personal_kb_url_without_key_is_skipped(self) -> None:
        proc = _render({"PERSONAL_KB_URL": "https://kb.example"})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["aws-documentation-mcp-server"]

    def test_team_kb_url_without_key_is_skipped(self) -> None:
        proc = _render({"TEAM_KB_URL": "https://team-kb.example"})
        assert proc.returncode == 0, proc.stderr
        assert _entry_names(proc) == ["aws-documentation-mcp-server"]


class TestTextGuards:
    def test_mcp_servers_sh(self) -> None:
        text = MCP_SERVERS_SH.read_text()
        assert "-e ANTHROPIC_API_KEY=" not in text
        assert "KB_ANTHROPIC_API_KEY" not in text
        assert "KB_DATABASE_URL" not in text
        assert "PERSONAL_KB_MCP_SRC" not in text
        for token in ("warn ", "info ", "die ", "would "):
            assert token not in text

    def test_setup_dispatch_host_sh(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert "mcp list" not in text
        assert "KB_ANTHROPIC_API_KEY" not in text
        # The retired KB DSNs may appear only in the strip step, never be read.
        for retired in ("TEAM_KB_DATABASE_URL", "KB_DATABASE_URL"):
            assert f"_read_env_var {retired}" not in text
            assert f"_set_service_env_var {retired}" not in text
        assert (
            "_RETIRED_DSN_RE='^(KB_DATABASE_URL|KB_SERVICE_DATABASE_URL|"
            "TEAM_KB_DATABASE_URL)='" in text
        )
        assert "_read_env_var PERSONAL_KB_URL" in text
        assert "_read_env_var PERSONAL_KB_API_KEY" in text
        assert "_read_env_var TEAM_KB_URL" in text
        assert "_read_env_var TEAM_KB_API_KEY" in text
        assert "MCP_PROBE_TIMEOUT:-600" in text
        assert "/api/health" in text
        assert (
            'would "read AGENT_GTD_URL, AGENT_GTD_API_KEY, '
            "PERSONAL_KB_URL, PERSONAL_KB_API_KEY, TEAM_KB_URL, TEAM_KB_API_KEY "
            'from ${SERVICE_ENV}"'
        ) in text
        assert "PERSONAL_KB_MCP_SRC" not in text
        assert 'for entry in "agent-gtd|" "${MCP_SERVERS[@]}"; do' in text
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


# --- mcp-probe.py HTTP (type: http) tests --------------------------------------

_GTD_HELPER = (
    'if [ -n "${AGENT_GTD_AGENT_NAME:-}" ]; then printf \'{"Authorization": '
    '"Bearer %s", "X-Agent-Name": "%s"}\' "${AGENT_GTD_API_KEY:-}" '
    '"$AGENT_GTD_AGENT_NAME"; else printf \'{"Authorization": "Bearer %s"}\' '
    '"${AGENT_GTD_API_KEY:-}"; fi'
)


class _McpHttpStub(BaseHTTPRequestHandler):
    # Behaviour knobs, set per test on a subclass.
    mode: ClassVar[str] = "json"  # json | sse | status | auth
    status: ClassVar[int] = 200
    seen: ClassVar[list[dict[str, str]]]

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802, RUF100 - stdlib name; pre-commit ruff flags it
        length = int(self.headers.get("Content-Length", "0"))
        msg = json.loads(self.rfile.read(length) or b"{}")
        self.seen.append(dict(self.headers.items()))
        auth = self.headers.get("Authorization", "")
        if self.mode == "status":
            self._send(self.status, b'{"error": "nope"}')
            return
        if self.mode == "auth" and auth == "Bearer ":
            self._send(401, b'{"error": "unauthenticated"}')
            return
        if self.mode == "echo":
            self._send(401, f"bad credentials: {auth}".encode(), "text/plain")
            return
        if "id" not in msg:
            self._send(202, b"")
            return
        if msg["method"] == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "0"},
            }
        else:
            result = {"tools": [{"name": "add_item"}, {"name": "get_item"}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if self.mode == "sse":
            body = f"event: message\ndata: {payload}\n\n".encode()
            self._send(200, body, "text/event-stream")
        else:
            self._send(200, payload.encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def http_stub(tmp_path: Path) -> Iterator[object]:
    servers: list[ThreadingHTTPServer] = []

    def start(mode: str = "json", status: int = 200) -> tuple[Path, list]:
        seen: list[dict[str, str]] = []
        handler = type(
            "H", (_McpHttpStub,), {"mode": mode, "status": status, "seen": seen}
        )
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
        cj = tmp_path / f".claude-{len(servers)}.json"
        cj.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "agent-gtd": {
                            "type": "http",
                            "url": url,
                            "headersHelper": _GTD_HELPER,
                        }
                    }
                }
            )
        )
        return cj, seen

    yield start
    for srv in servers:
        srv.shutdown()


def _run_http_probe(
    claude_json: Path, key: str, helper_override: str | None = None
) -> subprocess.CompletedProcess[str]:
    if helper_override is not None:
        data = json.loads(claude_json.read_text())
        data["mcpServers"]["agent-gtd"]["headersHelper"] = helper_override
        claude_json.write_text(json.dumps(data))
    env = {**os.environ, "AGENT_GTD_API_KEY": key}
    env.pop("AGENT_GTD_AGENT_NAME", None)
    return subprocess.run(
        [
            sys.executable,
            str(MCP_PROBE),
            "--claude-json",
            str(claude_json),
            "--name",
            "agent-gtd",
            "--expect-tool",
            "add_item",
            "--timeout",
            "10",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


class TestMcpProbeHttp:
    def test_json_pass(self, http_stub: object) -> None:
        cj, seen = http_stub("json")  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PASS agent-gtd tools=2")
        assert len(seen) == 3
        for headers in seen:
            assert headers["Authorization"] == "Bearer k1"
            assert headers["Content-Type"] == "application/json"
            assert headers["Accept"] == "application/json, text/event-stream"
            assert "MCP-Protocol-Version" not in headers

    def test_sse_pass(self, http_stub: object) -> None:
        cj, _ = http_stub("sse")  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PASS agent-gtd")

    def test_empty_key_401(self, http_stub: object) -> None:
        cj, _ = http_stub("auth")  # type: ignore[operator]
        proc = _run_http_probe(cj, "")
        assert proc.returncode == 1
        assert "FAIL agent-gtd reason=http-401 elapsed_s=" in proc.stdout

    def test_500(self, http_stub: object) -> None:
        cj, _ = http_stub("status", 500)  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1")
        assert proc.returncode == 2
        assert "reason=http-500" in proc.stdout

    def test_404(self, http_stub: object) -> None:
        cj, _ = http_stub("status", 404)  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1")
        assert proc.returncode == 1
        assert "reason=http-404" in proc.stdout

    def test_redaction(self, http_stub: object) -> None:
        cj, _ = http_stub("echo")  # type: ignore[operator]
        proc = _run_http_probe(cj, "sekret-123")
        assert proc.returncode == 1
        assert "sekret-123" not in proc.stdout
        assert "sekret-123" not in proc.stderr
        assert "***REDACTED***" in proc.stdout or "reason=http-401" in proc.stdout

    def test_helper_exit(self, http_stub: object) -> None:
        cj, seen = http_stub("json")  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1", helper_override="exit 7")
        assert proc.returncode == 2
        assert "reason=helper-exit-7" in proc.stdout
        assert seen == []

    def test_helper_bad_json(self, http_stub: object) -> None:
        cj, _ = http_stub("json")  # type: ignore[operator]
        proc = _run_http_probe(cj, "k1", helper_override="echo '[1]'")
        assert proc.returncode == 2
        assert "reason=helper-bad-json" in proc.stdout

    def test_connect_refused(self, tmp_path: Path) -> None:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cj = tmp_path / ".claude.json"
        cj.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "agent-gtd": {
                            "type": "http",
                            "url": f"http://127.0.0.1:{port}/mcp",
                            "headersHelper": _GTD_HELPER,
                        }
                    }
                }
            )
        )
        proc = _run_http_probe(cj, "k1")
        assert proc.returncode == 2
        assert "reason=connect-refused" in proc.stdout


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
