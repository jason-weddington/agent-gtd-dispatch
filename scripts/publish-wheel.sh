#!/usr/bin/env bash
set -euo pipefail

# publish-wheel.sh — Publish a built wheel to a private PEP 503 index over ssh.
#
# Usage:
#   scripts/publish-wheel.sh <wheel-path>
#
# scp's the given wheel into ${PYPI_DIR}/packages/ on ${PYPI_HOST}, then
# runs the index regenerator (${PYPI_DIR}/regen.py) over ssh to rebuild
# the PEP-503 simple/ tree so `uv tool install --index <index-url>` sees it.
#
# Environment variables:
#   DISPATCH_PYPI_HOST   REQUIRED. SSH target for the index host (user@host)
#   DISPATCH_PYPI_DIR    Index root on the host        (default: /srv/pypi)
#
# Idempotent: re-publishing the same version overwrites the wheel in
# packages/ and regen.py rebuilds simple/.

if [ $# -ne 1 ]; then
    echo "usage: $0 <wheel-path>" >&2
    exit 2
fi

WHEEL="$1"
if [ ! -f "$WHEEL" ]; then
    echo "error: wheel not found: ${WHEEL}" >&2
    exit 1
fi

PYPI_HOST="${DISPATCH_PYPI_HOST:?DISPATCH_PYPI_HOST must be set to the SSH target of the index host}"
PYPI_DIR="${DISPATCH_PYPI_DIR:-/srv/pypi}"

echo "Publishing $(basename "$WHEEL") → ${PYPI_HOST}:${PYPI_DIR}/packages/"
scp "$WHEEL" "${PYPI_HOST}:${PYPI_DIR}/packages/"

echo "Regenerating simple/ index on ${PYPI_HOST}"
ssh "$PYPI_HOST" "python3 ${PYPI_DIR}/regen.py"

echo "Published $(basename "$WHEEL")"
