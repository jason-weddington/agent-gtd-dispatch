r"""CLI helper: print the agent transcript for a dispatch run.

Usage:
    python -m agent_gtd_dispatch.show_run_transcript <run_id>

The transcript lives at ``{WORKSPACE_ROOT}/*-{run_id}/transcript.txt`` and is
only available while the workspace has not been cleaned up yet (i.e. the run
is still active, or cleanup_workspace() has not been called).

Example (SSH to dispatch host)::

    ssh dispatch@pironman01 \\
        'python -m agent_gtd_dispatch.show_run_transcript abc123def456'

Or for a manage-mode run whose workspace is ``wave-manager-{run_id}``::

    ssh dispatch@pironman01 \\
        'cat /home/dispatch/workspace/wave-manager-abc123/transcript.txt'
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def main() -> None:
    """Entry point for ``python -m agent_gtd_dispatch.show_run_transcript``."""
    if len(sys.argv) != 2:
        print(
            "Usage: python -m agent_gtd_dispatch.show_run_transcript <run_id>",
            file=sys.stderr,
        )
        sys.exit(1)

    run_id = sys.argv[1]

    # Import config lazily so module-level import doesn't trigger load()
    from agent_gtd_dispatch import config

    config.load()

    workspace_root: Path = config.WORKSPACE_ROOT
    matches = list(workspace_root.glob(f"*-{run_id}/transcript.txt"))
    if not matches:
        print(
            f"No transcript found for run {run_id!r} under {workspace_root}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(matches[0].read_text(), end="")


if __name__ == "__main__":
    main()
