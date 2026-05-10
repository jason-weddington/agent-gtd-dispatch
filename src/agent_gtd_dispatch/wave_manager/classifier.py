"""Wave manager allowlist classifier.

Loads allowlist.yaml and classifies completed agent runs against it,
deciding whether the executor may auto-approve a merge or must HALT.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class AllowlistRule:
    """A single rule in the wave manager allowlist.

    Attributes:
        name: Unique rule identifier.
        description: Human-readable reason why this pattern is safe to auto-merge.
        comment_patterns: Python regexes; ALL must match the agent's final GTD comment.
            Empty list means "match any comment".
        diff_path_patterns: fnmatch globs; EVERY changed file path must match at least
            one pattern for the rule to apply. Empty list means "match any file path".
    """

    name: str
    description: str
    comment_patterns: list[str] = field(default_factory=list)
    diff_path_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClassifierResult:
    """Result of classifying an agent run against the allowlist.

    Attributes:
        action: "allow" if all changed files are covered; "halt" otherwise.
        matched_rules: Rule names that covered the diff; empty if action="halt".
        halt_reason: Human-readable reason if action="halt"; empty string if allow.
    """

    action: Literal["allow", "halt"]
    matched_rules: list[str]
    halt_reason: str


def load_allowlist(path: Path) -> list[AllowlistRule]:
    """Load and parse allowlist.yaml.

    Args:
        path: Path to the allowlist YAML file.

    Returns:
        List of AllowlistRule instances; empty list if rules: [] in the file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or a rule entry is missing required fields.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"Failed to parse allowlist YAML: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(data, dict):
        msg = "allowlist.yaml must be a YAML mapping with a 'rules' key"
        raise ValueError(msg)

    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        msg = "'rules' must be a list"
        raise ValueError(msg)

    rules: list[AllowlistRule] = []
    for i, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            msg = f"Rule entry {i} must be a mapping"
            raise ValueError(msg)
        if "name" not in entry:
            msg = f"Rule entry {i} is missing required field 'name'"
            raise ValueError(msg)
        if "description" not in entry:
            name_repr = repr(entry.get("name"))
            msg = (
                f"Rule entry {i} (name={name_repr}) is missing required field"
                " 'description'"
            )
            raise ValueError(msg)
        rules.append(
            AllowlistRule(
                name=entry["name"],
                description=entry["description"],
                comment_patterns=list(entry.get("comment_patterns") or []),
                diff_path_patterns=list(entry.get("diff_path_patterns") or []),
            )
        )

    return rules


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
            # Extract the path after "a/"
            rest = line[len("diff --git a/") :]
            # The path ends before " b/" — find the last occurrence of " b/"
            idx = rest.rfind(" b/")
            if idx != -1:
                paths.append(rest[:idx])
            else:
                # Fallback: take everything after "a/"
                paths.append(rest)
    return paths


def _rule_covers_path(rule: AllowlistRule, comment: str, path: str) -> bool:
    """Check whether a rule covers a single changed file path.

    Args:
        rule: The allowlist rule to test.
        comment: The agent's final GTD comment text.
        path: A changed file path from the diff.

    Returns:
        True if the rule covers the path (both comment and path conditions satisfied).
    """
    # Check comment patterns — ALL must match
    if rule.comment_patterns:
        for pattern in rule.comment_patterns:
            if not re.search(pattern, comment):
                return False

    # Check diff path patterns — at least ONE must match
    return not rule.diff_path_patterns or any(
        fnmatch.fnmatch(path, glob) for glob in rule.diff_path_patterns
    )


def classify(comment: str, diff: str, rules: list[AllowlistRule]) -> ClassifierResult:
    """Classify a completed agent run against the allowlist.

    Args:
        comment: The agent's final GTD comment text.
        diff: The unified diff of the agent's changes (e.g. ``git diff main...HEAD``).
        rules: Loaded allowlist rules.

    Returns:
        ClassifierResult with action="allow" if all changed files are covered by at
        least one rule, or action="halt" if any file is uncovered, the diff is empty,
        or the allowlist is empty.
    """
    changed_paths = _parse_diff_paths(diff)

    if not changed_paths:
        return ClassifierResult(
            action="halt",
            matched_rules=[],
            halt_reason="empty diff — agent made no changes",
        )

    if not rules:
        return ClassifierResult(
            action="halt",
            matched_rules=[],
            halt_reason="allowlist is empty — executor halts on any non-clean merge",
        )

    uncovered: list[str] = []
    covering_rule_names: set[str] = set()

    for path in changed_paths:
        covered = False
        for rule in rules:
            if _rule_covers_path(rule, comment, path):
                covering_rule_names.add(rule.name)
                covered = True
        if not covered:
            uncovered.append(path)

    if uncovered:
        uncovered_str = ", ".join(repr(p) for p in uncovered)
        return ClassifierResult(
            action="halt",
            matched_rules=[],
            halt_reason=(
                f"uncovered files not matched by any allowlist rule: {uncovered_str}"
            ),
        )

    return ClassifierResult(
        action="allow",
        matched_rules=sorted(covering_rule_names),
        halt_reason="",
    )
