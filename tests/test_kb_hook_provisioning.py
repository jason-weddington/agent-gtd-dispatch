"""Tests for the personal-kb-hook provisioning step (setup-dispatch-host.sh Step 4.10).

Two concerns:
  1. Text guards over setup-dispatch-host.sh: the step is present, follows the
     documented idiom, references the hook binary by absolute path, carries the
     exact PostToolUse matcher and --format=claude-json flag, uses $(cat ...)
     key indirection, reads PERSONAL_KB_HOOK_SRC from the service env, and is
     deliberately unpinned (unlike PERSONAL_KB_MCP_SRC).
  2. A hermetic exercise of the settings-merge logic in
     templates/personal-kb-hook-settings.py: pre-existing settings survive,
     re-running yields exactly four personal-kb-hook blocks (never eight), and
     the PostToolUse matcher is exact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SETUP_SCRIPT = REPO_ROOT / "setup-dispatch-host.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"
MERGE_SCRIPT = REPO_ROOT / "templates" / "personal-kb-hook-settings.py"

POST_TOOL_USE_MATCHER = "mcp__personal-kb__kb_get|mcp__team-kb__team_kb_get"
HOOK_BIN = "/home/dispatch/.local/bin/personal-kb-hook"
KEY_FILE = "/home/dispatch/.personal_kb_hook_key"


def _run_merge(settings_path: Path, url: str = "https://kb.example") -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(MERGE_SCRIPT),
            str(settings_path),
            HOOK_BIN,
            KEY_FILE,
            url,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


class TestTextGuards:
    def test_setup_script_has_step_4_10(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'echo "--- Step 4.10: personal-kb-hook (agent user) ---"' in text

    def test_install_is_unpinned_and_forced(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert "uv tool install --force --from '${_hook_src}' personal-kb-hook" in text
        # The default source carries no @<sha> pin, unlike PERSONAL_KB_MCP_SRC.
        assert (
            "PERSONAL_KB_HOOK_SRC_DEFAULT="
            '"git+ssh://git@ubuntu-vm01/home/git/repos/personal_kb'
            '#subdirectory=packages/personal-kb-hook"' in text
        )

    def test_hook_src_read_from_service_env(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'PERSONAL_KB_HOOK_SRC="$(_read_env_var PERSONAL_KB_HOOK_SRC)"' in text

    def test_absolute_binary_path(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'HOOK_BIN="${AGENT_HOME}/.local/bin/personal-kb-hook"' in text

    def test_key_file_path_and_permissions(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'HOOK_KEY_FILE="${AGENT_HOME}/.personal_kb_hook_key"' in text
        assert "install -m 0600" in text

    def test_silent_degradation_warning_verbatim(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert (
            'warn "PERSONAL_KB_API_KEY not set in ${SERVICE_ENV} — personal-kb-hook '
            "will be installed but not wired; it degrades SILENTLY (no roster, no "
            "telemetry, no error), so this would look exactly like a working "
            'install"' in text
        )

    def test_no_separate_hook_key_minted(self) -> None:
        text = SETUP_SCRIPT.read_text()
        # The hook must reuse PERSONAL_KB_API_KEY, never mint its own var.
        assert "PERSONAL_KB_HOOK_API_KEY" not in text
        assert "PERSONAL_KB_API_KEY" in text

    def test_verification_runs_as_agent_user(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'runuser -l "$AGENT_USER" -c "\'${HOOK_BIN}\' --help"' in text

    def test_dry_run_would_lines(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'would "runuser -l ${AGENT_USER} -- uv tool install --force' in text
        assert 'would "write ${HOOK_KEY_FILE}' in text
        assert 'would "merge SessionStart/UserPromptSubmit/Stop/PostToolUse' in text

    def test_no_restart_in_step(self) -> None:
        # Step 4.10 spans from its own banner to the Step 5a banner; nothing in
        # between may restart the service.
        text = SETUP_SCRIPT.read_text()
        start = text.index("Step 4.10: personal-kb-hook (agent user)")
        end = text.index("Step 5a: Claude symlink")
        step_text = text[start:end]
        assert "systemctl restart" not in step_text

    def test_deploy_script_refresh(self) -> None:
        text = DEPLOY_SCRIPT.read_text()
        assert (
            "uv tool install --force --from '${PERSONAL_KB_HOOK_SRC}' personal-kb-hook"
            in text
        )
        assert "[OK]   personal-kb-hook" in text
        assert "[WARN] personal-kb-hook install failed" in text
        # Deploy only refreshes the binary — wiring is setup-dispatch-host.sh's job.
        assert "personal-kb-hook-settings.py" not in text
        assert "Does NOT touch settings.json" in text


class TestSettingsMerge:
    def test_preserves_other_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "hooks": {
                        "PostToolUse": [
                            {
                                "matcher": "SomeOtherTool",
                                "hooks": [{"type": "command", "command": "echo hi"}],
                            }
                        ]
                    },
                }
            )
        )
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        assert data["theme"] == "dark"
        post_tool_use = data["hooks"]["PostToolUse"]
        assert any(e.get("matcher") == "SomeOtherTool" for e in post_tool_use)

    def test_creates_file_as_empty_object_when_absent(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        assert not settings_path.exists()
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        assert "hooks" in data

    def test_four_blocks_after_one_run(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        count = sum(
            1
            for entries in data["hooks"].values()
            for e in entries
            if any(
                "personal-kb-hook" in h.get("command", "") for h in e.get("hooks", [])
            )
        )
        assert count == 4

    def test_still_four_blocks_after_two_runs(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        count = sum(
            1
            for entries in data["hooks"].values()
            for e in entries
            if any(
                "personal-kb-hook" in h.get("command", "") for h in e.get("hooks", [])
            )
        )
        assert count == 4
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "PostToolUse"):
            assert len(data["hooks"][event]) == 1

    def test_post_tool_use_matcher_exact(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        matchers = [e.get("matcher") for e in data["hooks"]["PostToolUse"]]
        assert matchers == [POST_TOOL_USE_MATCHER]

    def test_no_matcher_events_have_none(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            assert "matcher" not in data["hooks"][event][0]

    def test_key_indirection_no_literal_key_value(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        # The merge script never receives the literal key — only a path — but
        # assert the rendered command uses $(cat ...) indirection and that a
        # plausible key-shaped literal never appears in the file.
        _run_merge(settings_path)
        rendered = settings_path.read_text()
        assert "$(cat" in rendered
        assert "kb_realsecretvalue12345" not in rendered

    def test_command_uses_absolute_binary_path_and_format_flag(
        self, tmp_path: Path
    ) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert command.startswith("PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL=")
        assert HOOK_BIN in command
        assert command.endswith("--format=claude-json")
        assert f"$(cat {KEY_FILE} 2>/dev/null)" in command
