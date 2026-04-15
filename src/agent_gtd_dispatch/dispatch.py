"""Core dispatch logic — workspace prep, prompt building, Claude invocation."""

from __future__ import annotations

import asyncio
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config


def repo_name_from_origin(origin: str) -> str:
    """Extract a clean repo name from a git origin URL.

    Handles SSH (git@host:org/repo.git), SCP-style (git@host:repos/name),
    and HTTPS URLs.
    """
    # SSH/SCP style: git@host:path/repo.git or git@host:repos/name
    match = re.search(r"[/:]([^/:]+/[^/:]+?)(?:\.git)?$", origin)
    if match:
        return match.group(1).replace("/", "-")
    # Fallback: last path component
    parsed = urlparse(origin)
    return Path(parsed.path).stem or "unknown"


def branch_name_for_item(item_id: str, title: str) -> str:
    """Build a branch-safe name from item ID and title."""
    short_id = item_id[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")
    return f"feat/{short_id}-{slug}"


def prepare_workspace(origin: str, item_id: str) -> Path:
    """Clone or update the repo into a workspace directory."""
    short_id = item_id[:8]
    name = repo_name_from_origin(origin)
    workspace = config.WORKSPACE_ROOT / f"{name}-{short_id}"

    if workspace.exists():
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=workspace,
            check=False,
            capture_output=True,
        )
    else:
        config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", origin, str(workspace)],
            check=True,
        )

    return workspace


def build_system_prompt(
    item: dict[str, Any], project: dict[str, Any], branch_name: str, max_turns: int
) -> str:
    """Build the headless agent system prompt."""
    item_id = item["id"]
    title = item["title"]
    description = item.get("description", "")
    project_name = project["name"]

    return textwrap.dedent(f"""\
        You are a headless Claude Code agent dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Project:** {project_name}
        **Item:** {title}
        **Item ID:** {item_id}

        {f"**Description:**{chr(10)}{description}" if description else "No description provided — work from the title only."}

        ## Rules

        1. **Understand first.** Read the codebase, understand the patterns, then act.
        2. **Branch.** Create and work on the branch `{branch_name}`. Never commit to main.
        3. **Test.** Run the project's test suite before committing. Fix failures.
        4. **Commit.** Use conventional commit messages. Small, focused commits.
        5. **Push.** When done, push `{branch_name}` to origin.
        6. **Stop if stuck.** If the task is too ambiguous, you lack information, or
           you cannot complete it cleanly — STOP. Do not guess or produce low-quality work.

        ## Reporting

        When you finish (success or blocked), post a comment to the GTD item.
        The Agent GTD MCP server is available — use `add_comment` with item_id="{item_id}".

        **On success**, your comment should include:
        - What you did (1-3 sentences)
        - The branch name: `{branch_name}`
        - Any notes for the reviewer

        **On failure/blocked**, your comment should include:
        - Why you stopped
        - What information or clarification you need
        - Any partial progress (if you pushed commits)

        ## Important

        - You have max {max_turns} turns. Budget them wisely.
        - Never force-push, never push to main, never delete branches you didn't create.
        - Never modify CI/CD configs, deployment scripts, or secrets.
        - Focus only on this task. Don't fix unrelated issues you notice.
    """)


async def run_claude(
    workspace: Path,
    system_prompt: str,
    title: str,
    max_turns: int,
) -> subprocess.CompletedProcess[str]:
    """Run Claude Code as a subprocess. Returns the CompletedProcess."""
    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(max_turns),
        "--system-prompt",
        system_prompt,
        "--print",
        title,
    ]

    env = {
        k: v for k, v in __import__("os").environ.items() if k in config.SAFE_ENV_KEYS
    }
    env["HOME"] = str(Path.home())

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            timeout=config.TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        ),
    )
