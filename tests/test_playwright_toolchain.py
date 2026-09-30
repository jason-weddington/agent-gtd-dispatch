"""Tests for the Playwright browser provisioning toolchain (Step 4.11).

Hermetic by construction: every test either sources templates/dev-toolchain.sh
in a `bash` subprocess (no network, no npx — it is a data file) or asserts
text/syntax properties of the two shell scripts that consume it:

  (a) the Playwright pins are well-formed data (non-empty array of semver
      entries + non-empty browser list) — this is what guarantees "adding a
      version is a one-line change in one file";
  (b) both consuming scripts stay syntactically valid bash (`bash -n`);
  (c) both scripts carry the 30s-download-cap override, and setup carries the
      Step 4.11 banner;
  (d) neither script ever sets PLAYWRIGHT_BROWSERS_PATH — browsers must land
      in the agent user's DEFAULT cache (~/.cache/ms-playwright) so every
      dispatched repo finds them without any per-repo configuration.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TOOLCHAIN_CONF = REPO_ROOT / "templates" / "dev-toolchain.sh"
SETUP_SCRIPT = REPO_ROOT / "setup-dispatch-host.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"

SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
DOWNLOAD_TIMEOUT = "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=600000"


def _source_toolchain() -> tuple[list[str], str]:
    """Source the data file in bash and print the two Playwright pins."""
    script = (
        f"source '{TOOLCHAIN_CONF}'\n"
        "printf '%s\\n' \"${PLAYWRIGHT_VERSIONS[*]}\"\n"
        "printf '%s' \"${PLAYWRIGHT_BROWSERS}\"\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    lines = proc.stdout.split("\n")
    versions = lines[0].split() if lines[0] else []
    browsers = lines[1] if len(lines) > 1 else ""
    return versions, browsers


class TestToolchainPins:
    def test_playwright_versions_is_nonempty_semver_array(self) -> None:
        versions, _ = _source_toolchain()
        assert versions, "PLAYWRIGHT_VERSIONS must be a non-empty array"
        for v in versions:
            assert SEMVER.match(v), f"entry {v!r} is not a plain x.y.z semver"

    def test_playwright_versions_pins_camera_profiles(self) -> None:
        # camera-profiles apps/desktop pins @playwright/test 1.63.0 — the pin
        # list must stay in lockstep with the dispatched repos.
        versions, _ = _source_toolchain()
        assert "1.63.0" in versions

    def test_playwright_browsers_nonempty(self) -> None:
        _, browsers = _source_toolchain()
        assert browsers.strip(), "PLAYWRIGHT_BROWSERS must be non-empty"

    def test_toolchain_conf_is_plain_bash(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(TOOLCHAIN_CONF)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr


class TestScriptSyntax:
    def test_setup_script_bash_n(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr

    def test_deploy_script_bash_n(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr


class TestScriptText:
    def test_setup_has_step_4_11(self) -> None:
        assert "Step 4.11: Playwright browsers" in SETUP_SCRIPT.read_text()

    def test_setup_has_download_timeout(self) -> None:
        assert DOWNLOAD_TIMEOUT in SETUP_SCRIPT.read_text()

    def test_deploy_has_download_timeout(self) -> None:
        assert DOWNLOAD_TIMEOUT in DEPLOY_SCRIPT.read_text()


class TestNoBrowsersPathOverride:
    def test_setup_never_sets_browsers_path(self) -> None:
        assert "PLAYWRIGHT_BROWSERS_PATH" not in SETUP_SCRIPT.read_text()

    def test_deploy_never_sets_browsers_path(self) -> None:
        assert "PLAYWRIGHT_BROWSERS_PATH" not in DEPLOY_SCRIPT.read_text()
