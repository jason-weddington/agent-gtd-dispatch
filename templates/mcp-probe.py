#!/usr/bin/env python3
"""Stdlib-only MCP health probe (stdio and HTTP).

Reads a server's REGISTERED shape out of a `.claude.json` file and performs a
real `initialize` + `tools/list` JSON-RPC handshake against it: over stdio for
a command/args/env entry, or over streamable HTTP (one JSON-RPC message per
POST to the entry's `url`) for a `type: http` entry, whose `headersHelper` is
run via `/bin/sh -c` with the probe's own environment to obtain the request
headers. HTTP probing validates TLS against Python's trust store, not Node's,
so a pass here does not prove Claude Code (Node) trusts the certificate.

This is deliberately NOT `claude mcp list` — that command reports every
server as failed when run from a shell whose cwd/PATH differ from the agent's,
so it is a registration-name check, not a health check (kb-03289). This probe
actually launches the registered command (or POSTs to the registered URL) and
talks MCP to it.

No third-party imports — this must run with nothing but the standard library
so it has no install step of its own on a freshly-provisioned host.

Exit codes:
    0  pass
    1  probe failed (no initialize result / tools-list error / 0 tools /
       expected tool absent)
    2  the process could not be launched, or resolution failed (non-zero
       child exit with no parsable MCP response on stdout)

    HTTP entries print `FAIL <name> reason=<token> elapsed_s=<n>` with:
    helper-exit-<rc> / helper-bad-json / http-5xx / dns / connect-refused /
    connect -> 2; http-4xx / tls-verify / no-initialize -> 1; timeout -> 3.
    3  timed out
    4  the named server is absent from .claude.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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


def _build_messages() -> list[dict]:
    return [
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


def _build_requests() -> str:
    return "".join(json.dumps(m) + "\n" for m in _build_messages())


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


def _check_results(
    name: str,
    init_msg: dict | None,
    tools_msg: dict | None,
    expect_tool: str | None,
    elapsed: int,
    detail: str,
) -> tuple[int, str] | None:
    """Shared tools-error / empty-tools / missing-tool checks (after initialize).

    Returns None on pass, else (exit_code, message).
    """
    if tools_msg is None or "error" in tools_msg:
        return (
            EXIT_FAIL,
            f"FAIL {name} reason=tools-error elapsed_s={elapsed}\n{detail}",
        )
    result = tools_msg.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or len(tools) == 0:
        return (
            EXIT_FAIL,
            f"FAIL {name} reason=empty-tools elapsed_s={elapsed}\n{detail}",
        )
    if expect_tool is not None:
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        if expect_tool not in names:
            return (
                EXIT_FAIL,
                f"FAIL {name} reason=missing-tool elapsed_s={elapsed}\n{detail}",
            )
    return None


class _HttpProbeError(Exception):
    def __init__(self, code: int, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = detail


def _parse_http_body(body: str, content_type: str, want_id: int) -> dict | None:
    """Return the JSON-RPC message with id == want_id from a JSON or SSE body."""
    if "text/event-stream" in content_type:
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        return None
    try:
        msg = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(msg, list):
        for m in msg:
            if isinstance(m, dict) and m.get("id") == want_id:
                return m
        return None
    return msg if isinstance(msg, dict) and msg.get("id") == want_id else None


def _is_timeout(exc: BaseException) -> bool:
    # socket.timeout is an alias of TimeoutError on Python 3.10+.
    return isinstance(exc, TimeoutError)


def _http_post(
    url: str,
    headers: dict[str, str],
    message: dict,
    remaining: float,
) -> tuple[str, str, str]:
    """POST one JSON-RPC message; return (content_type, body, session_id).

    Raises _HttpProbeError for transport failures and non-2xx statuses.
    """
    req = urllib.request.Request(  # noqa: S310 - url comes from the registration
        url,
        data=json.dumps(message).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=remaining) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
            return (
                resp.headers.get("Content-Type", ""),
                body,
                resp.headers.get("Mcp-Session-Id", ""),
            )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # diagnostic only
            detail = ""
        code = EXIT_LAUNCH if exc.code >= 500 else EXIT_FAIL
        raise _HttpProbeError(code, f"http-{exc.code}", detail) from exc
    except ssl.SSLCertVerificationError as exc:
        raise _HttpProbeError(EXIT_FAIL, "tls-verify", str(exc)) from exc
    except urllib.error.URLError as exc:
        inner = exc.reason
        if isinstance(inner, ssl.SSLCertVerificationError):
            raise _HttpProbeError(EXIT_FAIL, "tls-verify", str(exc)) from exc
        if isinstance(inner, BaseException) and _is_timeout(inner):
            raise _HttpProbeError(EXIT_TIMEOUT, "timeout", str(exc)) from exc
        if isinstance(inner, socket.gaierror):
            raise _HttpProbeError(EXIT_LAUNCH, "dns", str(exc)) from exc
        if isinstance(inner, ConnectionRefusedError):
            raise _HttpProbeError(EXIT_LAUNCH, "connect-refused", str(exc)) from exc
        raise _HttpProbeError(EXIT_LAUNCH, "connect", str(exc)) from exc
    except socket.gaierror as exc:
        raise _HttpProbeError(EXIT_LAUNCH, "dns", str(exc)) from exc
    except ConnectionRefusedError as exc:
        raise _HttpProbeError(EXIT_LAUNCH, "connect-refused", str(exc)) from exc
    except OSError as exc:
        if _is_timeout(exc):
            raise _HttpProbeError(EXIT_TIMEOUT, "timeout", str(exc)) from exc
        raise _HttpProbeError(EXIT_LAUNCH, "connect", str(exc)) from exc


def _probe_http(
    server: dict,
    name: str,
    expect_tool: str | None,
    timeout: float,
    start: float,
) -> tuple[int, str]:
    """Run the handshake against a ``type: http`` registration."""

    def elapsed() -> int:
        return int(time.monotonic() - start)

    def remaining() -> float:
        return max(0.1, timeout - (time.monotonic() - start))

    def fail(code: int, reason: str, detail: str = "") -> tuple[int, str]:
        msg = f"FAIL {name} reason={reason} elapsed_s={elapsed()}"
        if detail:
            msg += "\n" + _tail(_redact(detail, secrets))
        return code, msg

    secrets: list[str] = _secret_values(dict(os.environ))
    headers: dict[str, str] = {}
    helper = server.get("headersHelper")
    if isinstance(helper, str) and helper:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv; helper is the registration's
                ["/bin/sh", "-c", helper],
                capture_output=True,
                text=True,
                timeout=remaining(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return fail(EXIT_TIMEOUT, "timeout")
        except OSError as exc:
            return fail(EXIT_LAUNCH, "helper-exit-127", str(exc))
        if proc.returncode != 0:
            return fail(EXIT_LAUNCH, f"helper-exit-{proc.returncode}")
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            return fail(EXIT_LAUNCH, "helper-bad-json")
        headers = parsed
        for value in headers.values():
            if value:
                secrets.append(value)
            if value.startswith("Bearer ") and value[len("Bearer ") :]:
                secrets.append(value[len("Bearer ") :])
    # Longest first so a full header value is redacted before its token substring.
    secrets.sort(key=len, reverse=True)

    url = server.get("url")
    if not isinstance(url, str) or not url:
        return fail(EXIT_LAUNCH, "connect", "registered http entry has no url")

    init_req, notif_req, tools_req = _build_messages()
    try:
        ctype, body, session_id = _http_post(url, headers, init_req, remaining())
        init_msg = _parse_http_body(body, ctype, 1)
        init_ok = bool(
            init_msg
            and isinstance(init_msg.get("result"), dict)
            and "serverInfo" in init_msg["result"]
        )
        if not init_ok:
            return fail(EXIT_FAIL, "no-initialize", body)
        if session_id:  # stateful servers; the board itself is stateless
            headers = {**headers, "Mcp-Session-Id": session_id}
        _http_post(url, headers, notif_req, remaining())
        ctype, tools_body, _sid = _http_post(url, headers, tools_req, remaining())
    except _HttpProbeError as exc:
        return fail(exc.code, exc.reason, exc.detail)
    tools_msg = _parse_http_body(tools_body, ctype, 2)
    bad = _check_results(
        name,
        init_msg,
        tools_msg,
        expect_tool,
        elapsed(),
        _tail(_redact(tools_body, secrets)),
    )
    if bad is not None:
        return bad
    assert tools_msg is not None  # noqa: S101 - narrowed by _check_results
    n_tools = len(tools_msg["result"]["tools"])
    return EXIT_PASS, f"PASS {name} tools={n_tools} elapsed_s={elapsed()}"


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

    if server.get("type") == "http":
        return _probe_http(server, name, expect_tool, timeout, start)

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
