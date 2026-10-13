"""setup-dispatch-host.sh fails closed on deployment config it has no default for.

The checks run right after argument parsing, before the root check and before any
step mutates the host, so these run unprivileged and touch nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SETUP_SCRIPT = Path(__file__).parent.parent / "setup-dispatch-host.sh"


def _run(*flags: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"]}
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT), *flags],
        capture_output=True,
        text=True,
        env={**base, **env},
        check=False,
    )


class TestRequiredConfig:
    def test_missing_wheel_index_fails_closed(self) -> None:
        r = _run("--dry-run")
        assert r.returncode != 0
        assert "DISPATCH_WHEEL_INDEX must be set" in r.stderr

    def test_with_talos_requires_harness_repo_url(self) -> None:
        r = _run("--dry-run", "--with-talos", DISPATCH_WHEEL_INDEX="https://x/simple/")
        assert r.returncode != 0
        assert "--with-talos requires HARNESS_DESIGN_REPO_URL" in r.stderr

    def test_harness_repo_url_not_required_without_talos(self) -> None:
        r = _run("--dry-run", DISPATCH_WHEEL_INDEX="https://x/simple/")
        assert "HARNESS_DESIGN_REPO_URL" not in r.stderr
        assert "DISPATCH_WHEEL_INDEX must be set" not in r.stderr
