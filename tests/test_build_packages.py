"""Tests for the system build packages every dispatch host provisions (Step 4.9).

Every host must build every dispatched project, so the system packages a repo needs
(cmake, ALSA headers, the Tauri 2 Linux stack, pnpm) live in templates/dev-toolchain.sh
and both setup-dispatch-host.sh and deploy.sh install them. Hermetic: sources the data
file in bash and checks text properties of the consuming scripts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TOOLCHAIN_CONF = REPO_ROOT / "templates" / "dev-toolchain.sh"
SETUP_SCRIPT = REPO_ROOT / "setup-dispatch-host.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"

REQUIRED_APT = {
    "cmake",
    "libasound2-dev",
    "libclang-dev",
    "pkg-config",
    "libwebkit2gtk-4.1-dev",
    "libgtk-3-dev",
    "libsoup-3.0-dev",
    "libjavascriptcoregtk-4.1-dev",
    "libayatana-appindicator3-dev",
}


def _source(expr: str) -> list[str]:
    proc = subprocess.run(
        ["bash", "-c", f"source '{TOOLCHAIN_CONF}'\nprintf '%s\\n' {expr}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def test_apt_list_covers_native_and_tauri_builds() -> None:
    pkgs = _source('"${DEV_TOOLCHAIN_APT_PKGS[@]}"')
    assert REQUIRED_APT <= set(pkgs)
    assert len(pkgs) == len(set(pkgs))


def test_npm_global_list_has_pnpm() -> None:
    entries = _source('"${DEV_TOOLCHAIN_NPM_GLOBAL_PKGS[@]}"')
    assert "pnpm|pnpm" in entries
    assert all(e.count("|") == 1 for e in entries)


def test_both_consumers_read_the_lists_and_stay_valid_bash() -> None:
    for script in (SETUP_SCRIPT, DEPLOY_SCRIPT):
        subprocess.run(["bash", "-n", str(script)], check=True)
        text = script.read_text()
        assert "DEV_TOOLCHAIN_APT_PKGS" in text
        assert "DEV_TOOLCHAIN_NPM_GLOBAL_PKGS" in text
        assert "dpkg-query" in text
