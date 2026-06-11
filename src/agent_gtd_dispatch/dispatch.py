"""Core dispatch logic — workspace prep, prompt building, agent invocation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import subprocess
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable

from agent_gtd_dispatch_protocol.branches import make_branch_name
from agent_gtd_dispatch_protocol.models import DispatchMode

from . import config, gtd_client
from .engines import Engine, build_env
from .models import PushStatus, RepoPushStatus

logger = logging.getLogger(__name__)

_executor: concurrent.futures.ThreadPoolExecutor | None = None

_DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master")


def _sudo_wrap(cmd: list[str]) -> list[str]:
    """Prepend sudo -u <user> -H when AGENT_SUBPROCESS_USER is set."""
    if config.AGENT_SUBPROCESS_USER:
        return ["sudo", "-u", config.AGENT_SUBPROCESS_USER, "-H", *cmd]
    return cmd


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


branch_name_for_item = make_branch_name


def repo_dir_from_url(url: str) -> str:
    """Extract a directory name from a git clone URL.

    Takes the segment after the last '/' or ':' (whichever appears later),
    strips a trailing '.git', and returns the result.  Raises ValueError if
    the result is empty.

    Examples::

        repo_dir_from_url('git@host:org/repo.git')          → 'repo'
        repo_dir_from_url('https://host/org/repo.git')      → 'repo'
        repo_dir_from_url('ssh://git@ubuntu-vm01/~/repos/agent_gtd') → 'agent_gtd'
        repo_dir_from_url('git@host:repo.git')              → 'repo'  (SCP, no slash)
    """
    # Tolerate a single trailing slash (e.g. from user copy-paste)
    url = url.rstrip("/")
    last_slash = url.rfind("/")
    last_colon = url.rfind(":")
    sep_pos = max(last_slash, last_colon)
    segment = url[sep_pos + 1 :] if sep_pos >= 0 else url
    if segment.endswith(".git"):
        segment = segment[:-4]
    if not segment:
        raise ValueError(f"Cannot determine repo directory from URL: {url!r}")
    return segment


def prepare_workspace(origin: str, run_id: str, branch_name: str) -> Path:
    """Clone the repo and check out a feature branch for this run."""
    name = repo_name_from_origin(origin)
    workspace = config.WORKSPACE_ROOT / f"{name}-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _sudo_wrap(["git", "clone", origin, str(workspace)]),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _sudo_wrap(["git", "checkout", "-b", branch_name]),
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    return workspace


def prepare_workspace_multi(
    repo_urls: list[str], run_id: str, branch_name: str
) -> Path:
    """Clone multiple repos and create a feature branch in each for this run.

    Workspace root is ``config.WORKSPACE_ROOT / f'ws-{run_id}'``.

    - Python-mkdirs only ``config.WORKSPACE_ROOT`` (not the workspace root).
    - Creates the workspace root via a sudo-wrapped ``mkdir -p`` so that under
      the two-user split (``AGENT_SUBPROCESS_USER`` set) the agent user owns it
      and the subsequent clones can write into it.
    - Clones each URL **in order** into ``<root>/<repo_dir_from_url(url)>``.
    - Checks out ``branch_name`` in every repo (service-side branch creation).
    - Returns the workspace root ``Path``.

    Raises ``ValueError`` before any subprocess if *repo_urls* is empty, if two
    URLs map to the same directory name, or if any URL produces an empty
    basename.  Raises ``RuntimeError`` on clone or checkout failure.
    """
    if not repo_urls:
        raise ValueError("workspace_repos must not be empty")

    # Validate / resolve directory names before touching the filesystem
    dir_names: list[str] = []
    for url in repo_urls:
        dir_names.append(repo_dir_from_url(url))  # raises ValueError on empty basename

    seen: set[str] = set()
    for name in dir_names:
        if name in seen:
            raise ValueError(f"Duplicate workspace repo directory: '{name}'")
        seen.add(name)

    # Python-mkdirs ONLY config.WORKSPACE_ROOT (mirrors prepare_workspace)
    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    root = config.WORKSPACE_ROOT / f"ws-{run_id}"

    # Create workspace root via subprocess so the agent user owns it
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root)]),
        check=True,
        capture_output=True,
    )

    for url, dir_name in zip(repo_urls, dir_names, strict=False):
        dest = root / dir_name

        result = subprocess.run(
            _sudo_wrap(["git", "clone", url, str(dest)]),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace clone failed for {url}: {stderr_tail}")

        result = subprocess.run(
            _sudo_wrap(["git", "checkout", "-b", branch_name]),
            cwd=dest,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace checkout failed for {url}: {stderr_tail}")

    return root


def get_head_sha(repo_path: Path) -> str:
    """Return the current HEAD SHA in repo_path (stripped)."""
    result = subprocess.run(
        _sudo_wrap(["git", "rev-parse", "HEAD"]),
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_pushes(
    repos: list[tuple[str, Path, str]],
    branch_name: str,
) -> list[RepoPushStatus]:
    """Verify that each repo has pushed its branch to origin.

    Args:
        repos: List of (repo_name, repo_path, base_sha) tuples.
        branch_name: The feature branch to check.

    Returns:
        Per-repo RepoPushStatus in the same order as *repos*.

    Classification order (fail-closed):
    1. Any git subprocess failure → unpushed (local_sha=None, remote_sha=None,
       commits_ahead=0, dirty=False)
    2. commits_ahead == 0 → no_changes
    3. remote_sha == local_sha → pushed
    4. else → unpushed
    """
    results: list[RepoPushStatus] = []
    for repo_name, repo_path, base_sha in repos:
        try:
            # local HEAD SHA
            local_proc = subprocess.run(
                _sudo_wrap(["git", "rev-parse", "HEAD"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if local_proc.returncode != 0:
                raise RuntimeError(f"git rev-parse HEAD failed: {local_proc.stderr}")
            local_sha = local_proc.stdout.strip()

            # commits ahead of base_sha
            ahead_proc = subprocess.run(
                _sudo_wrap(["git", "rev-list", f"{base_sha}..HEAD", "--count"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if ahead_proc.returncode != 0:
                raise RuntimeError(f"git rev-list failed: {ahead_proc.stderr}")
            commits_ahead = int(ahead_proc.stdout.strip())

            # remote SHA for the branch (empty output → branch not on remote)
            remote_proc = subprocess.run(
                _sudo_wrap(["git", "ls-remote", "origin", f"refs/heads/{branch_name}"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if remote_proc.returncode != 0:
                raise RuntimeError(f"git ls-remote failed: {remote_proc.stderr}")
            remote_sha: str | None = None
            ls_output = remote_proc.stdout.strip()
            if ls_output:
                # output format: "<sha>\trefs/heads/<branch>"
                remote_sha = ls_output.split()[0]

            # dirty check — only tracked-file modifications (untracked files ignored)
            dirty_proc = subprocess.run(
                _sudo_wrap(["git", "status", "--porcelain", "--untracked-files=no"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if dirty_proc.returncode != 0:
                raise RuntimeError(f"git status failed: {dirty_proc.stderr}")
            dirty = bool(dirty_proc.stdout.strip())

            # Classification
            if commits_ahead == 0:
                status = PushStatus.no_changes
            elif remote_sha == local_sha:
                status = PushStatus.pushed
            else:
                status = PushStatus.unpushed

        except Exception:
            logger.exception("verify_pushes: git command failed for repo %s", repo_name)
            results.append(
                RepoPushStatus(
                    repo_name=repo_name,
                    branch=branch_name,
                    status=PushStatus.unpushed,
                    local_sha=None,
                    remote_sha=None,
                    commits_ahead=0,
                    dirty=False,
                )
            )
            continue

        results.append(
            RepoPushStatus(
                repo_name=repo_name,
                branch=branch_name,
                status=status,
                local_sha=local_sha,
                remote_sha=remote_sha,
                commits_ahead=commits_ahead,
                dirty=dirty,
            )
        )

    return results


def _detect_default_branch(repo_path: Path) -> str:
    """Detect the default branch for a cloned repo (detection only, no checkout).

    Steps:
    1. git remote set-head origin --auto  (non-fatal if it fails)
    2. git symbolic-ref --short refs/remotes/origin/HEAD  → extract branch name
    3. If step 2 fails, probe remote branches via git branch -r against
       _DEFAULT_BRANCH_CANDIDATES (defaults to _DEFAULT_BRANCH_CANDIDATES[0])

    Returns the detected default branch name (e.g. 'main' or 'master').
    """
    subprocess.run(
        _sudo_wrap(["git", "remote", "set-head", "origin", "--auto"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    result = subprocess.run(
        _sudo_wrap(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        branches_result = subprocess.run(
            _sudo_wrap(["git", "branch", "-r", "--format=%(refname:short)"]),
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        remote_branches = branches_result.stdout.splitlines()
        default_branch = _DEFAULT_BRANCH_CANDIDATES[0]
        for candidate in _DEFAULT_BRANCH_CANDIDATES:
            if f"origin/{candidate}" in remote_branches:
                default_branch = candidate
                break
    else:
        default_branch = result.stdout.strip().removeprefix("origin/")
    return default_branch


def prepare_manage_workspace(git_origin: str, run_id: str) -> Path:
    """Clone the repo for manage mode and detect the default branch.

    Steps:
    1. git clone --depth=50 {git_origin} {workspace}
    2. git remote set-head origin --auto  (populate HEAD ref; non-fatal if it fails)
    3. git symbolic-ref --short refs/remotes/origin/HEAD  → detect default branch
    4. If step 3 fails, probe remote branches via git branch -r and pick the first
       match from _DEFAULT_BRANCH_CANDIDATES (defaults to _DEFAULT_BRANCH_CANDIDATES[0])
    5. git checkout {default_branch}  (explicit, stays on default branch)

    Returns the workspace path.
    """
    workspace = config.WORKSPACE_ROOT / f"repos-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _sudo_wrap(["git", "clone", "--depth=50", git_origin, str(workspace)]),
        check=True,
        capture_output=True,
    )
    default_branch = _detect_default_branch(workspace)
    subprocess.run(
        _sudo_wrap(["git", "checkout", default_branch]),
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    return workspace


def prepare_manage_workspace_multi(repo_urls: list[str], run_id: str) -> Path:
    """Clone multiple repos for manage mode, each checked out on its default branch.

    Workspace root is ``config.WORKSPACE_ROOT / f'repos-{run_id}'``.

    - Python-mkdirs only ``config.WORKSPACE_ROOT`` (not the workspace root).
    - Creates the workspace root via a sudo-wrapped ``mkdir -p`` so that under
      the two-user split (``AGENT_SUBPROCESS_USER`` set) the agent user owns it.
    - Clones each URL **in order** with ``--depth=50`` into
      ``<root>/<repo_dir_from_url(url)>``.
    - Detects and checks out each repo's default branch (no feature branch).
    - Returns the workspace root ``Path``.

    Raises ``ValueError`` before any subprocess if *repo_urls* is empty, if two
    URLs map to the same directory name, or if any URL produces an empty
    basename.  Raises ``RuntimeError`` on clone or checkout failure.
    """
    if not repo_urls:
        raise ValueError("workspace_repos must not be empty")

    # Validate / resolve directory names before touching the filesystem
    dir_names: list[str] = []
    for url in repo_urls:
        dir_names.append(repo_dir_from_url(url))  # raises ValueError on empty basename

    seen: set[str] = set()
    for name in dir_names:
        if name in seen:
            raise ValueError(f"Duplicate workspace repo directory: '{name}'")
        seen.add(name)

    # Python-mkdirs ONLY config.WORKSPACE_ROOT (mirrors prepare_workspace_multi)
    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    root = config.WORKSPACE_ROOT / f"repos-{run_id}"

    # Create workspace root via subprocess so the agent user owns it
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root)]),
        check=True,
        capture_output=True,
    )

    for url, dir_name in zip(repo_urls, dir_names, strict=False):
        dest = root / dir_name

        result = subprocess.run(
            _sudo_wrap(["git", "clone", "--depth=50", url, str(dest)]),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace clone failed for {url}: {stderr_tail}")

        default_branch = _detect_default_branch(dest)

        result = subprocess.run(
            _sudo_wrap(["git", "checkout", default_branch]),
            cwd=dest,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace checkout failed for {url}: {stderr_tail}")

    return root


def cleanup_workspace(workspace: Path) -> None:
    """Remove a workspace directory after a run completes."""
    if workspace.exists() and config.WORKSPACE_ROOT in workspace.parents:
        if config.AGENT_SUBPROCESS_USER:
            subprocess.run(_sudo_wrap(["rm", "-rf", str(workspace)]), check=False)
        else:
            import shutil

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


def _build_workspace_layout_section_build(
    workspace_repo_dirs: list[str], branch_name: str
) -> str:
    """'## Workspace Layout' section for build-mode workspace runs."""
    repo_bullets = "\n".join(f"- `{d}/`" for d in workspace_repo_dirs)
    return textwrap.dedent(
        f"""\
        ## Workspace Layout

        Your working directory is a **workspace root** containing these cloned repos:

        {repo_bullets}

        Branch `{branch_name}` is already created and checked out in every repo —
        the service did this before launching. **Never create branches.**

        - **Commit** your changes in whichever repos you modify.
        - **Push `{branch_name}` to origin ONLY in repos where you made commits.**
        - After each push, verify the remote ref advanced. Run in that repo's directory:
          ```bash
          git ls-remote origin refs/heads/{branch_name}
          ```
          Compare the returned SHA against `git rev-parse HEAD`. If the SHAs do not match
          (or no SHA is returned), post a failure comment and do NOT set item status to `review`.
        - Your final success comment **MUST list exactly which repos you pushed to**."""
    )


def _build_workspace_layout_section_plan(workspace_repo_dirs: list[str]) -> str:
    """'## Workspace Layout' section for plan-mode workspace runs (read-only)."""
    repo_bullets = "\n".join(f"- `{d}/`" for d in workspace_repo_dirs)
    return textwrap.dedent(
        f"""\
        ## Workspace Layout

        Your working directory is a **workspace root** containing these cloned repos:

        {repo_bullets}

        Explore across all of them as needed to understand the codebase."""
    )


def build_system_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    branch_name: str | None,
    max_turns: int,
    mode: DispatchMode = DispatchMode.BUILD,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    rollout_id: str | None = None,
    manage_retry_count: int = 0,
    workspace_repo_dirs: list[str] | None = None,
) -> str:
    """Build the headless agent system prompt."""
    if mode == DispatchMode.PLAN:
        return _build_plan_prompt(
            item,
            project,
            max_turns,
            attachments=attachments,
            run_id=run_id,
            workspace_repo_dirs=workspace_repo_dirs,
        )
    if mode == DispatchMode.MANAGE:
        return _build_manage_prompt(
            rollout_id or "",
            project,
            max_turns,
            manage_retry_count=manage_retry_count,
            workspace_repo_dirs=workspace_repo_dirs,
        )
    return _build_build_prompt(
        item,
        project,
        branch_name or "",
        max_turns,
        attachments=attachments,
        run_id=run_id,
        workspace_repo_dirs=workspace_repo_dirs,
    )


def _build_plan_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    max_turns: int,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    workspace_repo_dirs: list[str] | None = None,
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

    if workspace_repo_dirs:
        prompt += (
            "\n" + _build_workspace_layout_section_plan(workspace_repo_dirs) + "\n"
        )

    prompt += textwrap.dedent(
        f"""\

        ## Before You Begin

        Before writing any acceptance criteria, complete these three steps:

        1. **Read repo conventions** — Read `docs/codebase.md`, `docs/architecture.md`,
           `docs/domain.md` (any that exist). Fall back to `CLAUDE.md` if none found;
           note the gap in the plan output.
        2. **Search the KB** — Call `kb_search(project_ref="{project_name}")` to skim for
           relevant conventions, anti-patterns, and prior decisions. Pull applicable
           `kb-XXXXX` IDs into the plan output.
        3. **Architectural-awareness sweep** — Before finalizing AC, explicitly call out:
           - **Magic strings**: should any be a `Literal` type or enum instead of bare strings?
           - **Duplication risk**: does this logic risk duplicating something already in a
             shared module or utility?
           - **Typed data homes**: do any data shapes already have a typed home (Pydantic
             model, TypedDict, or dataclass)?
           State "No architectural concerns found" if clean.
        """
    )

    prompt += textwrap.dedent(
        f"""\

        ## What to do

        1. **Read the codebase.** Understand existing patterns, architecture, and conventions.
        2. **Write structured fields.** Call `update_item` with the structured fields — legality
           validation reads these, not description prose, so these calls are mandatory:
           - `acceptance_criteria`: list of testable AC strings, e.g.
             `acceptance_criteria=["AC-1: the widget renders", "AC-2: tests pass"]`
           - `files_to_modify`: list of dicts with `"path"` and `"change"` keys, e.g.
             `files_to_modify=[{{"path": "src/foo.py", "change": "add error handling"}}, ...]`
           - `scope_out`: list of things explicitly out of scope, e.g.
             `scope_out=["Do NOT change the API surface", "Do NOT touch unrelated modules"]`
           Free-form `description` is still fine for context/lead paragraph, but the structured
           fields are the source of truth that the legality validator checks.
        3. **Add patterns to follow.** Reference existing code the implementer should copy.
        4. **Define scope boundaries.** Explicitly state what NOT to touch (use `scope_out`).
        5. **Add verification steps.** How to test the changes (commands, expected output).
        6. **Select build engine.** Evaluate this task against the Engine-Selection Rubric below.
           - Route to one of the three engines per the rubric criteria.
           - Call `update_item(build_engine="<engine-name>")` if routing to anything other than the
             default (e.g. `build_engine="claude-code-haiku"` or `"claude-code-sonnet"`).
           - Leave `build_engine` unset (don't call update_item for it) to route to `claude-code`
             (default Opus).
           - When uncertain, route UP (toward Opus), not down.
        7. **Ask questions if unclear.** If the intent is ambiguous, post a comment asking
           for clarification and stop. Do NOT guess.

        ## Rules

        - Do NOT write code, create branches, or push anything.
        - Do NOT modify any files in the repo.
        - Use `update_item` (with the item's current version) to set structured fields
          (`acceptance_criteria`, `files_to_modify`, `scope_out`) and optionally `description`.
        - **Legality validation reads `acceptance_criteria` and `files_to_modify` from the
          structured fields only** — prose Markdown in `description` is ignored by the validator.
        - Use `add_comment` with item_id="{item_id}" for questions or notes.
        - When grooming is complete, set item status to `ready` using `update_item`.

        ## Engine-Selection Rubric

        Three engines are available for build-mode dispatches:

        - **`claude-code-haiku`** — cloud Haiku 4.5, very cheap, fast, weak-ish reasoning
        - **`claude-code-sonnet`** — cloud Sonnet 4.6, medium cost, fast, strong reasoning for well-scoped work
        - **`claude-code` (default Opus)** — cloud Opus, expensive, slower, most capable reasoning

        ### Route to `claude-code-haiku` when ALL of these hold

        1. **Single-file or tightly bounded** — changes touch 1-3 files, no orchestration across modules
        2. **Pattern-following** — the AC can be expressed as "make X look like Y"; a clear template exists
        3. **Mechanical edits dominate** — renames, string/copy changes, format fixes, type tightening, null guards
        4. **Tests are clone-and-modify** — new tests fit an existing test pattern; no novel test design
        5. **No cross-cutting decisions or novel design** — right place is obvious from AC; no judgment needed
        6. **Wall-clock speed matters** — Haiku completes in <60 s

        ### Route to `claude-code-sonnet` when

        - Item has populated `acceptance_criteria` and `files_to_modify` structured fields AND
        - Task is too complex for mechanical pattern-matching (4+ files, or per-file logic is non-trivial), BUT
        - No novel design decisions, no debugging, no cross-cutting judgment
        - "Well-scoped non-trivial" sweet spot — the plan agent did the thinking, builder needs strong execution

        ### Route to `claude-code` (default Opus) when ANY of these hold

        1. Multi-file orchestration with coordinating intent across modules
        2. Novel design decisions ("decide whether…", "design a way to…")
        3. Debugging (root cause not named in the description)
        4. Cross-cutting concerns (auth, error handling, migrations, threading)
        5. New API/protocol surface
        6. Test design from scratch
        7. Wide blast radius (model field changes, schema migrations affecting many consumers)
        8. Security or data-integrity sensitive
        9. Plan/manage mode — these always use the default; rubric only applies to `mode=build`

        ### Default policy

        When uncertain, route UP (toward Opus), not down. The cost of a failed cheap-engine attempt (re-dispatch + lead intervention) outweighs the savings.

        ## Reporting

        Post a comment when you start: "Planning..."

        **On success:**
        1. Post a comment summarizing the structured fields you set and the build engine selected
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


def _build_manage_workspace_main_prompt(
    rollout_id: str,
    project: dict[str, Any],
    max_turns: int,
    workspace_repo_dirs: list[str],
) -> str:
    """Workspace-variant manage prompt: per-repo review, merge, push, cleanup."""
    project_name = project["name"]
    project_id = project.get("id", "")
    repo_bullets = ("\n        ").join(f"- `{d}/`" for d in workspace_repo_dirs)
    first_repo = workspace_repo_dirs[0]
    repos_order = " → ".join(f"`{d}/`" for d in workspace_repo_dirs)

    return textwrap.dedent(
        f"""\
        You are a headless rollout-manager executor dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: MANAGE** — You are orchestrating a rollout execution and merging build results.

        **Project:** {project_name}
        **Workspace Repos:**
        {repo_bullets}
        **Rollout ID:** {rollout_id}
        **Project ID:** {project_id}
        **Turns remaining:** {max_turns}
        **Time budget:** {config.MANAGE_TIMEOUT_SECONDS // 3600} hours ({config.MANAGE_TIMEOUT_SECONDS // 60} min) of wall-clock time. Up to {config.MAX_MANAGE_RETRIES} automatic relaunches on timeout — but each relaunch rebuilds context from rollout state. Complete as many waves as possible per run.

        This rollout ID is your primary anchor. Every action you take is scoped to it.
        Your workspace is a **workspace root** containing one git clone per repo listed above, each checked out on its own default branch (auto-detected).

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
            current_step="Verifying Workspace Repos are green",
        )
        ```

        NOTE on `update_rollout_state`: each call REPLACES all four state fields
        (phase, current_item_id, current_step, last_updated). Fields you omit
        are reset to None. If you want to preserve `current_item_id` across a
        phase change, pass it in every subsequent call.

        For EACH repo in Workspace Repos (run all five steps in that repo's directory):

        **1. Record the default branch** — run BEFORE any other checkout in this repo:
        ```bash
        cd <repo_dir>
        git rev-parse --abbrev-ref HEAD
        ```
        Store this as `<repo_dir>_default_branch` in your working memory.

        **2. Install dependencies**:
        ```bash
        [ -f pyproject.toml ] && uv sync
        [ -f package.json ] && npm install
        ```

        **3. Install pre-commit hooks** (if `.pre-commit-config.yaml` exists):
        ```bash
        [ -f .pre-commit-config.yaml ] && pre-commit install \\
          --hook-type pre-commit --hook-type commit-msg \\
          --hook-type post-commit --hook-type pre-push
        ```

        **4. Record the merge bar** — read `CLAUDE.md` and/or `README.md` for this repo:
        - Test command (e.g. `uv run pytest`, `npm test`)
        - Lint command (e.g. `uv run ruff check src/ tests/`, `npm run lint`)
        - Coverage threshold (if any)
        - Any project-specific merge conventions

        If a repo has no discoverable test or lint command, record `none` for that repo
        and continue — do NOT halt.

        **5. Verify that repo's default branch is green** — run the test + lint commands
        you recorded. If they fail, call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="warm-up failure in <repo_dir>: <command>: <error snippet>"
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

        **Step 5a — Discover pushed repos**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step="Discovering pushed repos for <branch_name>",
        )
        ```

        For each completed build item, determine which Workspace Repos received the feature
        branch. Run in EVERY repo directory:
        ```bash
        cd <repo_dir>
        git ls-remote origin refs/heads/<branch_name>
        ```
        Non-empty output = that repo has the branch. `git ls-remote` is AUTHORITATIVE for
        push verification.

        Also cross-check against the build agent's final success comment. Build agents are
        REQUIRED to list exactly which repos they pushed to. Both disagreement directions
        require a halt — name both sources in the halt reason:
        - If the build comment claims a repo received the branch but `git ls-remote` does
          NOT confirm it: halt — the push was reported as success but the remote ref is absent.
        - If `git ls-remote` shows the branch in a repo that the build comment did NOT list:
          halt — there is unreported partial work in that repo.

        Do NOT use `get_run_status` to determine push verification — the `claude_runs` schema
        has no `push_results` column; structured push results exist only in `git ls-remote`
        output and build agent comments.

        **Step 5b — Review all pushed repos (quality gates — ALL before any merge)**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step="Running quality gates across all pushed repos for <branch_name>",
        )
        ```

        For EACH pushed repo (run inside that repo's directory):
        ```bash
        cd <repo_dir>
        git fetch origin <branch_name>
        git checkout <branch_name>
        # run that repo's recorded test + lint commands
        ```

        Also inspect the diff for **unrelated manifest changes**. If the diff
        includes additions to `package.json` / `package-lock.json` /
        `pyproject.toml` / `uv.lock` that are NOT directly tied to the item's
        stated scope, treat them as suspect — revert those specific changes via
        `git checkout HEAD -- <file>` and re-run gates.

        **Inline-fix phase boundary:**
        - While ZERO repos for this item have been merged: if a gate failure is small
          (formatting, single missing import, one-line change, coverage ratchet bump,
          stale test assertion), apply an inline fix with `Edit`/`Bash` and re-run gates.
          If the fix fails or is non-trivial, halt.
        - ALL pushed repos must pass quality gates before merging any repo.
        - Once the FIRST repo for an item is merged+pushed, ANY subsequent failure halts
          IMMEDIATELY — no inline fixes, no retries.

        **Step 6 — Squash merge (repo-by-repo in Workspace Repos list order)**

        Publish merging state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="merging",
            current_item_id=item_id,
            current_step=f"Merging <branch_name> repo-by-repo",
        )
        ```

        Merge order: {repos_order} (Workspace Repos list order — pushed repos only).

        For EACH pushed repo, in list order:

        1. Run the commit-count guard immediately before this repo's squash merge.
           Use THAT repo's default branch recorded in warm-up (never a global value):
           ```bash
           cd <repo_dir>
           git fetch origin <branch_name>
           commit_count=$(git rev-list origin/<repo_default_branch>..<branch_name> --count)
           ```
           If `commit_count` is 0: halt with the multi-repo halt template below —
           step = `commit-count-guard`.

        2. Squash merge sequence (inside that repo's directory, against THAT repo's default branch):
           ```bash
           git checkout <repo_default_branch>
           git merge --squash <branch_name>
           git commit -F - <<'COMMITEOF'
           feat(<item_id short>): <item title>

           Rollout: {rollout_id}
           Item: <item_id>
           COMMITEOF
           git push origin <repo_default_branch>
           ```

        Record `merged+pushed (<sha>)` for this repo after a successful push.

        Once the FIRST repo for an item is merged+pushed, ANY subsequent failure
        (`fetch` | `gates` | `commit-count-guard` | `squash-merge` | `push` | `branch-cleanup`)
        halts IMMEDIATELY — no inline fixes, no retries.

        Use this EXACT halt template for multi-repo merge failures:
        ```
        Rollout halted: multi-repo merge failure on item <item_id> (branch <branch_name>)
        Per-repo state:
        - <repo_dir>: merged+pushed (<merge commit sha>)
        - <repo_dir>: FAILED — <step>: <error snippet>
        - <repo_dir>: untouched
        ```
        Exactly three per-repo states: `merged+pushed`, `FAILED`, `untouched`.
        `<step>` is one of: `fetch` | `gates` | `commit-count-guard` | `squash-merge` | `push` | `branch-cleanup`.

        **Absolute prohibitions (never cross these):**
        - Do NOT roll back or revert already merged+pushed repos.
        - Do NOT force-push.
        - Do NOT continue merging remaining repos after a failure.

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
        - If `result["graph_complete"]` is `true`: the rollout is closed. Before
          exiting, run cleanup (feature-branch deletion and manage-branch cleanup
          below), then EXIT with success — do NOT call `advance_rollout` again
          (it will reject the now-completed rollout).
        - Otherwise: go back to Step 1 (advance) for the next wave / next
          unblocked items.

        **Cleanup — after all pushed repos for an item are merged+pushed**

        Feature branch cleanup (run in EACH pushed repo's directory):
        ```bash
        cd <repo_dir>
        git push origin --delete <branch_name>
        git branch -D <branch_name>
        ```

        Manage branch cleanup — run ONCE in `{first_repo}/` only, NOT repeated per repo:
        ```bash
        cd {first_repo}
        git push origin --delete feat/{rollout_id[:8]}-manage || true
        ```

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

        ## Guardrails — Never Lower the Quality Bar

        These rules are absolute. No circumstance justifies violating them.

        **Coverage threshold — ratchets up only:**
        - NEVER lower `[tool.coverage.report] fail_under` in `pyproject.toml`.
        - Coverage threshold ratchets up only. After adding tests that increase coverage,
          raise `fail_under` to lock in the gain — never edit it downward.
        - If a build fails the pre-push coverage gate, your only options are:
          1. Add tests to recover coverage (fix the deficit properly).
          2. Halt the rollout and flag the lead — if the deficit is too large to fix inline.
        - A `chore: lower coverage threshold` commit is a guardrail violation. If you see
          one on the branch, revert it before merging.

        **Additional prohibitions — do not cross these lines:**
        - Do not comment out `pytest` hooks or skip the test suite.
        - Do not skip linting (`--skip` flags, removing lint steps, etc.).
        - Do not add blanket `# type: ignore` suppressions to silence type errors.
        - Do not use `git push --no-verify` to bypass pre-push hooks.

        When in doubt: halt. A halted rollout recovers. A merged regression does not.

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


def _build_manage_prompt(
    rollout_id: str,
    project: dict[str, Any],
    max_turns: int,
    manage_retry_count: int = 0,
    workspace_repo_dirs: list[str] | None = None,
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

    if workspace_repo_dirs:
        return recovery_block + _build_manage_workspace_main_prompt(
            rollout_id, project, max_turns, workspace_repo_dirs
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
        **Time budget:** {config.MANAGE_TIMEOUT_SECONDS // 3600} hours ({config.MANAGE_TIMEOUT_SECONDS // 60} min) of wall-clock time. Up to {config.MAX_MANAGE_RETRIES} automatic relaunches on timeout — but each relaunch rebuilds context from rollout state. Complete as many waves as possible per run.

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

        Before merging, run a commit-count guard to confirm the build agent actually
        pushed commits (guards against a build agent that reported success but pushed
        no commits):
        ```bash
        git fetch origin <branch_name>
        commit_count=$(git rev-list origin/<default_branch>..<branch_name> --count)
        ```
        If `commit_count` is 0 (branch has no commits beyond origin/main), the build
        agent reported success but pushed no commits. Call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="build agent reported success but pushed no commits: <branch_name> has no commits beyond origin/<default_branch>"
        )
        ```
        and STOP — do not attempt the squash merge.

        ```bash
        git checkout <default_branch>
        git merge --squash <branch_name>
        git commit -F - <<'COMMITEOF'
        feat(<item_id short>): <item title>

        Rollout: {rollout_id}
        Item: <item_id>
        COMMITEOF
        git push origin <default_branch>
        git push origin --delete <branch_name>
        git branch -D <branch_name>
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
        - If `result["graph_complete"]` is `true`: the rollout is closed. Before
          exiting, clean up the manage branch from origin:
          ```bash
          git push origin --delete feat/{rollout_id[:8]}-manage || true
          ```
          Then EXIT with success — do NOT call `advance_rollout` again (it will
          reject the now-completed rollout).
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

        ## Guardrails — Never Lower the Quality Bar

        These rules are absolute. No circumstance justifies violating them.

        **Coverage threshold — ratchets up only:**
        - NEVER lower `[tool.coverage.report] fail_under` in `pyproject.toml`.
        - Coverage threshold ratchets up only. After adding tests that increase coverage,
          raise `fail_under` to lock in the gain — never edit it downward.
        - If a build fails the pre-push coverage gate, your only options are:
          1. Add tests to recover coverage (fix the deficit properly).
          2. Halt the rollout and flag the lead — if the deficit is too large to fix inline.
        - A `chore: lower coverage threshold` commit is a guardrail violation. If you see
          one on the branch, revert it before merging.

        **Additional prohibitions — do not cross these lines:**
        - Do not comment out `pytest` hooks or skip the test suite.
        - Do not skip linting (`--skip` flags, removing lint steps, etc.).
        - Do not add blanket `# type: ignore` suppressions to silence type errors.
        - Do not use `git push --no-verify` to bypass pre-push hooks.

        When in doubt: halt. A halted rollout recovers. A merged regression does not.

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
    workspace_repo_dirs: list[str] | None = None,
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

    if workspace_repo_dirs:
        prompt += (
            "\n"
            + _build_workspace_layout_section_build(workspace_repo_dirs, branch_name)
            + "\n"
        )

    prompt += textwrap.dedent(
        f"""\

        ## Rules

        1. **Fetch the item first.** Call `get_item` with item_id="{item_id}" as your first action.
        2. **Branch.** You are already on branch `{branch_name}`. Stay on it. Never commit to main.
        3. **Test.** Run the project's test suite before committing. Fix failures.
        4. **Commit.** Use conventional commit messages. Small, focused commits.
        5. **Push.** When done, push `{branch_name}` to origin. After pushing, verify the remote
           ref advanced — run:
           ```bash
           git ls-remote origin refs/heads/{branch_name}
           ```
           Compare the returned SHA against `git rev-parse HEAD`. If the SHAs do not match
           (or no SHA is returned), post a failure comment and do NOT set item status to `review`.
        6. **Stop if stuck.** If the task is too ambiguous, you lack information, or
           you cannot complete it cleanly — STOP. Do not guess or produce low-quality work.{att_rule}

        ## No-Op Case — Work Already Done

        Before writing any code, check whether the acceptance criteria are **already satisfied**
        by existing code. If no source changes are needed:
        - Post a comment describing what already exists and why no changes were needed
          (e.g. "No changes needed — <feature> already implemented at <file>:<line>").
        - Do NOT push any commits.
        - Do NOT set item status to `review`. Leave it unchanged (stays `active`) so the lead
          has a clear signal that no new code was shipped.
        - STOP.

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
    mode: DispatchMode = DispatchMode.BUILD,
    attribution: str | None = None,
    popen_callback: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a headless agent CLI as a subprocess."""
    if timeout_seconds is None:
        timeout_seconds = (
            config.MANAGE_TIMEOUT_SECONDS
            if mode == DispatchMode.MANAGE
            else config.TIMEOUT_SECONDS
        )
    if engine.name == "kiro":
        (workspace / "system_prompt.md").write_text(
            f"{system_prompt}\n\n---\n\n## Task\n\n{title}"
        )
    cmd = engine.build_command(system_prompt, title, max_turns, agent_name)
    if allowed_tools is not None and engine.name == "claude-code":
        # Insert --allowedTools BEFORE --print.  claude's argparser breaks
        # when --allowedTools sits between --print and the positional prompt
        # ("Error: Input must be provided ... when using --print"); --print
        # must be the last flag before the prompt.
        print_idx = cmd.index("--print")
        cmd[print_idx:print_idx] = ["--allowedTools", ",".join(allowed_tools)]
    env = build_env(engine, mode=mode)
    if attribution:
        env["AGENT_GTD_AGENT_NAME"] = attribution

    cmd = _sudo_wrap(cmd)
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
            if popen_callback is not None:
                popen_callback(proc)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout="", stderr="")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _stream)
