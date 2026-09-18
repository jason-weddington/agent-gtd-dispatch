#!/usr/bin/env python3
"""Pin the protocol dependency to the version being released.

Run from `build_command` in [tool.semantic_release], i.e. AFTER semantic-release
has written the new version into both `pyproject.toml:project.version` and
`packages/protocol/pyproject.toml:project.version` (they are bumped in lockstep
by `version_toml`), and BEFORE `uv build`. The resulting wheel therefore declares
`agent-gtd-dispatch-protocol==<this release's version>`.

WHY THIS EXISTS (2026-09-18). The protocol package carried a hand-written
`version = "0.1.0"` that nothing ever bumped, and the dependency was unpinned.
`release.sh` republished an identical 0.1.0 wheel every release, hosts already
had 0.1.0 installed, and an unpinned requirement is satisfied by whatever is
already there — so a protocol change never reached a host. `RunStatus` is
re-exported from that package, so `RunStatus.already_satisfied` (added by the
completion contract) did not exist in the deployed enum, and both the
claude-code already_satisfied path and the talos exit-30 mapping raised
AttributeError in production while `/info` happily reported the new version.

An exact pin makes that failure LOUD at install time — uv must fetch the
matching protocol wheel or fail — instead of silent at runtime.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT_PYPROJECT = ROOT / "pyproject.toml"
PROTOCOL_PYPROJECT = ROOT / "packages" / "protocol" / "pyproject.toml"
DEP_NAME = "agent-gtd-dispatch-protocol"


def _version(path: pathlib.Path) -> str:
    return str(tomllib.loads(path.read_text())["project"]["version"])


def main() -> int:
    release_version = _version(ROOT_PYPROJECT)
    protocol_version = _version(PROTOCOL_PYPROJECT)

    if protocol_version != release_version:
        # version_toml should have bumped both in lockstep; if it did not, the
        # pin we are about to write would point at a wheel nobody publishes.
        print(
            f"error: protocol version {protocol_version!r} != release version "
            f"{release_version!r} — check `version_toml` in [tool.semantic_release]",
            file=sys.stderr,
        )
        return 1

    text = ROOT_PYPROJECT.read_text()
    # Match the dependency entry whether it is bare or already pinned.
    pattern = re.compile(rf'^(\s*)"{re.escape(DEP_NAME)}(?:==[^"]*)?",$', re.MULTILINE)
    pinned = f'\\1"{DEP_NAME}=={release_version}",'
    new_text, count = pattern.subn(pinned, text)

    if count != 1:
        print(
            f"error: expected exactly one {DEP_NAME} dependency entry in "
            f"{ROOT_PYPROJECT}, found {count}",
            file=sys.stderr,
        )
        return 1

    if new_text != text:
        ROOT_PYPROJECT.write_text(new_text)
    print(f"pinned {DEP_NAME}=={release_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
