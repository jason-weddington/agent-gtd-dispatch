"""Non-Linux refusal in the shell entry points (deploy.sh, talos-update.sh, setup)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Records every call as one line "<tool> <argv...>" and answers `uname -s` for
# the host named in SHIM_OS_<host>; any other ssh command fails (the first real
# deploy step), so a Linux host is observed reaching its deploy loop and no
# further. SHIM_SSH_FAIL=1 makes every ssh exit 255 (unreachable host).
SSH_SHIM = """#!/bin/sh
echo "ssh $*" >> "$SHIM_LOG"
[ "${SHIM_SSH_FAIL:-0}" = 1 ] && exit 255
while [ "$#" -gt 0 ]; do
    case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
    esac
done
host=$1
shift
if [ "$*" = "uname -s" ]; then
    eval "os=\\${SHIM_OS_$host:-}"
    [ -n "$os" ] || exit 255
    echo "$os"
    exit 0
fi
cat >/dev/null
exit 1
"""

SCP_SHIM = """#!/bin/sh
echo "scp $*" >> "$SHIM_LOG"
exit 0
"""


def _shims(tmp_path: Path) -> Path:
    shim = tmp_path / "shim"
    shim.mkdir()
    for name, body in (("ssh", SSH_SHIM), ("scp", SCP_SHIM)):
        f = shim / name
        f.write_text(body)
        f.chmod(0o755)
    return shim


def _run(
    tmp_path: Path, script: str, hosts: str, oses: dict[str, str], *args: str, **extra
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    shim = _shims(tmp_path)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "SHIM_LOG": str(log),
        "DISPATCH_INDEX": "http://index.invalid/simple/",
        "TALOS_ARTIFACT_BASE": "http://artifacts.invalid/talos",
        "DISPATCH_HOSTS": hosts,
        **{f"SHIM_OS_{h}": o for h, o in oses.items()},
        **extra,
    }
    env.pop("DISPATCH_HOST", None)
    r = subprocess.run(
        ["bash", str(ROOT / script), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    return r, log.read_text().splitlines()


def _calls(lines: list[str], tool: str, host: str) -> list[str]:
    out = []
    for line in lines:
        parts = line.split()
        if parts[0] != tool:
            continue
        hit_ssh = tool == "ssh" and host in parts[1:]
        hit_scp = tool == "scp" and any(p.startswith(f"{host}:") for p in parts[1:])
        if hit_ssh or hit_scp:
            out.append(line)
    return out


class TestDeploy:
    def test_mac_skipped_linux_still_deployed(self, tmp_path: Path) -> None:
        r, calls = _run(tmp_path, "deploy.sh", "a b", {"a": "Darwin", "b": "Linux"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert _calls(calls, "ssh", "a") == ["ssh -o BatchMode=yes a uname -s"]
        assert _calls(calls, "scp", "a") == []
        assert _calls(calls, "scp", "b"), calls
        assert len(_calls(calls, "ssh", "b")) >= 2
        assert (
            "[WARN] skipped a (Darwin): not Linux; manual macOS path, "
            "docs/install.md (Running on macOS)" in r.stderr
        )

    def test_single_mac_host(self, tmp_path: Path) -> None:
        r, calls = _run(tmp_path, "deploy.sh", "a", {"a": "Darwin"})
        assert r.returncode == 1
        assert len([c for c in calls if c.startswith("ssh ")]) == 1
        assert [c for c in calls if c.startswith("scp ")] == []
        assert "skipped a (Darwin)" in r.stderr

    def test_ssh_failure_is_an_error(self, tmp_path: Path) -> None:
        r, calls = _run(tmp_path, "deploy.sh", "a", {}, SHIM_SSH_FAIL="1")
        assert r.returncode != 0
        assert "could not determine the OS of a" in r.stderr
        assert [c for c in calls if c.startswith("scp ")] == []

    def test_preflight_runs_before_any_deploy(self, tmp_path: Path) -> None:
        # The mac sorts AFTER the Linux host: it is still probed first.
        r, calls = _run(tmp_path, "deploy.sh", "b a", {"a": "Darwin", "b": "Linux"})
        assert r.returncode == 1
        assert calls[0].endswith("b uname -s") and calls[1].endswith("a uname -s")


class TestTalosUpdate:
    def test_mac_skipped_linux_still_updated(self, tmp_path: Path) -> None:
        r, calls = _run(
            tmp_path,
            "talos-update.sh",
            "a b",
            {"a": "Darwin", "b": "Linux"},
            "--version",
            "0.1.0-gabc",
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert _calls(calls, "ssh", "a") == ["ssh -o BatchMode=yes a uname -s"]
        assert len(_calls(calls, "ssh", "b")) == 2
        assert "skipped a (Darwin)" in r.stderr

    def test_ssh_failure_is_an_error(self, tmp_path: Path) -> None:
        r, _ = _run(
            tmp_path, "talos-update.sh", "a", {}, "--version", "x", SHIM_SSH_FAIL="1"
        )
        assert r.returncode != 0
        assert "could not determine the OS of a" in r.stderr


class TestFleetHostsHelper:
    def test_partition(self, tmp_path: Path) -> None:
        shim = _shims(tmp_path)
        script = (
            f". {ROOT / 'scripts' / 'fleet-hosts.sh'}; HOSTS='a b c d'; "
            "partition_linux_hosts; "
            'echo "L=$LINUX_HOSTS|N=$NON_LINUX_HOSTS|E=$OS_ERROR_HOSTS"; '
            "report_host_partition; echo rc=$?"
        )
        r = subprocess.run(
            ["bash", "-c", script],
            env={
                **os.environ,
                "PATH": f"{shim}:{os.environ['PATH']}",
                "SHIM_LOG": str(tmp_path / "log"),
                "SHIM_OS_a": "Linux",
                "SHIM_OS_b": "Darwin",
                "SHIM_OS_d": "Linux",
            },
            capture_output=True,
            text=True,
        )
        assert "L=a d|N=b=Darwin|E=c" in r.stdout
        assert "rc=1" in r.stdout
        assert "could not determine the OS of c" in r.stderr


class TestSetupDispatchHost:
    @pytest.mark.parametrize("args", [[], ["--dry-run"]])
    def test_refuses_non_linux_before_anything(
        self, tmp_path: Path, args: list[str]
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("must run non-root to prove the platform check comes first")
        shim = tmp_path / "shim"
        shim.mkdir()
        uname = shim / "uname"
        uname.write_text(
            '#!/bin/sh\nif [ "$1" = -s ]; then echo Darwin; exit 0; fi\n'
            'exec /usr/bin/uname "$@"\n'
        )
        uname.chmod(0o755)
        work = tmp_path / "cwd"
        work.mkdir()
        r = subprocess.run(
            ["bash", str(ROOT / "setup-dispatch-host.sh"), *args],
            env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"},
            cwd=work,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode != 0
        assert "not Linux" in r.stderr and "docs/install.md" in r.stderr
        assert "root" not in r.stderr.lower()
        assert list(work.iterdir()) == []
