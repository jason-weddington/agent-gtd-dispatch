#!/usr/bin/env bash
# Non-mutating replacement for pre-commit-hooks' trailing-whitespace and
# end-of-file-fixer. Reports violations; never rewrites a file.
#
# Those two upstream hooks have no check-only mode, and their default
# behaviour of rewriting files on the first hook run is exactly the
# problem: a talos worker commits once, with no retry loop, so a
# mutating hook silently destroys the completed unit of work. Claude
# Code's retry loop is fine with a hook that only fails; talos is not
# fine with one that fixes files out from under it. See kb-03099.
set -euo pipefail

status=0

for f in "$@"; do
    [ -f "$f" ] || continue

    if grep -nE '[[:blank:]]+$' "$f" >/dev/null 2>&1; then
        echo "$f: trailing whitespace"
        status=1
    fi

    if [ -s "$f" ] && [ -n "$(tail -c1 -- "$f")" ]; then
        echo "$f: missing newline at end of file"
        status=1
    fi
done

exit "$status"
