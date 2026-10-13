"""Guard: no tracked file names the maintainer's home-lab infrastructure.

This repo is mirrored publicly. Hosts, LAN addresses, the private domain and
private git URLs belong in the maintainer's deploy glue (invoked through the
gitignored ``release.local.sh`` hook), and this repo's scripts take them from
env vars. CHANGELOG.md is generated release history and is exempt.

The pattern is assembled from fragments so that this file does not match it
itself; any other test that wants to assert "no lab host leaks into X" imports
``LAB_PATTERN`` from here rather than spelling host names out.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_FRAGMENTS = (
    "jason" + "weddington",
    r"192\." + r"168\.",
    "ubuntu" + "-vm01",
    "piron" + "man",
    "r7" + "-research",
    "r7" + "-server",
    "jason" + "-precision",
    r"\bpi" + r"-0[0-9]\b",
)
LAB_PATTERN = re.compile("|".join(_FRAGMENTS))

EXEMPT = {"CHANGELOG.md"}


def _tracked_files() -> list[str] | None:
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode().split("\0") if p]


def test_pattern_matches_what_it_should() -> None:
    # Keep the guard honest: each fragment must still catch its target.
    for sample in (
        "pi" + "-04",
        "piron" + "man01",
        "http://192" + ".168.1.5",
        "kb.lab.jason" + "weddington.com",
    ):
        assert LAB_PATTERN.search(sample), sample
    for sample in ("api-04", "pi-0x", "git.example.com", "host-a"):
        assert not LAB_PATTERN.search(sample), sample


def test_no_tracked_file_names_lab_infrastructure() -> None:
    files = _tracked_files()
    if files is None:
        pytest.skip("not a git checkout")
    hits: list[str] = []
    for rel in files:
        if rel in EXEMPT:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if LAB_PATTERN.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not hits, "lab-specific references in tracked files:\n" + "\n".join(hits)
