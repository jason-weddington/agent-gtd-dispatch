#!/usr/bin/env python3
"""Merge the seven personal-kb-hook Claude Code hook blocks into settings.json.

Targets an agent user's ~/.claude/settings.json. Read-modify-write: any OTHER
settings already present in the file are preserved untouched. Any pre-existing
hook block whose command mentions 'personal-kb-hook' is REPLACED (never
appended) — including a hand-added block with a different shape — so re-running
this script against the same file is idempotent: a second run still yields
exactly seven personal-kb-hook blocks, never fourteen.

Events wired (the upstream canonical wiring, reproduced exactly):
  SessionStart        matcher ""           no timeout
  UserPromptSubmit    matcher ""           no timeout
  PreToolUse          matcher "Bash"       timeout 5
  PostToolUse         matcher "mcp__personal-kb__kb_get|mcp__team-kb__team_kb_get"
                                            no timeout
  PostToolUseFailure  matcher ""           timeout 5
  Stop                matcher ""           no timeout
  SubagentStop        matcher ""           no timeout

No block is async. PreToolUse must stay synchronous (it has to be able to deny
the tool call), and PostToolUseFailure must stay synchronous too — under async
Claude Code would deliver its additionalContext on a later turn, which defeats
the failure cue. The PostToolUseFailure command is prefixed with
KB_FAILURE_CONTEXT=1 so the hook emits the failure-observatory context.

Usage:
    personal-kb-hook-settings.py <settings_file> <hook_bin> <key_file> <url>

The command embedded in each block references the API key via `$(cat
<key_file> 2>/dev/null)` shell indirection — the literal key value never
appears in the rendered settings.json.

No third-party imports — stdlib only, so it runs with nothing but the
python3 already required elsewhere in setup-dispatch-host.sh.
"""

from __future__ import annotations

import json
import sys

POST_TOOL_USE_MATCHER = "mcp__personal-kb__kb_get|mcp__team-kb__team_kb_get"
KB_FAILURE_CONTEXT_PREFIX = "KB_FAILURE_CONTEXT=1 "
HOOK_MARKER = "personal-kb-hook"

# (event, matcher, timeout). matcher "" matches every tool call; timeout None
# means the block carries no "timeout" key. Order mirrors the upstream wiring.
EVENTS: tuple[tuple[str, str, int | None], ...] = (
    ("SessionStart", "", None),
    ("UserPromptSubmit", "", None),
    ("PreToolUse", "Bash", 5),
    ("PostToolUse", POST_TOOL_USE_MATCHER, None),
    ("PostToolUseFailure", "", 5),
    ("Stop", "", None),
    ("SubagentStop", "", None),
)


def _hook_command(hook_bin: str, key_file: str, url: str) -> str:
    return (
        f"PERSONAL_KB_LISTENER=1 PERSONAL_KB_URL={url} "
        f"PERSONAL_KB_API_KEY=$(cat {key_file} 2>/dev/null) "
        f"{hook_bin} --format=claude-json"
    )


def _is_hook_block(entry: dict) -> bool:
    for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
        if HOOK_MARKER in h.get("command", ""):
            return True
    return False


def _replace_event(
    hooks: dict, event: str, matcher: str, timeout: int | None, command: str
) -> None:
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
    # Drop any pre-existing personal-kb-hook block(s) for this event so a
    # re-run replaces in place rather than accumulating duplicates.
    entries = [e for e in entries if not _is_hook_block(e)]
    # The timeout belongs on the hook command, not the matcher entry: Claude Code
    # reads it per command and ignores it at entry level.
    hook: dict = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    new_entry: dict = {"matcher": matcher, "hooks": [hook]}
    entries.append(new_entry)
    hooks[event] = entries


def merge(settings: dict, hook_bin: str, key_file: str, url: str) -> dict:
    """Replace the seven personal-kb-hook blocks in settings, in place."""
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    for event, matcher, timeout in EVENTS:
        command = _hook_command(hook_bin, key_file, url)
        if event == "PostToolUseFailure":
            command = KB_FAILURE_CONTEXT_PREFIX + command
        _replace_event(hooks, event, matcher, timeout, command)
    return settings


def main(argv: list[str]) -> int:
    """CLI entry point: read argv, merge, write back. Returns the process exit code."""
    if len(argv) != 5:
        print(
            "usage: personal-kb-hook-settings.py <settings_file> <hook_bin> "
            "<key_file> <url>",
            file=sys.stderr,
        )
        return 2
    settings_file, hook_bin, key_file, url = argv[1:5]

    try:
        with open(settings_file, encoding="utf-8") as f:
            raw = f.read().strip()
        settings = json.loads(raw) if raw else {}
    except FileNotFoundError:
        settings = {}

    merge(settings, hook_bin, key_file, url)

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
