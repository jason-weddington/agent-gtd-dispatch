"""Hermetic tests for the macOS cross-check toolchain (zig, pinned crate, rust target).

Sources templates/dev-toolchain.sh in bash and checks text properties of the two
consumers. Nothing is downloaded and no host state is read.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONF = REPO_ROOT / "templates" / "dev-toolchain.sh"
SETUP = REPO_ROOT / "setup-dispatch-host.sh"
DEPLOY = REPO_ROOT / "deploy.sh"

SHA_X86 = "02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239"
SHA_ARM = "958ed7d1e00d0ea76590d27666efbf7a932281b3d7ba0c6b01b0ff26498f667f"
PARSE = '_m="${_e%%|*}"; _rest="${_e#*|}"; _zarch="${_rest%%|*}"; _zsha="${_rest#*|}"'


def _bash(script: str) -> list[str]:
    proc = subprocess.run(
        ["bash", "-c", f"source '{CONF}'\n{script}"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "DISPATCH_WORKSPACE_ROOT": "/nonexistent-ws"},
    )
    return [line for line in proc.stdout.splitlines() if line]


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i)]


class TestDataFile:
    def test_zig_pins(self) -> None:
        assert _bash('echo "$ZIG_VERSION"') == ["0.15.2"]
        assert _bash('printf "%s\\n" "${ZIG_ARCH_MAP[@]}"') == [
            f"x86_64|x86_64|{SHA_X86}",
            f"aarch64|aarch64|{SHA_ARM}",
        ]
        for entry in _bash('printf "%s\\n" "${ZIG_ARCH_MAP[@]}"'):
            assert re.fullmatch(r"[a-z0-9_]+\|[a-z0-9_]+\|[0-9a-f]{64}", entry)
        assert CONF.read_text().count(SHA_X86) >= 1

    def test_url_and_paths(self) -> None:
        url = _bash(
            'u="${ZIG_URL_TEMPLATE//\\{version\\}/$ZIG_VERSION}"; echo "${u//\\{arch\\}/x86_64}"'
        )
        assert url == [
            "https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz"
        ]
        assert _bash('echo "$ZIG_INSTALL_DIR_TEMPLATE"') == ["/opt/zig-{version}"]
        assert _bash('echo "$ZIG_BIN_LINK"') == ["/usr/local/bin/zig"]

    def test_lists(self) -> None:
        pinned = _bash('printf "%s\\n" "${DEV_TOOLCHAIN_CARGO_PINNED_PKGS[@]}"')
        assert pinned == ["cargo-zigbuild|cargo-zigbuild|0.23.4"]
        assert all(e.count("|") == 2 for e in pinned)
        assert _bash('printf "%s\\n" "${DEV_TOOLCHAIN_RUST_TARGETS[@]}"') == [
            "aarch64-apple-darwin"
        ]
        unpinned = _bash('printf "%s\\n" "${DEV_TOOLCHAIN_CARGO_PKGS[@]}"')
        assert all(e.count("|") == 1 for e in unpinned)
        assert "xz-utils" in _bash('printf "%s\\n" "${DEV_TOOLCHAIN_APT_PKGS[@]}"')

    def test_parse_seam(self) -> None:
        out = _bash(
            f'for _e in "${{ZIG_ARCH_MAP[@]}}"; do {PARSE}; echo "$_m $_zarch $_zsha"; done'
        )
        assert out == [f"x86_64 x86_64 {SHA_X86}", f"aarch64 aarch64 {SHA_ARM}"]


class TestConsumers:
    def test_valid_bash_and_reference_lists(self) -> None:
        for script in (SETUP, DEPLOY, CONF):
            subprocess.run(["bash", "-n", str(script)], check=True)
        for script in (SETUP, DEPLOY):
            text = script.read_text()
            for needle in (
                "ZIG_ARCH_MAP",
                "DEV_TOOLCHAIN_CARGO_PINNED_PKGS",
                "DEV_TOOLCHAIN_RUST_TARGETS",
                "sha256sum",
                "[STATE] cross-toolchain",
                "--force",
                "@=",
                "toolchain list",
                "--toolchain",
            ):
                assert needle in text, (script.name, needle)

    def test_no_hard_coded_pins(self) -> None:
        for script in (SETUP, DEPLOY):
            text = script.read_text()
            for needle in (
                "0.15.2",
                "0.23.4",
                "aarch64-apple-darwin",
                "cargo-zigbuild",
                SHA_X86,
                SHA_ARM,
            ):
                assert needle not in text, (script.name, needle)

    def test_setup_zig_block_order(self) -> None:
        block = _block(SETUP.read_text(), "# Sub-action D: zig", "# Step 4.10")
        assert (
            block.index("sha256sum")
            < block.index("--strip-components")
            < block.index("ln -sfn")
        )
        assert "chmod 0755" in block

    def test_setup_placement_and_banner(self) -> None:
        text = SETUP.read_text()
        assert (
            "--- Step 4.9: Dev toolchain (system build packages, Rust tools, gitleaks, "
            "zig + darwin cross target, agent user) ---" in text
        )
        assert text.index("DEV_TOOLCHAIN_CARGO_PINNED_PKGS[@]") < text.index(
            "Dev toolchain incomplete for"
        )
        assert text.index("DEV_TOOLCHAIN_RUST_TARGETS[@]") < text.index(
            "Dev toolchain incomplete for"
        )

    def test_deploy_zig_block_order(self) -> None:
        block = _block(
            DEPLOY.read_text(),
            "# zig: re-install only when the installed version differs from the pinned version.",
            "# State line",
        )
        assert (
            block.index("sha256sum")
            < block.index("--strip-components")
            < block.index("ln -sfn")
        )

    def test_deploy_defaults_all_vars_when_conf_missing(self) -> None:
        text = DEPLOY.read_text()
        else_branch = text[text.index("not found — dev toolchain refresh") :]
        else_branch = else_branch[: else_branch.index("\nfi\n")]
        for var in (
            "ZIG_VERSION",
            "ZIG_ARCH_MAP_LIST",
            "ZIG_URL_VERSIONED",
            "ZIG_INSTALL_DIR",
            "ZIG_BIN_LINK",
            "CARGO_PINNED_LIST",
            "RUST_TARGET_LIST",
        ):
            assert f'{var}=""' in else_branch, var
