"""Wave manager halt-list classifier.

Loads halt_list.yaml and classifies completed agent runs, deciding whether
the executor may auto-merge or must HALT for human review.

Default contract: allow unless a halt condition is met (empty diff, halt-list
file touched, or scope violation).  The lead's role is to handle halts, not to
review every merge.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


@dataclass(frozen=True)
class HaltPattern:
    """A single entry in the wave manager halt list.

    Attributes:
        name: Stable identifier for the pattern.
        description: Human-readable reason why this pattern needs eyes.
        file_patterns: fnmatch globs.  Any file match triggers a halt.
    """

    name: str
    description: str
    file_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClassifierResult:
    """Result of classifying an agent run.

    Attributes:
        action: "allow" to auto-merge; "halt" to require human review.
        halt_reason: Human-readable reason if action="halt"; empty string if allow.
        halted_files: Files that triggered the halt (halt-list hits or scope
            violations).  Empty list when action="allow".
    """

    action: Literal["allow", "halt"]
    halt_reason: str = ""
    halted_files: list[str] = field(default_factory=list)


# Mechanical files: always permitted even if not listed in declared_files.
# These are auto-generated or non-code artifacts whose presence never signals
# a meaningful scope violation.  If a project adds a new lockfile type later,
# add it here.
_MECHANICAL_FILES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "CHANGELOG.md",
    }
)


def load_halt_list(path: Path) -> list[HaltPattern]:
    """Load and parse halt_list.yaml.

    Args:
        path: Path to the halt list YAML file.

    Returns:
        List of HaltPattern instances; empty list if halt_patterns is empty or absent.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or an entry is missing required fields.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"Failed to parse halt_list YAML: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(data, dict):
        msg = "halt_list.yaml must be a YAML mapping with a 'halt_patterns' key"
        raise ValueError(msg)

    raw_patterns = data.get("halt_patterns") or []
    if not isinstance(raw_patterns, list):
        msg = "'halt_patterns' must be a list"
        raise ValueError(msg)

    patterns: list[HaltPattern] = []
    for i, entry in enumerate(raw_patterns):
        if not isinstance(entry, dict):
            msg = f"halt_patterns entry {i} must be a mapping"
            raise ValueError(msg)
        if "name" not in entry:
            msg = f"halt_patterns entry {i} is missing required field 'name'"
            raise ValueError(msg)
        if "description" not in entry:
            name_repr = repr(entry.get("name"))
            msg = (
                f"halt_patterns entry {i} (name={name_repr}) is missing required"
                " field 'description'"
            )
            raise ValueError(msg)
        patterns.append(
            HaltPattern(
                name=entry["name"],
                description=entry["description"],
                file_patterns=list(entry.get("file_patterns") or []),
            )
        )

    return patterns


def _parse_diff_paths(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff.

    Args:
        diff: The unified diff text (e.g. output of ``git diff main...HEAD``).

    Returns:
        List of file paths extracted from ``diff --git a/<path>`` lines.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git a/"):
            # Format: diff --git a/<path> b/<path>
            rest = line[len("diff --git a/") :]
            idx = rest.rfind(" b/")
            if idx != -1:
                paths.append(rest[:idx])
            else:
                paths.append(rest)
    return paths


def _pyproject_toml_is_ratchet_only(diff: str) -> bool:
    r"""Return True iff the pyproject.toml hunk only modifies ``fail_under``.

    Scans the pyproject.toml section of a unified diff and inspects every
    added/removed line.  Returns True if every such line is either blank or
    matches the ``fail_under = <number>`` coverage-ratchet pattern.  Any other
    change (new dependency, mypy config tweak, etc.) -> False.

    Examples::

        # Only fail_under bumped -> True
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "@@ -10 +10 @@\n"
            "-fail_under = 95.0\n"
            "+fail_under = 95.3\n"
        )
        _pyproject_toml_is_ratchet_only(diff)  # True

        # New dependency added -> False
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "@@ -5 +6 @@\n"
            '+anthropic = ">=0.40"\n'
        )
        _pyproject_toml_is_ratchet_only(diff)  # False

        # Both fail_under and new dep -> False
        _pyproject_toml_is_ratchet_only(both_diff)  # False

    Args:
        diff: Full unified diff text for the whole branch.

    Returns:
        True if the only changes in pyproject.toml are fail_under value bumps.
    """
    # Pattern: "fail_under = <number>" with optional surrounding whitespace.
    fail_under_re = re.compile(r"^\s*fail_under\s*=\s*[\d.]+\s*$")

    in_pyproject = False
    found_any_change = False

    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_pyproject = "pyproject.toml" in line
            continue

        if not in_pyproject:
            continue

        # Examine only added/removed lines (not context or diff-header lines).
        is_add = line.startswith("+") and not line.startswith("+++")
        is_del = line.startswith("-") and not line.startswith("---")
        if is_add or is_del:
            content = line[1:]
            found_any_change = True
            if not (content.strip() == "" or fail_under_re.match(content)):
                return False

    # Return True only if we saw actual change lines (vacuous True is meaningless).
    return found_any_change


def classify(
    comment: str,
    diff: str,
    declared_files: list[str],
    halt_patterns: list[HaltPattern],
) -> ClassifierResult:
    """Decide whether the build agent's output is safe to auto-merge.

    Decision logic (in order):

    1. Empty diff → halt, reason "empty diff — agent made no changes".
    2. For each changed file: if it matches any halt-pattern glob → halt,
       reason "halt-list file touched: <file> (rule: <name>)".  All halted
       files are collected into ``halted_files`` for the executor's comment.
    3. Pyproject.toml special case: if ``pyproject.toml`` is in the diff AND
       the diff for that file modifies ONLY the ``fail_under = <value>`` line
       (coverage ratchet bump), treat it as mechanical.  Otherwise it counts
       as a regular declared/undeclared file.
    4. For each changed file: if not in ``declared_files`` AND not in
       ``_MECHANICAL_FILES`` (after pyproject special-case) → halt, reason
       "scope violation: <file> not in declared Files to Modify".
    5. Otherwise → allow.

    Note: the agent's ``comment`` text is NOT used as a safety signal; it is
    accepted for API compatibility and logging context only.

    Args:
        comment: The agent's final GTD comment text (not a safety signal).
        diff: Unified diff of the agent's changes (``git diff main...HEAD``).
        declared_files: File paths from the item's "## Files to Modify" section.
        halt_patterns: Loaded halt-list patterns.

    Returns:
        ClassifierResult with action="allow" or action="halt".
    """
    changed_paths = _parse_diff_paths(diff)

    # Step 1: Empty diff.
    if not changed_paths:
        return ClassifierResult(
            action="halt",
            halt_reason="empty diff — agent made no changes",
            halted_files=[],
        )

    # Step 2: Halt-list check — scan every changed file.
    halted_files: list[str] = []
    halt_reasons: list[str] = []
    for path in changed_paths:
        for pattern in halt_patterns:
            if any(fnmatch.fnmatch(path, glob) for glob in pattern.file_patterns):
                halted_files.append(path)
                halt_reasons.append(
                    f"halt-list file touched: {path} (rule: {pattern.name})"
                )
                break  # one matching rule is enough for this file

    if halted_files:
        if len(halt_reasons) == 1:
            reason = halt_reasons[0]
        else:
            reason = "halt-list files touched: " + "; ".join(halt_reasons)
        return ClassifierResult(
            action="halt",
            halt_reason=reason,
            halted_files=halted_files,
        )

    # Step 3: Pyproject.toml ratchet special case.
    effective_mechanical: set[str] = set(_MECHANICAL_FILES)
    if "pyproject.toml" in changed_paths and _pyproject_toml_is_ratchet_only(diff):
        effective_mechanical.add("pyproject.toml")

    # Step 4: Scope violation check.
    declared_set = set(declared_files)
    scope_violations: list[str] = []
    for path in changed_paths:
        # Check by basename (e.g. "uv.lock") or by full path if someone
        # adds a path-qualified entry to _MECHANICAL_FILES in the future.
        basename = Path(path).name
        if basename in effective_mechanical or path in effective_mechanical:
            continue
        if path not in declared_set:
            scope_violations.append(path)

    if scope_violations:
        first = scope_violations[0]
        return ClassifierResult(
            action="halt",
            halt_reason=(
                f"scope violation: {first} not in declared Files to Modify"
            ),
            halted_files=scope_violations,
        )

    # Step 5: All checks passed.
    return ClassifierResult(
        action="allow",
        halt_reason="",
        halted_files=[],
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="classifier",
        description=(
            "Classify a completed build-agent run against the halt list. "
            "Outputs ALLOW or HALT:<reason> on stdout."
        ),
    )
    parser.add_argument(
        "--comment",
        required=True,
        help="Final agent comment text (string).",
    )
    parser.add_argument(
        "--diff",
        required=True,
        help="Unified-diff text (string or @path to read from file).",
    )
    parser.add_argument(
        "--declared-files",
        required=True,
        dest="declared_files",
        help="Comma-separated list of declared file paths from the item.",
    )
    parser.add_argument(
        "--wave-run-id",
        default="",
        dest="wave_run_id",
        help="Optional wave run ID for logging context.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint: classify and print ALLOW or HALT:<reason>."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Resolve diff: "@path" means read from file.
    diff_text: str = args.diff
    if diff_text.startswith("@"):
        diff_path = Path(diff_text[1:])
        diff_text = diff_path.read_text(encoding="utf-8")

    # Parse declared files (comma-separated, strip whitespace, drop empties).
    declared_files = [f.strip() for f in args.declared_files.split(",") if f.strip()]

    # Load halt list from config path.
    from agent_gtd_dispatch import config as _config

    halt_list_path = _config.WAVE_MANAGER_HALT_LIST_PATH
    try:
        halt_patterns = load_halt_list(halt_list_path)
    except FileNotFoundError:
        # If the halt list is missing, default to an empty list (allow by default).
        halt_patterns = []

    result = classify(args.comment, diff_text, declared_files, halt_patterns)

    if result.action == "allow":
        print("ALLOW")
    else:
        print(f"HALT:{result.halt_reason}")

    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
