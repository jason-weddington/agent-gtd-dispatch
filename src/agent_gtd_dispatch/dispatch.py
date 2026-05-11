"""Core dispatch logic — workspace prep, prompt building, agent invocation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, gtd_client
from .engines import COMMON_ENV_KEYS, Engine, build_env
from .models import CIGateResult

logger = logging.getLogger(__name__)


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


def prepare_workspace(origin: str, run_id: str, branch_name: str) -> Path:
    """Clone the repo and check out a feature branch for this run."""
    name = repo_name_from_origin(origin)
    workspace = config.WORKSPACE_ROOT / f"{name}-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", origin, str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    return workspace


def cleanup_workspace(workspace: Path) -> None:
    """Remove a workspace directory after a run completes."""
    import shutil

    if workspace.exists() and config.WORKSPACE_ROOT in workspace.parents:
        shutil.rmtree(workspace, ignore_errors=True)


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe filesystem use.

    Strips path separators to prevent directory traversal, keeps only
    safe characters [A-Za-z0-9._-], and truncates to 200 chars.
    """
    # Strip path separators to prevent directory traversal
    name = re.sub(r"[/\\]", "", filename)
    # Keep only safe characters
    name = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    # Truncate to 200 chars; fall back to "attachment" if nothing survives
    return name[:200] or "attachment"


async def stage_attachments(
    workspace: Path, run_id: str, item_id: str
) -> list[dict[str, Any]]:
    """Fetch attachments for the item, write them into {run_id}-attachments/.

    Returns the list of staged attachments (metadata only; for use in the prompt).
    Empty list if the item has no attachments.
    Individual download failures are logged but don't abort the run — the failed
    entry is omitted from the returned list.
    """
    try:
        attachments = await gtd_client.list_attachments(item_id)
    except Exception as exc:
        logger.warning("Failed to list attachments for item %s: %s", item_id, exc)
        return []

    if not attachments:
        return []

    attach_dir = workspace / f"{run_id}-attachments"
    attach_dir.mkdir(mode=0o700, exist_ok=True)

    staged: list[dict[str, Any]] = []
    for attachment in attachments:
        att_id = attachment["id"]
        raw_filename = attachment.get("filename", "attachment")
        filename = _sanitize_filename(raw_filename)
        try:
            data = await gtd_client.download_attachment(att_id)
            (attach_dir / filename).write_bytes(data)
            staged.append(attachment)
        except Exception as exc:
            logger.warning(
                "Failed to download attachment %s: %s — skipping", att_id, exc
            )

    return staged


def _build_supporting_files_section(
    attachments: list[dict[str, Any]] | None, run_id: str
) -> str:
    """Build the Supporting Files prompt section, or empty string if not applicable."""
    if not attachments or not run_id:
        return ""

    att_lines = []
    for att in attachments:
        filename = att.get("filename", "attachment")
        mime_type = att.get("mime_type", "application/octet-stream")
        size_kb = round(att.get("size_bytes", 0) / 1024, 1)
        att_lines.append(f"- `{filename}` ({mime_type}, {size_kb} KB)")

    file_list = "\n".join(att_lines)
    return (
        "## Supporting Files\n\n"
        "The human attached these files to this item. They're available in the\n"
        f"`{run_id}-attachments/` directory of your workspace:\n\n"
        f"{file_list}\n\n"
        "Read them when relevant to your task. **DO NOT** commit the\n"
        f"`{run_id}-attachments/` directory — it exists only for this run."
    )


_MANAGE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__agent-gtd__advance_wave",
    "mcp__agent-gtd__complete_in_wave",
    "mcp__agent-gtd__halt_wave",
    "mcp__agent-gtd__replan_wave",
    "mcp__agent-gtd__add_comment",
    "mcp__agent-gtd__get_item",
    "mcp__agent-gtd__list_items",
    "mcp__agent-gtd__get_run_status",
    "mcp__agent-gtd__list_runs",
    "mcp__agent-gtd__dispatch_item",    # dispatch child build runs
    "mcp__agent-gtd__list_comments",    # read final agent comment
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
)


def build_system_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    branch_name: str | None,
    max_turns: int,
    mode: str = "build",
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    wave_run_id: str | None = None,
) -> str:
    """Build the headless agent system prompt."""
    if mode == "plan":
        return _build_plan_prompt(
            item, project, max_turns, attachments=attachments, run_id=run_id
        )
    if mode == "manage":
        return _build_manage_prompt(wave_run_id or "", project, max_turns)
    return _build_build_prompt(
        item, project, branch_name or "", max_turns, attachments=attachments, run_id=run_id
    )


def _build_plan_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    max_turns: int,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
) -> str:
    """System prompt for plan mode — groom a task, don't build it."""
    item_id = item["id"]
    title = item["title"]
    description = item.get("description", "")
    project_name = project["name"]

    desc_block = (
        f"**Description:**\n{description}"
        if description
        else "No description provided — work from the title only."
    )

    files_section = _build_supporting_files_section(attachments, run_id)

    prompt = textwrap.dedent(
        f"""\
        You are a headless planning agent dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: PLAN** — You are grooming this task, NOT implementing it.

        **Project:** {project_name}
        **Item:** {title}
        **Item ID:** {item_id}

        {desc_block}
        """
    )

    if files_section:
        prompt += "\n" + files_section + "\n"

    prompt += textwrap.dedent(
        f"""\

        ## What to do

        1. **Read the codebase.** Understand existing patterns, architecture, and conventions.
        2. **Write acceptance criteria.** Update the item description with clear, testable AC.
        3. **Identify files to modify.** List specific file paths and what changes in each.
        4. **Add patterns to follow.** Reference existing code the implementer should copy.
        5. **Define scope boundaries.** Explicitly state what NOT to touch.
        6. **Add verification steps.** How to test the changes (commands, expected output).
        7. **Ask questions if unclear.** If the intent is ambiguous, post a comment asking
           for clarification and stop. Do NOT guess.

        ## Rules

        - Do NOT write code, create branches, or push anything.
        - Do NOT modify any files in the repo.
        - Use `update_item` (with the item's current version) to update the description.
        - Use `add_comment` with item_id="{item_id}" for questions or notes.
        - When grooming is complete, set item status to `ready` using `update_item`.

        ## Reporting

        Post a comment when you start: "Planning..."

        **On success:**
        1. Post a comment summarizing what you added to the description
        2. Set item status to `ready`

        **On failure/blocked:**
        1. Post a comment explaining what's unclear
        2. Leave status unchanged

        ## Important

        - You have max {max_turns} turns. Budget them wisely.
        - Focus only on this task. Don't groom other items you notice.
    """
    )

    return prompt


def _heartbeat_prompt_addendum() -> str:
    """Addendum instructing the manage-mode executor to ping_wave for liveness."""
    return textwrap.dedent("""\
        ## Liveness (Heartbeat)

        During any idle wait (between dispatches, while monitoring child runs, while merging)
        call the `mcp__agent-gtd__ping_wave(wave_run_id, phase=<current_phase>,
        waiting_on=<current_item_id_or_empty_string>)` MCP tool at least every 90 seconds.
        Valid `phase` values: `"planning"`, `"dispatching"`, `"monitoring"`, `"merging"`,
        `"halted"`. This proves liveness so the reaper does not mark the wave crashed.
        The reaper threshold is 300 seconds; 90s gives margin.
    """)


def _ci_gate_prompt_addendum() -> str:
    """Addendum instructing the manage-mode executor to run CI gate before marking items complete."""
    return textwrap.dedent("""\
        ## Pre-merge CI Gate

        Before calling `complete_in_wave(item_id, outcome=success)` for any item, POST to the
        dispatch worker's `/ci-gate` endpoint with
        `{"repo_url": "<repo>", "branch_name": "<branch>"}`. If the response has
        `"passed": false`, call
        `halt_wave(wave_run_id, reason="CI failure on <branch>: <failed_step>")` and STOP.
    """)


def _build_manage_prompt(
    wave_run_id: str,
    project: dict[str, Any],
    max_turns: int,
) -> str:
    """System prompt for manage mode — run the wave-manager executor loop."""
    project_name = project["name"]
    git_origin = project.get("git_origin", "")

    prompt = textwrap.dedent(
        f"""\
        You are a headless wave-manager executor dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: MANAGE** — You are orchestrating a wave execution, NOT writing code.

        **Project:** {project_name}
        **Git Origin:** {git_origin}
        **Wave Run ID:** {wave_run_id}
        **Turns remaining:** {max_turns}

        This wave run ID is your primary anchor. Every action you take is scoped to it.

        Check `~/.claude/CLAUDE.md` for the project dispatch playbook — it is read
        automatically at startup. Follow any relevant procedures described there.

        This executor never writes code, pushes branches, or takes ownership of any repository.

        ## Launch item_id — Ignore It

        The `item_id` you received as the dispatch trigger is a positional placeholder, not a
        wave item to act on. **Ignore it.** Your sole source of truth for which items to dispatch
        is the wave plan — read it via
        `mcp__agent-gtd__advance_wave(wave_run_id="{wave_run_id}")` and dispatch every item in
        `next_ready` in build mode.
        Do NOT add comments to the launch item_id.
        Do NOT mark it complete.
        Do NOT treat it as a gate.

        ## Executor Loop

        Repeat until advance_wave reports graph_complete=true:

        STEP 1 — ADVANCE
          Call: mcp__agent-gtd__advance_wave(wave_run_id="{wave_run_id}")
          → {{next_ready: [...], in_progress: [...], graph_complete: bool}}
          If advance_wave fails: retry up to 3 times with 30 s sleep. After 3 failures:
            call halt_wave(wave_run_id, reason="advance_wave failed 3 times"); EXIT.
          If graph_complete=true and next_ready=[]: EXIT SUCCESS.

        STEP 2 — DISPATCH READY ITEMS
          For each item_id in next_ready:
            call mcp__agent-gtd__dispatch_item(
                item_id=item_id,
                mode="build",
                wave_run_id="{wave_run_id}",
            )
            record the returned run_id alongside item_id
          NOTE: wave_run_id is REQUIRED on every child dispatch — the reaper depends on it.

        STEP 3 — MONITOR TO COMPLETION
          For each dispatched (item_id, run_id):
            Poll mcp__agent-gtd__get_run_status(run_id) every 30 s until status in
            {{succeeded, failed, timed_out, cancelled}}.
            Process each item as it lands (don't wait for all to finish before processing any).

        STEP 4 — CLASSIFY
          For each completed item:
            a. List comments: mcp__agent-gtd__list_comments(item_id=item_id)
               Take the last comment whose created_by starts with "claude-" (the build agent).
               If no such comment found, treat as HALT with reason "no final agent comment".
            b. Get branch_name: mcp__agent-gtd__list_runs(item_id=item_id)
               The most recent run's branch_name field.
            c. Run classifier (Bash):
                 python -m agent_gtd_dispatch.wave_manager.classifier \\
                   --comment "<final_comment_text>" \\
                   --wave-run-id "{wave_run_id}"
               Outputs DECIDE:<rule_name> or HALT:<reason> on stdout.
               If the classifier command fails to run or is not importable,
               treat every completion as HALT with reason "classifier unavailable".
            d. If run status is failed/timed_out/cancelled: treat as HALT
               with reason "build agent <status>: <run_id>".

        STEP 5a — DECIDE PATH (auto-merge)
          Call (Bash):
            python -m agent_gtd_dispatch.wave_manager.squash_merge \\
              --origin {git_origin or "<project.git_origin>"} \\
              --branch <branch_name> \\
              --item-id <item_id> \\
              --wave-run-id {wave_run_id} \\
              --decision-rule <rule_name>
          Exit code 0 → success:
            call mcp__agent-gtd__complete_in_wave(
              wave_run_id, item_id, outcome="completed",
              merge_actor="manager-allowlist", decision_rule=<rule_name>)
          Exit code non-zero → treat as HALT with stderr as reason.

        STEP 5b — HALT PATH
          Post the halt comment to the OFFENDING WAVE ITEM (the item whose build run
          triggered the halt), NOT to the launch placeholder item_id.
          If there is no specific offending item (e.g. advance_wave failed 3 times),
          post to the project instead.
          call mcp__agent-gtd__add_comment(
            item_id=<offending_wave_item_id>,  ← NOT the launch placeholder
            content="Wave halted: <reason>")
          # OR if no specific offending item:
          # call mcp__agent-gtd__add_comment(
          #   project_id=<project_id>,
          #   content="Wave halted: <reason>")
          call mcp__agent-gtd__halt_wave(wave_run_id, reason=<reason>)
          EXIT.

        ## Rules

        - Never commit code, push branches, or modify any repository directly.
        - Never touch waves or items outside wave_run_id={wave_run_id}.
        - The squash_merge helper handles all git operations; if CI gate (item 7e2753ec)
          is not yet available, squash_merge.py stubs it as always-pass with a logged warning.
        - You have max {max_turns} turns. Budget them wisely.
    """
    )

    prompt += "\n" + _heartbeat_prompt_addendum()
    prompt += "\n" + _ci_gate_prompt_addendum()
    return prompt


def _build_build_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    branch_name: str,
    max_turns: int,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
) -> str:
    """System prompt for build mode — implement and push a branch."""
    item_id = item["id"]
    title = item["title"]
    description = item.get("description", "")
    project_name = project["name"]

    desc_block = (
        f"**Description:**\n{description}"
        if description
        else "No description provided — work from the title only."
    )

    files_section = _build_supporting_files_section(attachments, run_id)

    att_rule = ""
    if files_section and run_id:
        att_rule = (
            f"\n7. **Ignore the `{run_id}-attachments/` directory.** "
            "It is run-scoped context, not part of the repo. "
            "Do not `git add` it, do not reference it in commit messages."
        )

    prompt = textwrap.dedent(
        f"""\
        You are a headless coding agent dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Project:** {project_name}
        **Item:** {title}
        **Item ID:** {item_id}

        {desc_block}
        """
    )

    if files_section:
        prompt += "\n" + files_section + "\n"

    prompt += textwrap.dedent(
        f"""\

        ## Rules

        1. **Understand first.** Read the codebase, understand the patterns, then act.
        2. **Branch.** You are already on branch `{branch_name}`. Stay on it. Never commit to main.
        3. **Test.** Run the project's test suite before committing. Fix failures.
        4. **Commit.** Use conventional commit messages. Small, focused commits.
        5. **Push.** When done, push `{branch_name}` to origin.
        6. **Stop if stuck.** If the task is too ambiguous, you lack information, or
           you cannot complete it cleanly — STOP. Do not guess or produce low-quality work.{att_rule}

        ## Reporting

        Post progress comments to the GTD item as you work. Use `add_comment`
        with item_id="{item_id}". Keep comments terse — one line is fine.

        Post a comment at each milestone:
        - When starting research/exploration: "Researching codebase..."
        - When starting implementation: "Implementing..."
        - When running tests: "Running tests..."

        **On success:**
        1. Post a final comment with: what you did, the branch name (`{branch_name}`), notes for the reviewer
        2. Set the item status to `review` using `update_item` with the item's current version

        **On failure/blocked**, your comment should include:
        - Why you stopped
        - What information or clarification you need
        - Any partial progress (if you pushed commits)

        ## Important

        - You have max {max_turns} turns. Budget them wisely.
        - Never force-push, never push to main, never delete branches you didn't create.
        - Never modify CI/CD configs, deployment scripts, or secrets.
        - Focus only on this task. Don't fix unrelated issues you notice.
    """
    )

    return prompt


def _detect_project_type(workspace: Path) -> str:
    """Auto-detect project type from the workspace filesystem.

    Checks for pyproject.toml → "python", package.json → "frontend", else "unknown".
    """
    if (workspace / "pyproject.toml").exists():
        return "python"
    if (workspace / "package.json").exists():
        return "frontend"
    return "unknown"


def _ci_steps_for_project_type(project_type: str) -> list[tuple[str, list[str]]]:
    """Return ordered CI steps (command_string, command_list) for the given project type."""
    if project_type == "python":
        return [
            ("uv run pytest", ["uv", "run", "pytest"]),
            ("uv run ruff check", ["uv", "run", "ruff", "check"]),
            ("uv run mypy src/", ["uv", "run", "mypy", "src/"]),
        ]
    if project_type == "frontend":
        return [
            ("npm run build", ["npm", "run", "build"]),
            ("npm run test", ["npm", "run", "test"]),
        ]
    return []  # "unknown" — nothing to run


def _ci_env() -> dict[str, str]:
    """Build a filtered env dict for CI subprocesses (COMMON_ENV_KEYS only)."""
    return {k: v for k, v in os.environ.items() if k in COMMON_ENV_KEYS}


async def run_ci_gate(
    repo_url: str,
    branch_name: str,
    project_type: str | None,
    timeout_s: int,
) -> CIGateResult:
    """Clone a branch and run the CI suite against it.

    Always returns a CIGateResult — never raises. CI failure is expressed as
    passed=False in the result body, not as an exception.

    The CI subprocess inherits only COMMON_ENV_KEYS (same allow-list used for agent
    subprocesses), so secrets like ANTHROPIC_API_KEY are never forwarded.
    """
    from uuid import uuid4

    run_id = f"ci-{uuid4().hex[:10]}"
    workspace: Path | None = None
    resolved_type = project_type or "unknown"

    try:
        # Clone and check out the existing branch
        name = repo_name_from_origin(repo_url)
        workspace = config.WORKSPACE_ROOT / f"{name}-{run_id}"
        config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["git", "clone", repo_url, str(workspace)],
                check=True,
                capture_output=True,
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["git", "checkout", branch_name],
                cwd=workspace,
                check=True,
                capture_output=True,
            ),
        )

        # Auto-detect project type if not provided
        if project_type is None:
            resolved_type = _detect_project_type(workspace)

        steps = _ci_steps_for_project_type(resolved_type)
        env = _ci_env()

        for step_str, step_cmd in steps:
            cmd_to_run = step_cmd
            try:

                def _run_step(
                    _cmd: list[str] = cmd_to_run,
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        _cmd,
                        cwd=workspace,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                    )

                result = await loop.run_in_executor(None, _run_step)
                if result.returncode != 0:
                    return CIGateResult(
                        passed=False,
                        project_type=resolved_type,
                        failed_step=step_str,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        returncode=result.returncode,
                    )
            except subprocess.TimeoutExpired:
                return CIGateResult(
                    passed=False,
                    project_type=resolved_type,
                    failed_step="timeout",
                    stdout="",
                    stderr=f"CI step timed out after {timeout_s}s",
                    returncode=None,
                )

        return CIGateResult(
            passed=True,
            project_type=resolved_type,
            failed_step=None,
            stdout="",
            stderr="",
            returncode=0,
        )

    except Exception as exc:
        logger.exception("CI gate error: %s", exc)
        return CIGateResult(
            passed=False,
            project_type=resolved_type,
            failed_step=str(exc),
            stdout="",
            stderr=str(exc),
            returncode=None,
        )
    finally:
        if workspace is not None:
            cleanup_workspace(workspace)


async def run_agent(
    engine: Engine,
    workspace: Path,
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None = None,
    timeout_seconds: int | None = None,
    allowed_tools: list[str] | None = None,
    mode: str = "build",
) -> subprocess.CompletedProcess[str]:
    """Run a headless agent CLI as a subprocess."""
    if timeout_seconds is None:
        timeout_seconds = config.TIMEOUT_SECONDS
    if engine.name == "kiro":
        (workspace / "system_prompt.md").write_text(
            f"{system_prompt}\n\n---\n\n## Task\n\n{title}"
        )
    cmd = engine.build_command(system_prompt, title, max_turns, agent_name)
    if allowed_tools is not None and engine.name == "claude":
        # Insert --allowedTools BEFORE --print.  claude's argparser breaks
        # when --allowedTools sits between --print and the positional prompt
        # ("Error: Input must be provided ... when using --print"); --print
        # must be the last flag before the prompt.
        print_idx = cmd.index("--print")
        cmd[print_idx:print_idx] = ["--allowedTools", ",".join(allowed_tools)]
    env = build_env(engine, mode=mode)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
        ),
    )
