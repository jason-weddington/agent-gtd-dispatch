#!/usr/bin/env python3
"""Stdlib-only MCP stdio health probe.

Reads a server's REGISTERED shape (command/args/env) out of a `.claude.json`
file and performs a real `initialize` + `tools/list` JSON-RPC handshake
against it over stdio. This is deliberately NOT `claude mcp list` — that
command reports every server as failed when run from a shell whose cwd/PATH
differ from the agent's, so it is a registration-name check, not a health
check (kb-03289). This probe actually launches the registered command and
talks MCP to it.

No third-party imports — this must run with nothing but the standard library
so it has no install step of its own on a freshly-provisioned host.

Exit codes:
    0  pass
    1  probe failed (no initialize result / tools-list error / 0 tools /
       expected tool absent)
    2  the process could not be launched, or resolution failed (non-zero
       child exit with no parsable MCP response on stdout)
    3  timed out
    4  the named server is absent from .claude.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_LAUNCH = 2
EXIT_TIMEOUT = 3
EXIT_NOT_REGISTERED = 4

_REDACT_KEY_RE = re.compile(r"(API_KEY|TOKEN|PASSWORD|SECRET|DATABASE_URL)$")
_REDACTED = "***REDACTED***"


def _load_registered_server(claude_json_path: str, name: str) -> dict | None:
    with open(claude_json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    servers = data.get("mcpServers", {}) or {}
    server = servers.get(name)
    if not isinstance(server, dict):
        return None
    return server


def _secret_values(env: dict[str, str]) -> list[str]:
    return [v for k, v in env.items() if v and _REDACT_KEY_RE.search(k)]


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, _REDACTED)
    return text


def _tail(text: str, n: int = 20) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _build_requests() -> str:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "setup-dispatch-host", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    return "".join(json.dumps(m) + "\n" for m in messages)


def _parse_responses(stdout: str) -> tuple[dict | None, dict | None]:
    """Return (initialize_response, tools_list_response).

    Each is the raw parsed JSON object with id==1 / id==2, or None if that
    response was never seen on stdout.
    """
    init_msg: dict | None = None
    tools_msg: dict | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == 1 and init_msg is None:
            init_msg = msg
        elif msg.get("id") == 2 and tools_msg is None:
            tools_msg = msg
    return init_msg, tools_msg


def probe(
    claude_json: str,
    name: str,
    expect_tool: str | None,
    timeout: float,
) -> tuple[int, str]:
    """Run the initialize + tools/list handshake against a registered server.

    Returns (exit_code, message) — never raises for probe-level failures
    (launch errors, timeouts, protocol errors); those are reported as a
    non-zero exit code with a diagnostic message instead.
    """
    start = time.monotonic()

    def elapsed() -> int:
        return int(time.monotonic() - start)

    server = _load_registered_server(claude_json, name)
    if server is None:
        return (
            EXIT_NOT_REGISTERED,
            f"FAIL {name} reason=not-registered elapsed_s={elapsed()}",
        )

    command = server.get("command")
    args = server.get("args") or []
    registered_env = server.get("env") or {}
    argv = [command, *args]
    secrets = _secret_values(registered_env)
    env = {**os.environ, **registered_env}

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is a list, never a shell string
            argv,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return (
            EXIT_LAUNCH,
            f"FAIL {name} reason=launch elapsed_s={elapsed()}\n"
            + _redact(str(exc), secrets),
        )

    # Write the requests but hold stdin OPEN until both responses arrive.
    # Closing stdin immediately (plain communicate(input=...)) makes the server
    # see EOF and shut down; on a slower host — or in HTTP mode, which starts
    # slower — it exits before answering tools/list, and the probe reports a
    # bogus tools-error. Reproduced on pironman01 (aarch64): identical probe
    # passes when stdin is held open, fails when it is closed at once.
    stdout_chunks: list[str] = []
    reader = threading.Thread(
        target=lambda: stdout_chunks.extend(iter(proc.stdout.readline, "")),  # type: ignore[union-attr]
        daemon=True,
    )
    reader.start()
    try:
        if proc.stdin is None:  # pragma: no cover - Popen(stdin=PIPE) always sets it
            return EXIT_LAUNCH, f"FAIL {name} reason=launch elapsed_s={elapsed()}"
        proc.stdin.write(_build_requests())
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        # Wait for the id=2 (tools/list) response, then close stdin so the
        # server exits cleanly.
        while time.monotonic() < deadline:
            if any('"id":2' in c or '"id": 2' in c for c in stdout_chunks):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return EXIT_TIMEOUT, f"FAIL {name} reason=timeout elapsed_s={elapsed()}"
    reader.join(timeout=5)
    stdout = "".join(stdout_chunks)
    stderr = proc.stderr.read() if proc.stderr is not None else ""

    stdout = stdout or ""
    stderr = stderr or ""
    combined = _redact(stdout + stderr, secrets)
    returncode = proc.returncode

    init_msg, tools_msg = _parse_responses(stdout)
    init_ok = bool(
        init_msg
        and isinstance(init_msg.get("result"), dict)
        and "serverInfo" in init_msg["result"]
    )

    if not init_ok:
        if returncode not in (0, None) and init_msg is None and tools_msg is None:
            return (
                EXIT_LAUNCH,
                f"FAIL {name} reason=launch elapsed_s={elapsed()}\n{_tail(combined)}",
            )
        return (
            EXIT_FAIL,
            f"FAIL {name} reason=no-initialize elapsed_s={elapsed()}\n"
            f"{_tail(combined)}",
        )

    if tools_msg is None or "error" in tools_msg:
        return (
            EXIT_FAIL,
            f"FAIL {name} reason=tools-error elapsed_s={elapsed()}\n{_tail(combined)}",
        )

    result = tools_msg.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or len(tools) == 0:
        return (
            EXIT_FAIL,
            f"FAIL {name} reason=empty-tools elapsed_s={elapsed()}\n{_tail(combined)}",
        )

    if expect_tool is not None:
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        if expect_tool not in names:
            return (
                EXIT_FAIL,
                f"FAIL {name} reason=missing-tool elapsed_s={elapsed()}\n"
                f"{_tail(combined)}",
            )

    return EXIT_PASS, f"PASS {name} tools={len(tools)} elapsed_s={elapsed()}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, run the probe, print the result line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-json", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--expect-tool", default=None)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)

    code, message = probe(args.claude_json, args.name, args.expect_tool, args.timeout)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
