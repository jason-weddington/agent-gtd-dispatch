"""Tests for the wave manager allowlist classifier."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_gtd_dispatch.wave_manager.classifier import (
    AllowlistRule,
    classify,
    load_allowlist,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RULE_PY = AllowlistRule(
    name="py-rule",
    description="Covers Python files",
    comment_patterns=[],
    diff_path_patterns=["*.py"],
)

_RULE_ANY = AllowlistRule(
    name="any-rule",
    description="Covers any file",
    comment_patterns=[],
    diff_path_patterns=[],
)

_RULE_FORMAT = AllowlistRule(
    name="format-rule",
    description="Covers formatting changes to Python files",
    comment_patterns=["(?i)format"],
    diff_path_patterns=["*.py"],
)

_DIFF_PY = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)
_DIFF_PY_AND_README = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


# ---------------------------------------------------------------------------
# classify() tests
# ---------------------------------------------------------------------------


class TestClassifyEmptyRulesHalts:
    def test_empty_rules_halts(self) -> None:
        result = classify("some comment", _DIFF_PY, [])
        assert result.action == "halt"
        assert "empty" in result.halt_reason


class TestClassifyEmptyDiffHalts:
    def test_empty_diff_halts(self) -> None:
        result = classify("some comment", "", [_RULE_PY])
        assert result.action == "halt"
        assert "empty diff" in result.halt_reason


class TestClassifyAllFilesCovered:
    def test_all_files_covered_allows(self) -> None:
        result = classify("some comment", _DIFF_PY, [_RULE_PY])
        assert result.action == "allow"
        assert result.matched_rules == ["py-rule"]
        assert result.halt_reason == ""


class TestClassifyUncoveredFileHalts:
    def test_uncovered_file_halts(self) -> None:
        result = classify("some comment", _DIFF_PY_AND_README, [_RULE_PY])
        assert result.action == "halt"
        assert "README.md" in result.halt_reason
        assert "uncovered" in result.halt_reason


class TestClassifyCommentPatternNotMatched:
    def test_comment_pattern_not_matched(self) -> None:
        # Rule requires "format" in the comment, but comment is unrelated
        result = classify("Fixed a bug in auth", _DIFF_PY, [_RULE_FORMAT])
        assert result.action == "halt"
        assert "uncovered" in result.halt_reason


class TestClassifyEmptyCommentPatternsMatchesAny:
    def test_empty_comment_patterns_matches_any(self) -> None:
        rule = AllowlistRule(
            name="no-comment-filter",
            description="Matches any comment",
            comment_patterns=[],
            diff_path_patterns=["*.py"],
        )
        result = classify("completely unrelated comment xyz", _DIFF_PY, [rule])
        assert result.action == "allow"
        assert result.matched_rules == ["no-comment-filter"]


class TestClassifyEmptyPathPatternsMatchesAny:
    def test_empty_path_patterns_matches_any(self) -> None:
        rule = AllowlistRule(
            name="no-path-filter",
            description="Matches any file path",
            comment_patterns=[],
            diff_path_patterns=[],
        )
        result = classify("comment", _DIFF_PY_AND_README, [rule])
        assert result.action == "allow"
        assert result.matched_rules == ["no-path-filter"]


class TestClassifyMultipleRulesSecondCovers:
    def test_multiple_rules_second_covers(self) -> None:
        rule_first = AllowlistRule(
            name="first-rule",
            description="Covers nothing useful (mismatched glob)",
            comment_patterns=[],
            diff_path_patterns=["*.ts"],
        )
        rule_second = AllowlistRule(
            name="second-rule",
            description="Covers Python files",
            comment_patterns=[],
            diff_path_patterns=["*.py"],
        )
        result = classify("comment", _DIFF_PY, [rule_first, rule_second])
        assert result.action == "allow"
        assert result.matched_rules == ["second-rule"]
        assert "first-rule" not in result.matched_rules


class TestClassifyHaltReasonMentionsUncoveredPath:
    def test_halt_reason_mentions_uncovered_path(self) -> None:
        result = classify("comment", _DIFF_PY_AND_README, [_RULE_PY])
        assert result.action == "halt"
        assert "README.md" in result.halt_reason


# ---------------------------------------------------------------------------
# load_allowlist() tests
# ---------------------------------------------------------------------------


class TestLoadAllowlistEmptyRules:
    def test_load_allowlist_empty_rules(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "allowlist.yaml"
        yaml_file.write_text("rules: []\n", encoding="utf-8")
        result = load_allowlist(yaml_file)
        assert result == []


class TestLoadAllowlistWithRule:
    def test_load_allowlist_with_rule(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "allowlist.yaml"
        yaml_file.write_text(
            "rules:\n"
            "  - name: lint-fix\n"
            "    description: Auto-fixed lint errors\n"
            "    comment_patterns:\n"
            "      - '(?i)lint'\n"
            "    diff_path_patterns:\n"
            "      - '*.py'\n",
            encoding="utf-8",
        )
        rules = load_allowlist(yaml_file)
        assert len(rules) == 1
        rule = rules[0]
        assert isinstance(rule, AllowlistRule)
        assert rule.name == "lint-fix"
        assert rule.description == "Auto-fixed lint errors"
        assert rule.comment_patterns == ["(?i)lint"]
        assert rule.diff_path_patterns == ["*.py"]


class TestLoadAllowlistDefaultsOptionalFields:
    def test_load_allowlist_defaults_optional_fields(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "allowlist.yaml"
        yaml_file.write_text(
            "rules:\n"
            "  - name: minimal-rule\n"
            "    description: A rule with no optional fields\n",
            encoding="utf-8",
        )
        rules = load_allowlist(yaml_file)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.comment_patterns == []
        assert rule.diff_path_patterns == []


class TestLoadAllowlistFileNotFound:
    def test_load_allowlist_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_allowlist(tmp_path / "nonexistent.yaml")


class TestLoadAllowlistInvalidYaml:
    def test_load_allowlist_invalid_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "allowlist.yaml"
        yaml_file.write_text("rules: [\nbroken yaml {{{\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_allowlist(yaml_file)


class TestLoadAllowlistMissingRequiredField:
    def test_load_allowlist_missing_required_field(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "allowlist.yaml"
        yaml_file.write_text(
            "rules:\n"
            "  - description: Missing name field\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required field 'name'"):
            load_allowlist(yaml_file)
