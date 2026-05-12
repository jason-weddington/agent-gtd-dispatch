"""Tests for the wave manager halt-list classifier."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_gtd_dispatch.wave_manager.classifier import (
    HaltPattern,
    _pyproject_toml_is_ratchet_only,
    classify,
    load_halt_list,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_AUTH_PATTERN = HaltPattern(
    name="auth-code",
    description="Auth paths can fail-open silently.",
    file_patterns=["**/auth.py", "**/auth_routes.py"],
)

_DEPLOY_PATTERN = HaltPattern(
    name="deploy-and-release-scripts",
    description="Bad deploy scripts can lock out production.",
    file_patterns=["deploy.sh", "release.sh"],
)


def _make_diff(*paths: str) -> str:
    """Build a minimal unified diff touching the given file paths."""
    parts: list[str] = []
    for path in paths:
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
    return "".join(parts)


def _make_pyproject_diff(extra_lines: str = "") -> str:
    """Build a diff for pyproject.toml with a fail_under bump and optional extras."""
    base = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -10 +10 @@\n"
        " [tool.coverage.report]\n"
        "-fail_under = 95.0\n"
        "+fail_under = 95.3\n"
    )
    return base + extra_lines


# ---------------------------------------------------------------------------
# classify() — basic allow/halt
# ---------------------------------------------------------------------------


class TestAllowWhenAllFilesDeclaredAndNoHalt:
    def test_allow_when_all_files_declared_and_no_halt(self) -> None:
        diff = _make_diff("src/foo.py", "src/bar.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/foo.py", "src/bar.py"],
            halt_patterns=[],
        )
        assert result.action == "allow"
        assert result.halt_reason == ""
        assert result.halted_files == []


class TestAllowWhenMechanicalFilesAdded:
    def test_allow_when_mechanical_files_added(self) -> None:
        diff = _make_diff("src/foo.py", "uv.lock", "CHANGELOG.md")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/foo.py"],
            halt_patterns=[],
        )
        assert result.action == "allow"
        assert result.halt_reason == ""

    def test_all_lock_files_are_mechanical(self) -> None:
        diff = _make_diff(
            "src/foo.py", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"
        )
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/foo.py"],
            halt_patterns=[],
        )
        assert result.action == "allow"


# ---------------------------------------------------------------------------
# classify() — halt-list checks
# ---------------------------------------------------------------------------


class TestHaltOnHaltListMatch:
    def test_halt_on_halt_list_match(self) -> None:
        diff = _make_diff("src/agent_gtd/auth.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/agent_gtd/auth.py"],
            halt_patterns=[_AUTH_PATTERN],
        )
        assert result.action == "halt"
        assert "auth-code" in result.halt_reason
        assert "src/agent_gtd/auth.py" in result.halt_reason
        assert "halt-list" in result.halt_reason

    def test_halt_reason_mentions_rule_name(self) -> None:
        diff = _make_diff("deploy.sh")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["deploy.sh"],
            halt_patterns=[_DEPLOY_PATTERN],
        )
        assert result.action == "halt"
        assert "deploy-and-release-scripts" in result.halt_reason

    def test_halt_list_takes_priority_over_declared_files(self) -> None:
        # Even if the file is declared, halt-list overrides.
        diff = _make_diff("src/auth.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/auth.py"],
            halt_patterns=[_AUTH_PATTERN],
        )
        assert result.action == "halt"


class TestHaltOnHaltListMultipleFiles:
    def test_halt_on_halt_list_multiple_files(self) -> None:
        diff = _make_diff("src/auth.py", "deploy.sh")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/auth.py", "deploy.sh"],
            halt_patterns=[_AUTH_PATTERN, _DEPLOY_PATTERN],
        )
        assert result.action == "halt"
        assert "src/auth.py" in result.halted_files
        assert "deploy.sh" in result.halted_files

    def test_classify_returns_halted_files_list(self) -> None:
        diff = _make_diff("src/auth.py", "src/auth_routes.py", "deploy.sh")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/auth.py", "src/auth_routes.py", "deploy.sh"],
            halt_patterns=[_AUTH_PATTERN, _DEPLOY_PATTERN],
        )
        assert result.action == "halt"
        assert len(result.halted_files) == 3
        assert "src/auth.py" in result.halted_files
        assert "src/auth_routes.py" in result.halted_files
        assert "deploy.sh" in result.halted_files


# ---------------------------------------------------------------------------
# classify() — scope violation checks
# ---------------------------------------------------------------------------


class TestHaltOnScopeViolation:
    def test_halt_on_scope_violation(self) -> None:
        diff = _make_diff("src/foo.py", "src/sneaky.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/foo.py"],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "scope violation" in result.halt_reason
        assert "src/sneaky.py" in result.halt_reason

    def test_scope_violation_file_in_halted_files(self) -> None:
        diff = _make_diff("src/declared.py", "src/undeclared.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/declared.py"],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "src/undeclared.py" in result.halted_files

    def test_multiple_scope_violations_all_in_halted_files(self) -> None:
        diff = _make_diff("src/a.py", "src/b.py", "src/c.py")
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/a.py"],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "src/b.py" in result.halted_files
        assert "src/c.py" in result.halted_files


class TestHaltOnEmptyDiff:
    def test_halt_on_empty_diff(self) -> None:
        result = classify(
            comment="Done.",
            diff="",
            declared_files=["src/foo.py"],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "empty diff" in result.halt_reason

    def test_halt_on_diff_with_no_file_headers(self) -> None:
        # A diff with content but no "diff --git" lines → no changed paths → halt.
        result = classify(
            comment="Done.",
            diff="some random text without diff headers\n",
            declared_files=["src/foo.py"],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "empty diff" in result.halt_reason


# ---------------------------------------------------------------------------
# classify() — pyproject.toml ratchet special case
# ---------------------------------------------------------------------------


class TestPyprojectTomlRatchetOnlyIsMechanical:
    def test_pyproject_toml_ratchet_only_is_mechanical(self) -> None:
        diff = _make_pyproject_diff()
        result = classify(
            comment="Bumped coverage threshold.",
            diff=diff,
            declared_files=[],  # NOT in declared_files — mechanical exception applies
            halt_patterns=[],
        )
        assert result.action == "allow"

    def test_pyproject_toml_ratchet_alongside_declared_files(self) -> None:
        diff = _make_diff("src/foo.py") + _make_pyproject_diff()
        result = classify(
            comment="Done.",
            diff=diff,
            declared_files=["src/foo.py"],  # pyproject.toml not listed → mechanical
            halt_patterns=[],
        )
        assert result.action == "allow"


class TestPyprojectTomlDepChangeRequiresDeclaration:
    def test_pyproject_toml_dep_change_requires_declaration(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -5 +6 @@\n"
            '+anthropic = ">=0.40"\n'
        )
        result = classify(
            comment="Added anthropic dep.",
            diff=diff,
            declared_files=[],  # pyproject.toml not declared
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "scope violation" in result.halt_reason
        assert "pyproject.toml" in result.halt_reason

    def test_pyproject_toml_dep_change_allowed_when_declared(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -5 +6 @@\n"
            '+anthropic = ">=0.40"\n'
        )
        result = classify(
            comment="Added anthropic dep.",
            diff=diff,
            declared_files=["pyproject.toml"],
            halt_patterns=[],
        )
        assert result.action == "allow"

    def test_pyproject_both_ratchet_and_dep_requires_declaration(self) -> None:
        # Both fail_under + new dep: ratchet detection → False, so declaration needed.
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -10 +12 @@\n"
            "-fail_under = 95.0\n"
            "+fail_under = 95.3\n"
            '+anthropic = ">=0.40"\n'
        )
        result = classify(
            comment="Bumped coverage and added dep.",
            diff=diff,
            declared_files=[],
            halt_patterns=[],
        )
        assert result.action == "halt"
        assert "scope violation" in result.halt_reason


# ---------------------------------------------------------------------------
# _pyproject_toml_is_ratchet_only() unit tests
# ---------------------------------------------------------------------------


class TestPyprojectRatchetDetection:
    def test_only_fail_under_returns_true(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -10 +10 @@\n"
            "-fail_under = 95.0\n"
            "+fail_under = 95.3\n"
        )
        assert _pyproject_toml_is_ratchet_only(diff) is True

    def test_new_dep_returns_false(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -5 +6 @@\n"
            '+anthropic = ">=0.40"\n'
        )
        assert _pyproject_toml_is_ratchet_only(diff) is False

    def test_both_ratchet_and_dep_returns_false(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -10 +12 @@\n"
            "-fail_under = 95.0\n"
            "+fail_under = 95.3\n"
            '+anthropic = ">=0.40"\n'
        )
        assert _pyproject_toml_is_ratchet_only(diff) is False

    def test_pyproject_not_in_diff_returns_false(self) -> None:
        # No pyproject.toml → no changes found → found_any_change=False → False.
        diff = _make_diff("src/foo.py")
        assert _pyproject_toml_is_ratchet_only(diff) is False

    def test_context_lines_ignored(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -10 +10 @@\n"
            " [tool.coverage.report]\n"  # context line — should be ignored
            "-fail_under = 95.0\n"
            "+fail_under = 95.3\n"
        )
        assert _pyproject_toml_is_ratchet_only(diff) is True


# ---------------------------------------------------------------------------
# load_halt_list() tests
# ---------------------------------------------------------------------------


class TestLoadHaltListEmpty:
    def test_load_halt_list_empty(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text("halt_patterns: []\n", encoding="utf-8")
        result = load_halt_list(yaml_file)
        assert result == []

    def test_load_halt_list_absent_key_returns_empty(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text("{}\n", encoding="utf-8")
        result = load_halt_list(yaml_file)
        assert result == []


class TestLoadHaltListMissingFile:
    def test_load_halt_list_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_halt_list(tmp_path / "nonexistent.yaml")


class TestLoadHaltListMalformed:
    def test_load_halt_list_malformed(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text("halt_patterns: [\nbroken yaml {{{\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_halt_list(yaml_file)

    def test_load_halt_list_missing_name_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text(
            "halt_patterns:\n"
            "  - description: Missing name field\n"
            "    file_patterns: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required field 'name'"):
            load_halt_list(yaml_file)

    def test_load_halt_list_missing_description_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text(
            "halt_patterns:\n"
            "  - name: some-pattern\n"
            "    file_patterns: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required field 'description'"):
            load_halt_list(yaml_file)


class TestLoadHaltListWithPatterns:
    def test_load_halt_list_with_pattern(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text(
            "halt_patterns:\n"
            "  - name: auth-code\n"
            "    description: Auth paths.\n"
            "    file_patterns:\n"
            "      - '**/auth.py'\n"
            "      - '**/auth_routes.py'\n",
            encoding="utf-8",
        )
        patterns = load_halt_list(yaml_file)
        assert len(patterns) == 1
        p = patterns[0]
        assert isinstance(p, HaltPattern)
        assert p.name == "auth-code"
        assert p.description == "Auth paths."
        assert p.file_patterns == ["**/auth.py", "**/auth_routes.py"]

    def test_load_halt_list_optional_file_patterns_defaults_empty(
        self, tmp_path: Path
    ) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text(
            "halt_patterns:\n"
            "  - name: minimal\n"
            "    description: No file_patterns field.\n",
            encoding="utf-8",
        )
        patterns = load_halt_list(yaml_file)
        assert len(patterns) == 1
        assert patterns[0].file_patterns == []


class TestLoadHaltListEdgeCases:
    def test_top_level_not_mapping_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text("- just a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_halt_list(yaml_file)

    def test_halt_patterns_not_list_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text("halt_patterns: not-a-list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            load_halt_list(yaml_file)

    def test_entry_not_mapping_raises(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "halt_list.yaml"
        yaml_file.write_text(
            "halt_patterns:\n"
            "  - just a string entry\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must be a mapping"):
            load_halt_list(yaml_file)


# ---------------------------------------------------------------------------
# _parse_diff_paths() — edge cases
# ---------------------------------------------------------------------------


class TestParseDiffPaths:
    def test_no_b_path_fallback(self) -> None:
        # Unusual diff line without " b/" separator — use fallback.
        from agent_gtd_dispatch.wave_manager.classifier import _parse_diff_paths

        diff = "diff --git a/some/oddpath\n"
        paths = _parse_diff_paths(diff)
        assert paths == ["some/oddpath"]


# ---------------------------------------------------------------------------
# CLI / main() tests
# ---------------------------------------------------------------------------


class TestClassifierCLI:
    def test_cli_outputs_allow(self, tmp_path: Path, capsys, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        halt_list = tmp_path / "halt_list.yaml"
        halt_list.write_text("halt_patterns: []\n", encoding="utf-8")
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", halt_list)

        diff = _make_diff("src/foo.py")
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    diff,
                    "--declared-files",
                    "src/foo.py",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "ALLOW"

    def test_cli_outputs_halt_on_empty_diff(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        halt_list = tmp_path / "halt_list.yaml"
        halt_list.write_text("halt_patterns: []\n", encoding="utf-8")
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", halt_list)

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    "",
                    "--declared-files",
                    "src/foo.py",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip().startswith("HALT:")
        assert "empty diff" in captured.out

    def test_cli_outputs_halt_on_scope_violation(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        halt_list = tmp_path / "halt_list.yaml"
        halt_list.write_text("halt_patterns: []\n", encoding="utf-8")
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", halt_list)

        diff = _make_diff("src/undeclared.py")
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    diff,
                    "--declared-files",
                    "src/other.py",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "HALT:scope violation" in captured.out

    def test_cli_reads_diff_from_file(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        halt_list = tmp_path / "halt_list.yaml"
        halt_list.write_text("halt_patterns: []\n", encoding="utf-8")
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", halt_list)

        diff_file = tmp_path / "my.diff"
        diff_file.write_text(_make_diff("src/foo.py"), encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    f"@{diff_file}",
                    "--declared-files",
                    "src/foo.py",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "ALLOW"

    def test_cli_allows_when_halt_list_missing(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        nonexistent = tmp_path / "no_such_file.yaml"
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", nonexistent)

        diff = _make_diff("src/foo.py")
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    diff,
                    "--declared-files",
                    "src/foo.py",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "ALLOW"

    def test_cli_accepts_wave_run_id(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.wave_manager.classifier import main

        halt_list = tmp_path / "halt_list.yaml"
        halt_list.write_text("halt_patterns: []\n", encoding="utf-8")
        monkeypatch.setattr(config, "WAVE_MANAGER_HALT_LIST_PATH", halt_list)

        diff = _make_diff("src/foo.py")
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--comment",
                    "Done.",
                    "--diff",
                    diff,
                    "--declared-files",
                    "src/foo.py",
                    "--wave-run-id",
                    "wr-abc123",
                ]
            )
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "ALLOW"
