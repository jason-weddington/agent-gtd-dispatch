#!/usr/bin/env bash
# Cut a release and publish it.
#
# During development: commit freely on main, push to origin, deploy liberally.
# When you hit a meaningful boundary — a coherent batch of work worth marking
# as a checkpoint — run this script. It will:
#   1. Bump the version via python-semantic-release (if there are feat/fix
#      commits since the last tag; no-op otherwise) — via build_command in
#      pyproject.toml this ALSO runs `uv build`, producing the dispatch wheel
#      in dist/.
#   2. Build the protocol wheel (uv build packages/protocol → repo-root dist/).
#   3. Push main + tags to BOTH origin and github.
#   4. Publish BOTH wheels to the homelab pypi index (pi-04 pypi.lab) so hosts
#      installing from the index see the new version.
#   5. Deploy to the running service (deploy.sh runs `uv tool install --force`
#      against the freshly-updated index).

set -euo pipefail

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "error: must be on main branch." >&2
  exit 1
fi

# 1. Bump version + build the dispatch wheel (semantic-release's build_command
#    runs `uv lock && git add uv.lock && uv build`, dropping the wheel in dist/).
uv run semantic-release version --no-push --no-vcs-release

# 2. Build the protocol wheel. Held on the versioning decision (OPS 16dee7dc);
#    kept here so the release flow publishes both wheels in one shot once
#    protocol versioning lands.
uv build packages/protocol

git push origin main --tags
git push github main --tags

# 3. Publish both wheels to the homelab index (pi-04 pypi.lab). Ships the newest
#    wheel out of each dist/ directory so re-running publishes the just-built
#    artifact.
DISPATCH_WHEEL="$(ls -t dist/agent_gtd_dispatch-*.whl 2>/dev/null | head -n1)"
PROTOCOL_WHEEL="$(ls -t dist/agent_gtd_dispatch_protocol-*.whl 2>/dev/null | head -n1)"

if [ -z "${DISPATCH_WHEEL:-}" ]; then
    echo "error: no dispatch wheel found in dist/ after uv build." >&2
    exit 1
fi
if [ -z "${PROTOCOL_WHEEL:-}" ]; then
    echo "error: no protocol wheel found in dist/ after uv build." >&2
    exit 1
fi

./scripts/publish-wheel.sh "$PROTOCOL_WHEEL"
./scripts/publish-wheel.sh "$DISPATCH_WHEEL"

# 4. Roll out to the running fleet from the updated index.
./deploy.sh
