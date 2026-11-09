"""Tests for the personal-kb-hook RETIREMENT path.

Two concerns:
  1. templates/personal-kb-hook-prune.py: it drops matcher groups whose command
     references personal-kb-hook while preserving every other setting, event and
     group; it deletes only event keys it empties; it is idempotent and only
     writes when something was removed; and it fails cleanly on bad input.
  2. Text guards over deploy.sh and setup-dispatch-host.sh: both stage/invoke the
     prune script and no longer reference Step 4.10 or the old merge script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SETUP_SCRIPT = REPO_ROOT / "setup-dispatch-host.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"
PRUNE_SCRIPT = REPO_ROOT / "templates" / "personal-kb-hook-prune.py"

HOOK_COMMAND = (
    "PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL=https://kb.example.com "
    "PERSONAL_KB_API_KEY=$(cat /home/dispatch/.personal_kb_hook_key 2>/dev/null) "
    "/home/dispatch/.local/bin/personal-kb-hook --format=claude-json"
)
FOREIGN = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}


def _run(settings_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PRUNE_SCRIPT), str(settings_path)],
        capture_output=True,
        text=True,
    )


def _hook_group(matcher: str = "") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": HOOK_COMMAND}]}


class TestTextGuards:
    def test_deploy_stages_and_invokes_prune(self) -> None:
        d = DEPLOY_SCRIPT.read_text()
        assert "personal-kb-hook-prune.py" in d
        assert "personal-kb-hook-settings.py" not in d
        assert "_kb_url" not in d
        assert "_kb_key" not in d
        assert "PERSONAL_KB_URL" not in d
        assert "[SKIP] personal-kb-hook retirement: " in d
        assert "[OK]   personal-kb-hook retirement: " in d
        assert "[WARN] personal-kb-hook retirement failed: " in d
        assert "KB hooks" not in d

    def test_setup_retires_prune_step_4_10(self) -> None:
        setup = SETUP_SCRIPT.read_text()
        assert "personal-kb-hook-prune.py" in setup
        assert "personal-kb-hook-settings.py" not in setup
        assert "Step 4.10" not in setup
        assert "PERSONAL_KB_HOOK_SRC" not in setup
        assert "HOOK_BIN" not in setup
        assert "HOOK_KEY_FILE" not in setup
        assert "HOOK_MERGE_SCRIPT" not in setup
        assert 'skip "personal-kb-hook retirement:' in setup
        assert 'info "personal-kb-hook retirement: ${_prune_out}"' in setup
        assert 'warn "personal-kb-hook retirement failed:' in setup
        assert "_read_env_var PERSONAL_KB" not in setup
        assert "_read_env_var TEAM_KB" not in setup


class TestPruneScript:
    def test_preserves_everything_else(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        original = {
            "theme": "dark",
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "PreToolUse": [_hook_group("Bash"), FOREIGN],
                "PostToolUse": [_hook_group("mcp__personal-kb__kb_get"), FOREIGN],
            },
        }
        settings_path.write_text(json.dumps(original))
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 2 matcher group(s) from 2 event(s)"
        )
        data = json.loads(settings_path.read_text())
        expected = {
            "theme": "dark",
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "PreToolUse": [FOREIGN],
                "PostToolUse": [FOREIGN],
            },
        }
        assert data == expected

    def test_drops_emptied_event_keys_only(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [_hook_group()],
                        "Stop": [_hook_group(), FOREIGN],
                        "AlreadyEmpty": [],
                    }
                }
            )
        )
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 2 matcher group(s) from 2 event(s)"
        )
        data = json.loads(settings_path.read_text())
        assert "SessionStart" not in data["hooks"]
        assert data["hooks"]["Stop"] == [FOREIGN]
        assert data["hooks"]["AlreadyEmpty"] == []

    def test_mixed_group_is_dropped_whole(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        mixed = {
            "matcher": "Bash",
            "hooks": [
                {"type": "command", "command": "echo foreign"},
                {"type": "command", "command": HOOK_COMMAND},
            ],
        }
        settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [mixed]}}))
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 1 matcher group(s) from 1 event(s)"
        )
        data = json.loads(settings_path.read_text())
        assert data["hooks"] == {}

    def test_second_run_is_byte_identical(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"theme": "dark", "hooks": {"Stop": [_hook_group(), FOREIGN]}})
        )
        first = _run(settings_path)
        assert first.returncode == 0, first.stderr
        raw = settings_path.read_bytes()
        stamp = settings_path.stat().st_mtime_ns
        second = _run(settings_path)
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == (
            "personal-kb-hook-prune: removed 0 matcher group(s) from 0 event(s)"
        )
        assert settings_path.read_bytes() == raw
        assert settings_path.stat().st_mtime_ns == stamp

    def test_tolerates_malformed_shapes(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        original = {
            "hooks": "not-a-dict",
            "other": [1, 2, 3],
        }
        settings_path.write_text(json.dumps(original))
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 0 matcher group(s) from 0 event(s)"
        )
        assert json.loads(settings_path.read_text()) == original

        # Non-list event value and non-dict list elements survive untouched.
        original = {
            "hooks": {
                "Stop": "not-a-list",
                "PreToolUse": ["a-string", 7, None, {"hooks": "nope"}],
            }
        }
        settings_path.write_text(json.dumps(original))
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 0 matcher group(s) from 0 event(s)"
        )
        assert json.loads(settings_path.read_text()) == original

    def test_invalid_json_left_untouched(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{not json")
        proc = _run(settings_path)
        assert proc.returncode == 1
        assert proc.stderr == (
            f"[ERR]  personal-kb-hook-prune: cannot parse {settings_path} — "
            "file left unchanged\n"
        )
        assert settings_path.read_text() == "{not json"

    def test_wrong_argc_usage(self, tmp_path: Path) -> None:
        for args in ([], [str(tmp_path / "a.json"), "extra"]):
            proc = subprocess.run(
                [sys.executable, str(PRUNE_SCRIPT), *args],
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 2
            assert proc.stderr == "usage: personal-kb-hook-prune.py <settings.json>\n"

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "absent.json"
        proc = _run(settings_path)
        assert proc.returncode == 0, proc.stderr
        assert not settings_path.exists()
        assert proc.stdout.strip() == (
            "personal-kb-hook-prune: removed 0 matcher group(s) from 0 event(s)"
        )
