"""Core dispatch logic — workspace prep, prompt building, agent invocation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, gtd_client
from .engines import Engine, build_env

logger = logging.getLogger(__name__)

_executor: concurrent.futures.ThreadPoolExecutor | None = None


def init_executor() -> None:
    """Create (or recreate) the module-level ThreadPoolExecutor.

    Must be called after config.load() so that config.MAX_CONCURRENT_RUNS is set.
    Shuts down the previous executor without waiting for running tasks to finish.
    """
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
    _executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.MAX_CONCURRENT_RUNS
    )


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


def prepare_manage_workspace(git_origin: str, run_id: str) -> Path:
    """Clone the repo for manage mode and detect the default branch.

    Steps:
    1. git clone --depth=50 {git_origin} {workspace}
    2. git remote set-head origin --auto  (populate HEAD ref)
    3. git symbolic-ref --short refs/remotes/origin/HEAD  → detect default branch
    4. git checkout {default_branch}  (explicit, stays on default branch)

    Returns the workspace path.
    """
    workspace = config.WORKSPACE_ROOT / f"repos-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=50", git_origin, str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-head", "origin", "--auto"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    default_branch = result.stdout.strip().removeprefix("origin/")
    subprocess.run(
        ["git", "checkout", default_branch],
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


def write_transcript(workspace: Path, result: subprocess.CompletedProcess[str]) -> None:
    """No-op: transcript is now streamed continuously during the run by run_agent().

    Kept to avoid breaking any external callers. The file is written by run_agent()
    via subprocess.Popen; this function does nothing.
    """


def _setup_git_exclude(workspace: Path) -> None:
    """Exclude transcript.txt from git before the subprocess starts."""
    git_exclude = workspace / ".git" / "info" / "exclude"
    if git_exclude.exists():
        with git_exclude.open("a") as f:
            f.write("\ntranscript.txt\n")


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
    "mcp__agent-gtd__advance_rollout",
    "mcp__agent-gtd__complete_item_in_rollout",
    "mcp__agent-gtd__halt_rollout",
    "mcp__agent-gtd__replan_rollout",
    "mcp__agent-gtd__update_rollout_state",
    "mcp__agent-gtd__dispatch_item",  # dispatch child build runs
    "mcp__agent-gtd__add_comment",
    "mcp__agent-gtd__get_item",
    "mcp__agent-gtd__update_item",  # AC reconciliation
    "mcp__agent-gtd__list_items",
    "mcp__agent-gtd__get_run_status",
    "mcp__agent-gtd__list_runs",
    "mcp__agent-gtd__list_comments",  # read final agent comment
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
    rollout_id: str | None = None,
    manage_retry_count: int = 0,
) -> str:
    """Build the headless agent system prompt."""
    if mode == "plan":
        return _build_plan_prompt(
            item, project, max_turns, attachments=attachments, run_id=run_id
        )
    if mode == "manage":
        return _build_manage_prompt(
            rollout_id or "", project, max_turns, manage_retry_count=manage_retry_count
        )
    return _build_build_prompt(
        item,
        project,
        branch_name or "",
        max_turns,
        attachments=attachments,
        run_id=run_id,
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
        7. **Select build engine.** Evaluate this task against the Engine-Selection Rubric below.
           - If ALL "Route to Ollama" criteria are met: call `update_item` with `build_engine="claude-code-ollama"`.
           - Otherwise: leave `build_engine` unset (vanilla Claude Code is the default).
           In both cases, append one line to the bottom of the item description:
           `Build engine: claude-code-ollama — <one-sentence reason>` or
           `Build engine: claude-code (default) — <one-sentence reason>`.
           When in doubt, choose `claude-code` — false positives (bad Ollama output) cost more than false negatives.
        8. **Ask questions if unclear.** If the intent is ambiguous, post a comment asking
           for clarification and stop. Do NOT guess.

        ## Rules

        - Do NOT write code, create branches, or push anything.
        - Do NOT modify any files in the repo.
        - Use `update_item` (with the item's current version) to update the description.
        - Use `add_comment` with item_id="{item_id}" for questions or notes.
        - When grooming is complete, set item status to `ready` using `update_item`.

        ## Engine-Selection Rubric

        Two engines are available for build-mode dispatches:

        - **`claude-code` (Anthropic API)** — full-capability default. Strong reasoning, large context, premium cost.
        - **`claude-code-ollama` (local inference)** — same Claude Code harness, local model. Free, private, slower per token, weaker on hard reasoning.

        ### Route to `claude-code-ollama` when ALL of these hold

        1. **Single-file or tightly bounded** — changes touch 1-3 files, no orchestration across modules
        2. **Pattern-following** — the AC can be expressed as "make X look like Y" or "do for B what was done for A"; a clear template exists in the codebase
        3. **Mechanical edits dominate** — renames, string/copy changes, format fixes, type tightening, adding a missing null guard, single-method extractions
        4. **Tests are clone-and-modify** — new tests fit an existing test pattern; no novel test design
        5. **No cross-cutting decisions** — no "should this go here or there?" judgment; the right place is obvious from the AC
        6. **No external system interaction** — pure code, no new API integrations, no novel database queries, no new auth flows

        ### Route to `claude-code` (Anthropic) when ANY of these hold

        1. **Multi-file orchestration** — changes touch 4+ files and require coordinating intent across them
        2. **Novel design decisions** — the AC says "decide whether…" or "design a way to…"; no template
        3. **Debugging** — investigating a bug whose root cause isn't named in the description
        4. **Cross-cutting concerns** — auth, error handling, migration logic, performance work, threading/async correctness
        5. **New API/protocol surface** — designing endpoints, request/response shapes, message formats
        6. **Test design from scratch** — the test pattern doesn't exist yet; you're inventing it
        7. **Wide blast radius** — change affects many consumers (e.g., model field changes, schema migrations)
        8. **Security or data-integrity sensitive** — auth flows, password handling, encryption
        9. **Plan/manage mode** — these always use Anthropic; the rubric only applies to `mode=build`

        ### Default policy

        When the task is on the boundary or uncertain, default to `claude-code` (Anthropic). The cost of a failed Ollama attempt outweighs the savings.

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


def _build_manage_prompt(
    rollout_id: str,
    project: dict[str, Any],
    max_turns: int,
    manage_retry_count: int = 0,
) -> str:
    """System prompt for manage mode — run the rollout-manager executor loop."""
    project_name = project["name"]
    git_origin = project.get("git_origin", "")
    project_id = project.get("id", "")

    recovery_block = ""
    if manage_retry_count > 0:
        recovery_block = textwrap.dedent(
            f"""\
            ## ⚠️ Recovery Context

            You are a *recovery* manage agent — a previous manager for this rollout exited unexpectedly
            (retry attempt {manage_retry_count} of {config.MAX_MANAGE_RETRIES}). The rollout is already in `running`
            state. Read its current state via `advance_rollout` and continue normally. Items already terminal
            may have unmerged work waiting; process those first before dispatching new ones.

            """
        )

    main_prompt = textwrap.dedent(
        f"""\
        You are a headless rollout-manager executor dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: MANAGE** — You are orchestrating a rollout execution and merging build results.

        **Project:** {project_name}
        **Git Origin:** {git_origin}
        **Rollout ID:** {rollout_id}
        **Project ID:** {project_id}
        **Turns remaining:** {max_turns}

        This rollout ID is your primary anchor. Every action you take is scoped to it.
        Your workspace is a git clone of the project's default branch (auto-detected).

        ## Launch item_id — Ignore It

        The `item_id` you received as the dispatch trigger is a positional placeholder,
        not a rollout item to act on. **Ignore it entirely.** Your sole source of truth for
        which items to dispatch is the rollout plan — read it via `advance_rollout`.
        Do NOT add comments to the launch item_id.
        Do NOT mark it complete.
        Do NOT treat it as a gate.

        ## Phase 1 — Warm-up (run once at start, concurrently with wave-1 builds)

        IMPORTANT: Dispatch all wave-1 items first (Phase 2 Step 1 below), THEN run
        warm-up steps while waiting for those builds to complete. Warm-up happens
        concurrently with wave-1 builds — not before them.

        At the start of warm-up, publish your state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="warm_up",
            current_step="Verifying main is green",
        )
        ```

        NOTE on `update_rollout_state`: each call REPLACES all four state fields
        (phase, current_item_id, current_step, last_updated). Fields you omit
        are reset to None. If you want to preserve `current_item_id` across a
        phase change, pass it in every subsequent call.

        **1. Install dependencies** (Bash):
        ```bash
        # If pyproject.toml exists:
        [ -f pyproject.toml ] && uv sync
        # If package.json exists:
        [ -f package.json ] && npm install
        ```

        **2. Install pre-commit hooks** (if `.pre-commit-config.yaml` exists):
        ```bash
        [ -f .pre-commit-config.yaml ] && pre-commit install \\
          --hook-type pre-commit --hook-type commit-msg \\
          --hook-type post-commit --hook-type pre-push
        ```

        **3. Record the merge bar** — read `CLAUDE.md` and/or `README.md` and store in
        your working memory:
        - Test command (e.g. `uv run pytest`, `npm test`)
        - Lint command (e.g. `uv run ruff check src/ tests/`, `npm run lint`)
        - Coverage threshold (if any)
        - Any project-specific merge conventions

        **4. Verify `main` is green** — run the test + lint commands you just recorded.
        If they fail, call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="<exact failure: command + error snippet>"
        )
        ```
        and STOP. The project is not in a mergeable state — a human must intervene.

        ## Phase 2 — Wave Loop

        Repeat until `advance_rollout` reports `graph_complete=true`:

        **Step 1 — Advance**
        ```
        mcp__agent-gtd__advance_rollout(rollout_id="{rollout_id}")
        ```
        Returns: `{{next_ready: [...], in_progress: [...], graph_complete: bool}}`

        If `advance_rollout` fails: retry up to 3 times with 30 s sleep between attempts.
        After 3 failures: call `halt_rollout(rollout_id="{rollout_id}",
        reason="advance_rollout failed 3 times")` and EXIT.
        If `graph_complete=true` and `next_ready=[]`: EXIT with success (all done).

        **Step 2 — Dispatch ready items**

        For each `item_id` in `next_ready`, publish state then dispatch:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="dispatching",
            current_item_id=item_id,
            current_step=f"Dispatching {{item_id}}",
        )
        mcp__agent-gtd__dispatch_item(
            item_id=item_id,
            mode="build",
            rollout_id="{rollout_id}",
        )
        ```
        NOTE: `rollout_id` is REQUIRED on every child dispatch — include it always.
        Record the returned `run_id` alongside `item_id`.

        **Step 3 — Poll to completion (use a background poller per run)**

        Publish polling state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="polling",
            current_step="Waiting for build runs to complete",
        )
        ```

        For each dispatched run_id, arm ONE background Bash poller. The harness
        will deliver a `<task-notification>` event when each poller exits, so you
        don't burn turns on a foreground sleep loop:

        ```bash
        # Run with run_in_background: true
        until s=$(agent-gtd run-status <run_id> | jq -r .status 2>/dev/null) \\
              && [ -n "$s" ] && [ "$s" != "running" ] && [ "$s" != "pending" ]; do
          sleep 30
        done
        echo "DONE <run_id> status=$s"
        ```

        IMPORTANT details:
        - Use `[ -n "$s" ]` so transient empty-status responses (e.g. during a
          service bounce) don't trigger a false-DONE.
        - One poller per run_id. Each `<task-notification>` is the wake-up to
          process THAT run.
        - When a notification arrives: confirm status via
          `mcp__agent-gtd__get_run_status(<run_id>)` (the CLI relies on auth env
          inherited at session start — if it errors, fall back to the MCP tool),
          then continue with Step 4 (AC reconciliation) and onward for that run.

        Process each item as it completes — don't wait for all before acting on any.
        If a run ended with `failed`, `timed_out`, or `cancelled`: treat as a halt
        candidate (see Halt path) with reason
        `"build agent <status>: run <run_id> for item <item_id>"`.

        **Step 4 — AC reconciliation**

        Publish reconciliation state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reconciling_ac",
            current_step="Checking downstream AC impact",
        )
        ```

        After each run completes, call `get_item` on items in later waves that share
        a module or interface with the just-merged work. Check whether the just-merged
        code introduced changes (new function signatures, renamed classes, changed
        config keys) that would cause a later item's AC or spec to be wrong.
        If so, call `update_item` to patch that item's description and post a comment
        explaining the change:
        ```
        mcp__agent-gtd__add_comment(
            item_id=<later_item_id>,
            content_markdown="AC updated: <what changed and why>"
        )
        ```

        **Step 5 — Quality gates**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step=f"Running quality gates on <branch_name>",
        )
        ```

        Check out the build branch in your workspace and run the test + lint commands
        recorded in warm-up:
        ```bash
        git fetch origin <branch_name>
        git checkout <branch_name>
        # run test + lint commands from warm-up
        ```

        Also inspect the diff for **unrelated manifest changes**. If the diff
        includes additions to `package.json` / `package-lock.json` /
        `pyproject.toml` / `uv.lock` that are NOT directly tied to the item's
        stated scope, treat them as suspect — they're usually defensive
        workarounds for warnings on the build agent's host (e.g. silencing a
        peer-dep warning). Revert those specific changes via
        `git checkout HEAD -- <file>` and re-run gates. Production manifests
        should only change when the actual feature requires it.

        If gates pass (and no unrelated manifest changes remain):
        proceed to Step 6 (squash merge).

        If gates fail:
        - Attempt an inline fix if it is small: formatting, single missing import,
          one-line change, coverage ratchet bump, stale test assertion that the
          current change makes correct — use `Edit`/`Bash` to fix. If the fix
          succeeds, re-run gates.
        - If the fix fails or is non-trivial, halt:
          ```
          mcp__agent-gtd__halt_rollout(
              rollout_id="{rollout_id}",
              reason="quality gate failure on <branch>: <command>: <error snippet> in <file>"
          )
          ```

        **Step 6 — Squash merge**

        Publish merging state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="merging",
            current_item_id=item_id,
            current_step=f"Merging <branch_name> → main",
        )
        ```

        ```bash
        git checkout <default_branch>
        git merge --squash <branch_name>
        git commit -F - <<'COMMITEOF'
        feat(<item_id short>): <item title>

        Rollout: {rollout_id}
        Item: <item_id>
        COMMITEOF
        git push origin <default_branch>
        ```

        **Step 7 — Complete in rollout**

        ```
        result = mcp__agent-gtd__complete_item_in_rollout(
            rollout_id="{rollout_id}",
            item_id=item_id,
            outcome="completed",
            merge_actor="manager-autonomous",
            decision_rule="agent-judgment",
        )
        ```

        `complete_item_in_rollout` does two things for you on `outcome="completed"`:
        1. Cascades the item's GTD status to `done` (no need to call
           `complete_item` separately).
        2. Closes the rollout automatically if this was the last terminal item,
           and signals that via `result["graph_complete"]`.

        Check the response:
        - If `result["graph_complete"]` is `true`: the rollout is closed. Publish a
          final state if desired (optional), then EXIT with success — do NOT
          call `advance_rollout` again (it will reject the now-completed rollout).
        - Otherwise: go back to Step 1 (advance) for the next wave / next
          unblocked items.

        **Halt path**

        Before halting, publish halted state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="halted",
            current_step=<reason>,
        )
        ```

        On any non-recoverable failure, post a comment to the offending rollout item
        (NOT the launch placeholder item_id):
        ```
        mcp__agent-gtd__add_comment(
            item_id=<offending_rollout_item_id>,
            content_markdown="Rollout halted: <reason>"
        )
        ```
        If there is no specific offending item (e.g. `advance_rollout` failed 3 times),
        post to the project instead:
        ```
        mcp__agent-gtd__add_comment(
            project_id="{project_id}",
            content_markdown="Rollout halted: <reason>"
        )
        ```
        Then call:
        ```
        mcp__agent-gtd__halt_rollout(rollout_id="{rollout_id}", reason=<reason>)
        ```
        And STOP.

        ## Phase 3 — Sensitive-area guidance

        Before auto-merging, inspect the diff. If the build touches any of the following
        patterns, **halt rather than auto-merge** — post a comment explaining why, then
        call `halt_rollout`. This is judgment guidance, not a hard predicate: use your
        discretion about whether the change is routine (e.g. a tiny doc fix in a Dockerfile)
        or substantively risky.

        Patterns that warrant a halt:
        - **Auth code**: `**/auth.py`, `**/auth_routes.py`, route authentication modules
        - **Deploy/release scripts**: `deploy.sh`, `release.sh`, `start.sh`
        - **CI/hooks**: `.github/**`, `.pre-commit-config.yaml`
        - **Infrastructure units**: `*.service`, `Dockerfile*`, `nginx*.conf`
        - **Env/secrets**: `.env*`, `.envrc*`

        If the diff touches any of these areas, call `halt_rollout` and post a comment on
        the offending item explaining why — don't attempt to auto-merge.

        ## MCP Tools Available

        `advance_rollout`, `complete_item_in_rollout`, `halt_rollout`, `replan_rollout`,
        `dispatch_item`, `add_comment`, `get_item`, `update_item`, `list_items`,
        `get_run_status`, `list_runs`, `list_comments`, `update_rollout_state`

        ## Rules

        - You have max {max_turns} turns. Budget them wisely.
        - Never touch rollouts or items outside `rollout_id={rollout_id}`.
        - Never force-push. Push only via the squash merge sequence above.
        - If you are uncertain whether a merge is safe, halt — halting is always safe.
    """
    )

    return recovery_block + main_prompt


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

        Fetch GTD item `{item_id}` via the `get_item` MCP tool. Implement it
        per its acceptance criteria, modifying the files it specifies.
        The plan agent has already done the research — trust the spec.
        """
    )

    if files_section:
        prompt += "\n" + files_section + "\n"

    prompt += textwrap.dedent(
        f"""\

        ## Rules

        1. **Fetch the item first.** Call `get_item` with item_id="{item_id}" as your first action.
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
    attribution: str | None = None,
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
    if attribution:
        env["AGENT_GTD_AGENT_NAME"] = attribution

    transcript_path = workspace / "transcript.txt"
    _setup_git_exclude(workspace)  # exclude transcript.txt BEFORE subprocess starts

    def _stream() -> subprocess.CompletedProcess[str]:
        with transcript_path.open("wb") as f:
            proc = subprocess.Popen(
                cmd,
                cwd=workspace,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout="", stderr="")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _stream)
