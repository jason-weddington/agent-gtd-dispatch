#!/usr/bin/env python3
"""Remove retired personal-kb-hook matcher groups from a Claude Code settings.json.

The personal-kb-hook is retired fleet-wide. This script is the retirement half of
the old merge script: instead of writing seven hook blocks, it drops every
matcher group whose ``hooks`` list contains a command mentioning
``personal-kb-hook``. A read-modify-write of the parsed JSON preserves every other
setting, every other matcher group and every other event; an event key whose list
becomes empty is deleted, while an already-empty list is preserved. Re-running is
idempotent and only writes the file when something was actually removed.

Usage:
    personal-kb-hook-prune.py <settings.json>

stdlib only (json, sys): it runs with nothing but the python3 already required
elsewhere in setup-dispatch-host.sh and deploy.sh.
"""

from __future__ import annotations

import json
import sys

HOOK_MARKER = "personal-kb-hook"


def _is_hook_group(group: object) -> bool:
    """Return True when *group* is a dict whose hooks mention personal-kb-hook."""
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if (
            isinstance(hook, dict)
            and isinstance(hook.get("command"), str)
            and HOOK_MARKER in hook["command"]
        ):
            return True
    return False


def prune(data: object) -> tuple[int, int]:
    """Drop hook groups in place. Return (groups dropped, events affected).

    Non-dict top-level values, non-dict ``hooks`` values, non-list event values and
    non-dict list elements are left untouched and never counted, so a malformed
    settings file cannot make this script raise.
    """
    if not isinstance(data, dict):
        return 0, 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0, 0

    dropped = 0
    events = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _is_hook_group(group)]
        removed = len(groups) - len(kept)
        if removed == 0:
            continue
        dropped += removed
        events += 1
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return dropped, events


def _summary(dropped: int, events: int) -> str:
    return (
        f"personal-kb-hook-prune: removed {dropped} matcher group(s) "
        f"from {events} event(s)"
    )


def main(argv: list[str]) -> int:
    """CLI entry point: read argv, prune, write back only when needed."""
    if len(argv) != 2:
        print("usage: personal-kb-hook-prune.py <settings.json>", file=sys.stderr)
        return 2
    path = argv[1]

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(_summary(0, 0))
        return 0
    except ValueError:
        print(
            f"[ERR]  personal-kb-hook-prune: cannot parse {path} — file left unchanged",
            file=sys.stderr,
        )
        return 1

    dropped, events = prune(data)
    if dropped:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
    print(_summary(dropped, events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
