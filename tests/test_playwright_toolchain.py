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
      dispatched repo finds them without any per-repo configuration;
  (e) neutral-cwd behavioural checks: root's install-deps runs from / in both
      scripts (rendered through a real heredoc for deploy.sh), so a .npmrc in
      the operator's cwd cannot fail it, and failures carry the exit code.
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


NPM_ERR = "npm error code ENOENT"


def _marker_shim(path: Path, marker: Path) -> None:
    path.parent.mkdir()
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' \"$PWD\" > '{marker}'\n"
        f"if [ -e \"$PWD/.npmrc\" ]; then echo '{NPM_ERR}' >&2; exit 254; fi\n"
        "exit 0\n"
    )
    path.chmod(0o755)


def _fail_shim(path: Path) -> None:
    path.parent.mkdir()
    path.write_text(f"#!/usr/bin/env bash\necho '{NPM_ERR}' >&2; exit 254\n")
    path.chmod(0o755)


def _setup_install_deps_command() -> str:
    lines = SETUP_SCRIPT.read_text().split("\n")
    starts = [
        i
        for i, line in enumerate(lines)
        if not line.lstrip().startswith("#")
        and "install-deps" in line
        and not line.lstrip().startswith("would")
        and not (i > 0 and lines[i - 1].endswith("\\"))
    ]
    assert len(starts) == 1, f"expected exactly one install-deps command: {starts}"
    i = starts[0]
    parts = [lines[i]]
    while parts[-1].endswith("\\"):
        i += 1
        parts.append(lines[i])
    cmd = "\n".join(parts)
    assert cmd.strip().startswith("( cd / && env PATH=")
    assert cmd.strip().endswith(
        '_pw_failed+=("${_pw_ver} install-deps (exit ${_rc})"); }'
    )
    return cmd


def _deploy_block() -> str:
    lines = DEPLOY_SCRIPT.read_text().split("\n")
    stripped = [line.strip() for line in lines]
    start_m = "_pw_deps_log=\\$(mktemp)"
    end_m = 'rm -f "\\$_pw_deps_log"'
    assert stripped.count(start_m) == 1
    assert stripped.count(end_m) == 1
    return "\n".join(lines[stripped.index(start_m) : stripped.index(end_m) + 1])


class TestInstallDepsNeutralCwd:
    def _env(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".npmrc").write_text("prefix=/nonexistent/npm-global\n")
        marker = tmp_path / "marker"
        good = tmp_path / "bin" / "npx"
        bad = tmp_path / "failbin" / "npx"
        _marker_shim(good, marker)
        _fail_shim(bad)
        return cwd, marker, good, bad

    def _run(
        self, tmp_path: Path, cwd: Path, body: str
    ) -> subprocess.CompletedProcess[str]:
        harness = tmp_path / "harness.sh"
        harness.write_text(body)
        return subprocess.run(
            ["bash", str(harness)], cwd=cwd, capture_output=True, text=True
        )

    def _setup_body(self, npx: Path, tail: str) -> str:
        return (
            "set -euo pipefail\n"
            'warn() { echo "WARN $*" >&2; return 0; }\n'
            "_pw_failed=()\n"
            f"_pw_npx='{npx}'\n"
            "_pw_ver=1.63.0\n"
            "PLAYWRIGHT_BROWSERS=chromium\n"
            "AGENT_USER=dispatch\n"
            f"{_setup_install_deps_command()}\n{tail}\n"
        )

    def _render_deploy(self, tmp_path: Path, cwd: Path) -> str:
        render = tmp_path / "render.sh"
        render.write_text(
            "set -euo pipefail\nPLAYWRIGHT_BROWSERS=chromium\nAGENT_USER=dispatch\n"
            f"cat <<EOF\n{_deploy_block()}\nEOF\n"
        )
        proc = subprocess.run(
            ["bash", str(render)], cwd=cwd, capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        for lit in ("$_pw_npx", "$(dirname", "$(mktemp)", "( cd / && sudo env"):
            assert lit in proc.stdout
        return proc.stdout

    def _run_deploy(
        self, tmp_path: Path, npx: Path
    ) -> subprocess.CompletedProcess[str]:
        cwd = tmp_path / "cwd"
        rendered = self._render_deploy(tmp_path, cwd)
        body = (
            'set -euo pipefail\nsudo() { "$@"; }\n'
            f"_pw_npx='{npx}'\n_v=1.63.0\n{rendered}\n"
        )
        return self._run(tmp_path, cwd, body)

    def test_setup_success_runs_from_root(self, tmp_path: Path) -> None:
        cwd, marker, good, _ = self._env(tmp_path)
        proc = self._run(
            tmp_path, cwd, self._setup_body(good, 'echo "N=${#_pw_failed[@]}"')
        )
        assert proc.returncode == 0, proc.stderr
        assert "N=0" in proc.stdout
        assert marker.read_text() == "/"

    def test_setup_failure_is_observable(self, tmp_path: Path) -> None:
        cwd, _, _, bad = self._env(tmp_path)
        proc = self._run(
            tmp_path,
            cwd,
            self._setup_body(bad, "printf '%s\\n' \"${_pw_failed[@]}\""),
        )
        assert proc.returncode == 0, proc.stderr
        assert "1.63.0 install-deps (exit 254)" in proc.stdout
        assert "install-deps failed (exit 254) for dispatch" in proc.stderr

    def test_deploy_success_runs_from_root(self, tmp_path: Path) -> None:
        _, marker, good, _ = self._env(tmp_path)
        proc = self._run_deploy(tmp_path, good)
        assert proc.returncode == 0, proc.stderr
        assert "[OK]   playwright@1.63.0 install-deps (chromium)" in proc.stdout
        assert "install-deps failed" not in proc.stderr
        assert marker.read_text() == "/"

    def test_deploy_failure_is_observable(self, tmp_path: Path) -> None:
        _, _, _, bad = self._env(tmp_path)
        proc = self._run_deploy(tmp_path, bad)
        assert proc.returncode == 0, proc.stderr
        assert "install-deps failed (exit 254) for dispatch" in proc.stderr
        assert NPM_ERR in proc.stderr
        assert "[OK]   playwright@1.63.0 install-deps" not in proc.stdout

    def test_text_pins(self) -> None:
        setup = SETUP_SCRIPT.read_text()
        deploy = DEPLOY_SCRIPT.read_text()
        assert (
            "would \"run (cd / && '${_pw_npx}' --yes playwright@${_pw_ver} "
            'install-deps ${PLAYWRIGHT_BROWSERS}) (as root)"' in setup
        )
        assert 'would "runuser -l ${AGENT_USER} -c \\"' + DOWNLOAD_TIMEOUT in setup
        assert "cd /tmp && " + DOWNLOAD_TIMEOUT in deploy
        for text in (setup, deploy):
            for line in text.split("\n"):
                if not line.lstrip().startswith("#"):
                    assert not ("cd /tmp" in line and "install-deps" in line)
        lines = deploy.split("\n")
        a = lines.index(
            "# Refresh the agent user's Playwright browsers (setup-dispatch-host.sh Step 4.11)."
        )
        b = next(i for i in range(a + 1, len(lines)) if lines[i] == "fi")
        assert not any("`" in line for line in lines[a : b + 1])
