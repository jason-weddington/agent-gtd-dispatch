"""Tests for the personal-kb-hook provisioning step (setup-dispatch-host.sh Step 4.10).

Two concerns:
  1. Text guards over setup-dispatch-host.sh: the step is present, follows the
     documented idiom, references the hook binary by absolute path, carries the
     exact PostToolUse matcher and --format=claude-json flag, uses $(cat ...)
     key indirection, reads PERSONAL_KB_HOOK_SRC from the service env, and is
     deliberately unpinned.
  2. A hermetic exercise of the settings-merge logic in
     templates/personal-kb-hook-settings.py: all seven events are wired with the
     canonical matchers/timeouts, pre-existing settings survive, re-running is
     byte-identical (exactly seven personal-kb-hook blocks, never fourteen), and
     deploy.sh re-applies the wiring on every host it deploys.
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
KB_URL = "https://kb.example.com"
# The seven wired events with their canonical matcher and timeout
# (upstream personal-kb-hook wiring; None means no "timeout" key).
EXPECTED_EVENTS: dict[str, tuple[str, int | None]] = {
    "SessionStart": ("", None),
    "UserPromptSubmit": ("", None),
    "PreToolUse": ("Bash", 5),
    "PostToolUse": (POST_TOOL_USE_MATCHER, None),
    "PostToolUseFailure": ("", 5),
    "Stop": ("", None),
    "SubagentStop": ("", None),
}


def _hook_blocks(data: dict) -> list[tuple[str, dict]]:
    """Return (event, block) pairs whose command mentions personal-kb-hook."""
    blocks = []
    for event, entries in data.get("hooks", {}).items():
        for entry in entries:
            if any(
                "personal-kb-hook" in h.get("command", "")
                for h in entry.get("hooks", [])
            ):
                blocks.append((event, entry))
    return blocks


def _run_merge(settings_path: Path, url: str = KB_URL) -> None:
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
    assert "kb_realsecretvalue12345" not in settings_path.read_text()


class TestTextGuards:
    def test_setup_script_has_step_4_10(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert 'echo "--- Step 4.10: personal-kb-hook (agent user) ---"' in text

    def test_install_is_unpinned_and_forced(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert "uv tool install --force --from '${_hook_src}' personal-kb-hook" in text
        # No baked-in source: the deployment supplies it (and pins nothing).
        assert "PERSONAL_KB_HOOK_SRC_DEFAULT" not in text
        assert '_hook_src="${PERSONAL_KB_HOOK_SRC:-}"' in text

    def test_hook_src_read_from_service_env(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert '_v="$(_read_env_var PERSONAL_KB_HOOK_SRC)"' in text

    def test_step_skipped_loudly_when_unconfigured(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert '_hook_missing="${_hook_missing} PERSONAL_KB_HOOK_SRC"' in text
        assert '_hook_missing="${_hook_missing} PERSONAL_KB_URL"' in text
        assert 'warn "SKIPPING personal-kb-hook:' in text

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
        assert (
            'would "merge SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/'
            "PostToolUseFailure/Stop/SubagentStop personal-kb-hook blocks into "
            "${HOOK_SETTINGS}, preserving other settings" in text
        )

    def test_no_restart_in_step(self) -> None:
        # Step 4.10 spans from its own banner to the Step 5a banner; nothing in
        # between may restart the service.
        text = SETUP_SCRIPT.read_text()
        start = text.index("Step 4.10: personal-kb-hook (agent user)")
        end = text.index("Step 5a: Claude symlink")
        step_text = text[start:end]
        assert "systemctl restart" not in step_text

    def test_wired_info_line_names_seven_events(self) -> None:
        text = SETUP_SCRIPT.read_text()
        assert (
            "Wired personal-kb-hook into ${HOOK_SETTINGS} for ${AGENT_USER} "
            "(SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, "
            "PostToolUseFailure, Stop, SubagentStop) url=${_hook_url}" in text
        )

    def test_deploy_script_refresh(self) -> None:
        text = DEPLOY_SCRIPT.read_text()
        # Deploy never reinstalls the hook binary: production installs are pinned
        # to a release tag by the KB release, and a branch reinstall would undo it.
        assert "--from '${PERSONAL_KB_HOOK_SRC}'" not in text
        assert "PERSONAL_KB_HOOK_SRC" not in text
        # Deploy re-applies the wiring on every host it deploys — provision is
        # not the only writer, so hosts cannot drift.
        assert "personal-kb-hook-settings.py" in text
        assert "[OK]   personal-kb-hook wiring: 7 events" in text
        assert "[WARN] personal-kb-hook wiring skipped:" in text
        assert "Does NOT touch settings.json" not in text
        assert "sudo python3" in text


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

    # --- AC-7(a): from {} the merge produces exactly the seven canonical events ---

    def test_seven_blocks_after_one_run(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        blocks = _hook_blocks(data)
        assert len(blocks) == 7
        assert sorted(event for event, _ in blocks) == sorted(EXPECTED_EVENTS)
        for event, entry in blocks:
            matcher, timeout = EXPECTED_EVENTS[event]
            assert entry["matcher"] == matcher, event
            assert "timeout" not in entry, event  # never at entry level
            (hook,) = entry["hooks"]
            if timeout is None:
                assert "timeout" not in hook, event
            else:
                assert hook["timeout"] == timeout, event

    def test_no_async_key_anywhere(self, tmp_path: Path) -> None:
        # PreToolUse must stay synchronous so it can deny; PostToolUseFailure must
        # stay synchronous so its additionalContext lands on the same turn.
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        rendered = settings_path.read_text()
        assert "async" not in rendered

    # --- AC-7(b): only PostToolUseFailure carries the failure-cue prefix ---

    def test_failure_context_prefix_on_post_tool_use_failure_only(
        self, tmp_path: Path
    ) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        for event, entry in _hook_blocks(data):
            command = entry["hooks"][0]["command"]
            if event == "PostToolUseFailure":
                assert command.startswith("KB_FAILURE_CONTEXT=1 ")
                assert "PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL=" in command
            else:
                assert "KB_FAILURE_CONTEXT" not in command
                assert command.startswith("PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL=")

    # --- AC-7(c): old four-event wiring + hand-added blocks collapses to seven ---

    def test_replaces_old_wiring_and_hand_added_blocks(self, tmp_path: Path) -> None:
        def hook_command() -> str:
            return (
                f"PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL={KB_URL} "
                f"PERSONAL_KB_API_KEY=$(cat {KEY_FILE} 2>/dev/null) "
                f"{HOOK_BIN} --format=claude-json"
            )

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": hook_command()}]}
                        ],
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": hook_command()}]}
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": hook_command()}]}
                        ],
                        "PostToolUse": [
                            {
                                "matcher": POST_TOOL_USE_MATCHER,
                                "hooks": [
                                    {"type": "command", "command": hook_command()}
                                ],
                            }
                        ],
                        # A hand-added PreToolUse block with a different shape.
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": hook_command() + " extra-arg",
                                    }
                                ],
                            },
                            # An unrelated, non-hook PreToolUse block: must survive.
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo other"}],
                            },
                        ],
                    },
                }
            )
        )
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        blocks = _hook_blocks(data)
        assert len(blocks) == 7
        assert sorted(event for event, _ in blocks) == sorted(EXPECTED_EVENTS)
        assert any(
            h.get("command") == "echo other"
            for e in data["hooks"]["PreToolUse"]
            for h in e.get("hooks", [])
        )
        assert data["theme"] == "dark"

    # --- AC-7(d): running the merge twice is byte-identical ---

    def test_two_runs_are_byte_identical(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        first = settings_path.read_bytes()
        _run_merge(settings_path)
        second = settings_path.read_bytes()
        assert first == second
        data = json.loads(second)
        assert len(_hook_blocks(data)) == 7
        for event in EXPECTED_EVENTS:
            assert len(data["hooks"][event]) == 1

    # --- Command shape guards ---

    def test_post_tool_use_matcher_exact(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        _run_merge(settings_path)
        data = json.loads(settings_path.read_text())
        matchers = [e.get("matcher") for e in data["hooks"]["PostToolUse"]]
        assert matchers == [POST_TOOL_USE_MATCHER]

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
