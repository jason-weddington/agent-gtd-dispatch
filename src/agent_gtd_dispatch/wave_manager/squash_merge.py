"""Squash-merge a build-agent branch into main for the wave manager.

CLI entrypoint: python -m agent_gtd_dispatch.wave_manager.squash_merge

Usage::

    squash_merge.py --origin ORIGIN --branch BRANCH --item-id ITEM_ID
                    --wave-run-id WAVE_RUN_ID --decision-rule RULE

Steps:
  1. Create temp workspace under DISPATCH_WORKSPACE_ROOT.
  2. git clone --depth=50 <origin> <workspace>
  3. git fetch origin <branch> + git checkout <branch>
  4. CI gate (optional; ImportError → skip with warning; failure → exit 1)
  5. git checkout main + git pull origin main
  6. git merge --squash <branch>
  7. git commit -F - (heredoc message, never $() substitution)
  8. git push origin main
  9. Cleanup workspace (always, via finally).
  10. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys

from agent_gtd_dispatch import config

logger = logging.getLogger(__name__)


def main() -> None:
    """Squash-merge a completed build branch into main."""
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Squash-merge a build-agent branch into main for the wave manager.",
    )
    parser.add_argument("--origin", required=True, help="Git remote origin URL")
    parser.add_argument("--branch", required=True, help="Branch name to merge")
    parser.add_argument(
        "--item-id", required=True, dest="item_id", help="GTD item ID"
    )
    parser.add_argument(
        "--wave-run-id", required=True, dest="wave_run_id", help="Wave run ID"
    )
    parser.add_argument(
        "--decision-rule",
        required=True,
        dest="decision_rule",
        help="Allowlist rule name that approved this merge",
    )
    args = parser.parse_args()

    config.load()

    workspace = (
        config.WORKSPACE_ROOT
        / f"wave-merge-{args.wave_run_id[:8]}-{args.item_id[:8]}"
    )

    try:
        # Step 1 — ensure workspace root exists
        config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

        # Step 2 — git clone --depth=50
        result = subprocess.run(
            ["git", "clone", "--depth=50", args.origin, str(workspace)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        # Step 3 — fetch + checkout branch (verify it exists)
        result = subprocess.run(
            ["git", "fetch", "origin", args.branch],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"Branch {args.branch!r} not found on origin: {result.stderr}",
                file=sys.stderr,
            )
            sys.exit(1)

        result = subprocess.run(
            ["git", "checkout", args.branch],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"Failed to checkout {args.branch!r}: {result.stderr}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Step 4 — CI gate (optional)
        try:
            from agent_gtd_dispatch.wave_manager import (  # type: ignore[attr-defined]
                ci_gate,
            )

            try:
                ci_gate.run(workspace)
            except Exception as exc:
                print(f"CI gate failed: {exc}", file=sys.stderr)
                sys.exit(1)
        except ImportError:
            logger.warning("CI gate not available, skipping")

        # Step 5 — checkout main + pull
        result = subprocess.run(
            ["git", "checkout", "main"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        # Step 6 — git merge --squash
        result = subprocess.run(
            ["git", "merge", "--squash", args.branch],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Squash merge conflict: {result.stderr}", file=sys.stderr)
            sys.exit(1)

        # Step 7 — git commit via stdin (never $() substitution)
        commit_msg = (
            f"feat: squash merge {args.item_id[:8]} (wave-manager)\n\n"
            f"wave_run_id: {args.wave_run_id}\n"
            f"item_id: {args.item_id}\n"
            f"decision_rule: {args.decision_rule}\n"
            f"merge_actor: manager-allowlist"
        )
        result = subprocess.run(
            ["git", "commit", "-F", "-"],
            input=commit_msg,
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        # Step 8 — git push origin main
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        # Step 9 — complete_in_wave is called by the executor agent (Step 5a in the
        # manage prompt) after this script exits 0.  squash_merge.py only does git ops.

    finally:
        # Step 10 — cleanup workspace always (even on failure)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
