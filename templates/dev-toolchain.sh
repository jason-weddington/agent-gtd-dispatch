# shellcheck shell=bash
# shellcheck disable=SC2034  # every variable here is consumed by the sourcing script
#
# dev-toolchain.sh — the dev toolchain provisioned for the dispatch agent user.
#
# Sourced by:
#   * setup-dispatch-host.sh  (Step 4.9 — installs the tools on the host)
#   * deploy.sh               (sourced LOCALLY from the repo checkout, then the
#                              resulting lists are interpolated into the ssh heredoc
#                              so the refresh block never hand-copies the tool list)
#
# This file is the SINGLE SOURCE OF TRUTH for the tool list. Do not duplicate any
# entry below into setup-dispatch-host.sh or deploy.sh.
#
# ---------------------------------------------------------------------------
# WHY these tools
# ---------------------------------------------------------------------------
# Dispatched repos activate their own hooks (lefthook.yml / .pre-commit-config.yaml)
# and their project "gate" command. Those hooks shell out to tools that must already
# be on the agent user's PATH — dispatched agents run as the unprivileged `dispatch`
# user and cannot install anything system-wide. harness-design's lefthook.yml is the
# current superset: cog, typos, cargo-sort, cargo-deny, cargo-llvm-cov, cargo-machete,
# cargo-nextest and gitleaks.
#
# ---------------------------------------------------------------------------
# FORMAT — DEV_TOOLCHAIN_CARGO_PKGS
# ---------------------------------------------------------------------------
# Each entry is "<crate>|<binary>":
#   <crate>   the name passed to `cargo binstall -y <crate>`
#   <binary>  the executable the crate provides, used for the idempotency check
#             (`command -v <binary>` as the agent user) and for logging.
# For most crates the two are identical; they differ for e.g. typos-cli -> typos
# and cocogitto -> cog. Same "pair per line" idiom as templates/mcp-servers.sh.
#
# To ADD a tool: append ONE line below. Re-run setup-dispatch-host.sh (or ./deploy.sh)
#   to provision it on every host. Nothing else needs editing.
# To REMOVE a tool: delete its line. Existing hosts keep the binary until an operator
#   runs `sudo -u dispatch -H bash -lc 'cargo uninstall <crate>'` (see docs/install.md
#   "Rollback procedure → Step 4.9").
#
# NOTE: `cargo binstall` downloads a prebuilt release binary when the crate publishes
# one for the host triple and falls back to `cargo install` (source build) otherwise,
# so every entry works on both x86_64 and aarch64 (pironman01 is a Pi 5).

DEV_TOOLCHAIN_CARGO_PKGS=(
  "cargo-nextest|cargo-nextest"   # test runner (harness-design lefthook + gate)
  "cargo-llvm-cov|cargo-llvm-cov" # coverage
  "cargo-deny|cargo-deny"         # dependency/licence/advisory linting
  "cargo-machete|cargo-machete"   # unused-dependency detection
  "typos-cli|typos"               # spell check (crate name != binary name)
  "cargo-sort|cargo-sort"         # Cargo.toml key ordering
  "cargo-release|cargo-release"   # release automation
  "cocogitto|cog"                 # conventional-commit linting (crate name != binary name)
)

# ---------------------------------------------------------------------------
# gitleaks — installed from a pinned GitHub release archive, not from crates.io
# ---------------------------------------------------------------------------
# gitleaks is a Go binary, so it has no cargo package. It is downloaded from the
# GitHub release matching GITLEAKS_VERSION and installed to
# <agent home>/.local/bin/gitleaks.
#
# To BUMP the pinned version:
#   1. Check https://github.com/gitleaks/gitleaks/releases for the newest tag.
#   2. Edit GITLEAKS_VERSION below (bare version, NO leading "v" — the "v" only
#      appears in the tag segment of GITLEAKS_URL_TEMPLATE).
#   3. Re-run setup-dispatch-host.sh (Step 4.9) or ./deploy.sh on every host. Both
#      compare the pinned string against `gitleaks version` and re-install on drift.
# This follows the talos-update.sh precedent: an explicit, bumpable pinned version
# recorded in the repo — never an unrecorded "latest".
GITLEAKS_VERSION="8.30.1"

# Release asset URL. Placeholders {version} and {arch} are substituted by the caller.
# Real example: .../download/v8.30.1/gitleaks_8.30.1_linux_arm64.tar.gz
GITLEAKS_URL_TEMPLATE="https://github.com/gitleaks/gitleaks/releases/download/v{version}/gitleaks_{version}_linux_{arch}.tar.gz"

# `uname -m` → gitleaks release-asset arch token, as "<uname -m>|<asset token>".
# gitleaks names its linux assets x64/arm64 (NOT x86_64/aarch64), hence the mapping.
# Only the two architectures the fleet runs are listed; anything else is reported as
# unsupported and gitleaks is skipped (non-fatal) rather than guessed at.
GITLEAKS_ARCH_MAP=(
  "x86_64|x64"     # r7-research, r7-server
  "aarch64|arm64"  # pironman01 (Raspberry Pi 5)
)
