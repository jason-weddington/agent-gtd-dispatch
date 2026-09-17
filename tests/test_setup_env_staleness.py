"""Tests for scripts/env-staleness-check.sh and its wiring into
setup-dispatch-host.sh (Step 6.5: service environment freshness).

The helper is a standalone, root-free, unit-testable comparator driven here
via subprocess.run against fixture files in tmp_path — the same
repo-file-reading precedent as tests/test_dispatch.py:365-399 (reading
templates/*.tmpl straight off disk rather than mocking).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "env-staleness-check.sh"
SETUP_SCRIPT = Path(__file__).parent.parent / "setup-dispatch-host.sh"

# Non-secret sentinels (not real credentials) so this file does not trip the
# gitleaks v8.24.3 pre-commit hook (.pre-commit-config.yaml:12-15).
_FIXTURE_VALUE = "fixture-value-not-a-secret"
_FIXTURE_STALE_VALUE = "fixture-value-stale"


def _run(env_file: Path, environ_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--env-file", str(env_file), "--environ", str(environ_file)],
        capture_output=True,
        text=True,
    )


def _write_environ(path: Path, pairs: dict[str, str]) -> None:
    """Write a NUL-separated KEY=VALUE dump — the /proc/<pid>/environ format."""
    data = "".join(f"{k}={v}\0" for k, v in pairs.items())
    path.write_bytes(data.encode())


class TestEnvStalenessCheckScript:
    def test_script_is_committed_executable(self) -> None:
        # Assert the mode git RECORDS (100755), not the working-tree mode: the
        # latter comes from the checking-out user's umask (022 -> 0755, 002 ->
        # 0775), so an exact-mode assert passes or fails depending on whose
        # machine runs it. An environment-dependent gate is a false verdict
        # waiting to happen in either direction (kb-03289).
        assert SCRIPT.exists()
        assert SCRIPT.stat().st_mode & 0o111, "script is not executable"
        entry = subprocess.run(
            ["git", "ls-files", "-s", "--", str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=SCRIPT.parent.parent,
        )
        assert entry.returncode == 0, entry.stderr
        assert entry.stdout.startswith("100755 "), entry.stdout

    def test_script_is_valid_bash(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_a_every_key_matches(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\nB=2\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1", "B": "2"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_b_one_value_changed(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\nB=2\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1", "B": "9"})
        result = _run(env_file, environ_file)
        assert result.returncode == 1
        assert result.stdout == "B\n"

    def test_c_key_absent_from_environ(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\nB=2\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1"})
        result = _run(env_file, environ_file)
        assert result.returncode == 1
        assert result.stdout == "B\n"

    def test_d_double_quoted_env_value_matches_unquoted_environ(
        self, tmp_path: Path
    ) -> None:
        env_file = tmp_path / "env"
        env_file.write_text('A="abc"\n')
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "abc"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_e_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("# a comment\n\n   \nA=1\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_f_duplicate_key_last_value_wins(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\nA=2\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "2"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_g_quote_normalization_pinned_case(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=\"'abc'\"\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "abc"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_g2_quote_normalization_other_pinned_case(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text('A="abc\n')
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "abc"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_h_backslash_value_skipped_not_stale(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=ab\\ncd\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "whatever"})
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "skipped (unsupported quoting): A" in result.stderr

    def test_i_two_stale_keys_sorted_output(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("B=2\nA=1\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"B": "9", "A": "9"})
        result = _run(env_file, environ_file)
        assert result.returncode == 1
        assert result.stdout == "A\nB\n"

    def test_j_environ_only_keys_never_reported(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\n")
        environ_file = tmp_path / "environ"
        _write_environ(
            environ_file,
            {"PATH": "/usr/bin", "INVOCATION_ID": "deadbeef", "A": "1"},
        )
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_k_value_containing_equals_matches_verbatim(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("KB_TEST_DATABASE_URL=postgresql:///postgres?a=1&b=2\n")
        environ_file = tmp_path / "environ"
        _write_environ(
            environ_file,
            {"KB_TEST_DATABASE_URL": "postgresql:///postgres?a=1&b=2"},
        )
        result = _run(env_file, environ_file)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_k2_value_containing_equals_partial_match_is_stale(
        self, tmp_path: Path
    ) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("X=a=b\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"X": "a"})
        result = _run(env_file, environ_file)
        assert result.returncode == 1
        assert result.stdout == "X\n"

    def test_l_missing_environ_path_exits_3(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\n")
        result = _run(env_file, tmp_path / "does-not-exist")
        assert result.returncode == 3
        assert "does-not-exist" in result.stderr
        assert "Usage:" not in result.stderr

    def test_l2_missing_env_file_path_exits_3(self, tmp_path: Path) -> None:
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1"})
        result = _run(tmp_path / "does-not-exist", environ_file)
        assert result.returncode == 3
        assert "does-not-exist" in result.stderr
        assert "Usage:" not in result.stderr

    def test_m_unknown_flag_exits_2_with_usage(self) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--bogus"], capture_output=True, text=True
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_m2_missing_required_option_exits_2_with_usage(
        self, tmp_path: Path
    ) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--env-file", str(tmp_path / "env")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_m3_no_args_exits_2_with_usage(self) -> None:
        result = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_n_no_secret_leak(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text(f"DISPATCH_API_KEY={_FIXTURE_VALUE}\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"DISPATCH_API_KEY": _FIXTURE_STALE_VALUE})
        result = _run(env_file, environ_file)
        assert result.returncode == 1
        assert result.stdout == "DISPATCH_API_KEY\n"
        assert _FIXTURE_VALUE not in result.stdout
        assert _FIXTURE_VALUE not in result.stderr
        assert _FIXTURE_STALE_VALUE not in result.stdout
        assert _FIXTURE_STALE_VALUE not in result.stderr

    def test_never_requires_root(self, tmp_path: Path) -> None:
        """The script must not shell out to anything requiring privilege —
        a plain unprivileged invocation exercising every code path must
        never fail because of missing permissions."""
        env_file = tmp_path / "env"
        env_file.write_text("A=1\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "1"})
        result = _run(env_file, environ_file)
        assert "permission denied" not in result.stderr.lower()

    def test_never_writes_a_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("A=1\n")
        environ_file = tmp_path / "environ"
        _write_environ(environ_file, {"A": "9"})
        before = {p.name for p in tmp_path.iterdir()}
        _run(env_file, environ_file)
        after = {p.name for p in tmp_path.iterdir()}
        assert before == after


class TestSetupScriptWiring:
    """Reads setup-dispatch-host.sh from disk (it is not sourced or executed
    — that would require root) and asserts the wiring invariants that make
    Step 6.5 reachable and correctly connected to the five env-mutation call
    sites and the corrected Step 3.5 banner copy."""

    @staticmethod
    def _text() -> str:
        return SETUP_SCRIPT.read_text()

    def test_step_6_5_header_present(self) -> None:
        assert "--- Step 6.5: Service environment freshness ---" in self._text()

    def test_restart_if_stale_flag_documented_and_wired(self) -> None:
        text = self._text()
        assert text.count("--restart-if-stale") >= 3

    def test_note_env_mutation_call_sites(self) -> None:
        text = self._text()
        for call in (
            "_note_env_mutation '<entire file>'",
            "_note_env_mutation DISPATCH_AGENT_SUBPROCESS_USER",
            "_note_env_mutation DISPATCH_API_KEY",
            "_note_env_mutation TALOS_BIN",
            '_note_env_mutation "$_name"',
        ):
            assert call in text, f"missing call site: {call}"

    def test_stale_rc_captured_without_set_e_abort(self) -> None:
        assert "|| _stale_rc=$?" in self._text()

    def test_false_step_6_restart_copy_removed(self) -> None:
        text = self._text()
        assert "BEFORE Step 6 restarts the service" not in text
        assert "Step 6 will do this" not in text

    def test_all_nine_decision_literals_present(self) -> None:
        text = self._text()
        for literal in (
            "current",
            "not-running",
            "dry-run",
            "unverified",
            "restarted-verified",
            "restarted-still-stale",
            "refused-no-flag",
            "refused-in-flight",
            "refused-probe-failed",
        ):
            assert text.count(literal) >= 1, f"missing decision literal: {literal}"

    def test_defaults_declared(self) -> None:
        text = self._text()
        assert "RESTART_IF_STALE=false" in text
        assert "ENV_MUTATED_VARS=()" in text
        assert 'ENV_FRESHNESS_STATE="unchecked"' in text

    def test_no_hardcoded_path_or_port_in_step_6_5(self) -> None:
        """Every operator-facing message in Step 6.5 must interpolate
        ${SERVICE_ENV} / ${SERVICE_NAME} / ${API_PORT} rather than hardcode
        a path or port — this repo runs on hosts with different homes,
        service names, and ports (single-user vs two-user mode)."""
        text = self._text()
        start = text.index("--- Step 6.5: Service environment freshness ---")
        end = text.index("# Step 7: Health check")
        block = text[start:end]
        assert "/home/dispatch-svc" not in block
        assert "localhost:8100" not in block
        assert "restart dispatch-api" not in block

    def test_summary_prints_env_freshness(self) -> None:
        assert 'echo "  Env freshness: ${ENV_FRESHNESS_STATE}"' in self._text()

    def test_bash_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_help_exits_0_and_documents_restart_if_stale(self) -> None:
        # The --help arm runs before the root guard, so no sudo is needed.
        result = subprocess.run(
            [str(SETUP_SCRIPT), "--help"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "--restart-if-stale" in result.stdout
