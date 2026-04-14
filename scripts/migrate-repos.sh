#!/usr/bin/env bash
set -euo pipefail

# migrate-repos.sh — Copy bare repos from /home/jason/git/ to /home/git/repos/.
# One-time migration script. Run on ubuntu-vm01.
#
# Usage: sudo bash migrate-repos.sh [--apply]

SOURCE_DIR="/home/jason/git"
DEST_DIR="/home/git/repos"

APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true

# --- Colors ---
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    RESET='\033[0m'
else
    GREEN='' YELLOW='' RED='' RESET=''
fi

info()  { printf "${GREEN}[OK]${RESET} %s\n" "$1"; }
warn()  { printf "${YELLOW}[SKIP]${RESET} %s\n" "$1"; }
fail()  { printf "${RED}[FAIL]${RESET} %s\n" "$1"; }

usage() {
    cat <<'EOF'
Usage: sudo bash migrate-repos.sh [--apply]

Copies bare git repos from /home/jason/git/ to /home/git/repos/.

Options:
  --apply   Actually copy the repos. Without this flag, performs a dry run.

Requires:
  - Run as root (sudo)
  - /home/git/repos/ must exist (run setup-git-server.sh first)
EOF
    exit 0
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage
[[ $EUID -ne 0 ]] && { echo "Error: Run as root (sudo)." >&2; exit 1; }
[[ ! -d "$SOURCE_DIR" ]] && { echo "Error: Source directory $SOURCE_DIR not found." >&2; exit 1; }
[[ ! -d "$DEST_DIR" ]] && { echo "Error: Destination $DEST_DIR not found. Run setup-git-server.sh first." >&2; exit 1; }

# --- Discover bare repos ---
repos=()
for candidate in "$SOURCE_DIR"/*/; do
    [[ ! -d "$candidate" ]] && continue
    if git -C "$candidate" rev-parse --is-bare-repository &>/dev/null; then
        is_bare=$(git -C "$candidate" rev-parse --is-bare-repository)
        [[ "$is_bare" == "true" ]] && repos+=("$candidate")
    fi
done

if [[ ${#repos[@]} -eq 0 ]]; then
    echo "No bare repos found in $SOURCE_DIR"
    exit 0
fi

echo "Found ${#repos[@]} bare repo(s) in $SOURCE_DIR"
echo ""

# --- Process each repo ---
migrated=0
skipped=0
failed=0

for repo in "${repos[@]}"; do
    name=$(basename "$repo")
    dest="$DEST_DIR/$name"
    size=$(du -sh "$repo" 2>/dev/null | cut -f1)

    if [[ -d "$dest" ]]; then
        warn "$name (already exists at $dest)"
        skipped=$((skipped + 1))
        continue
    fi

    if $APPLY; then
        # Copy preserving internals
        if cp -a "$repo" "$dest" 2>/dev/null; then
            chown -R git:git "$dest"

            # Validate the copy (run as git user to avoid safe.directory errors)
            if sudo -u git git -C "$dest" rev-parse --git-dir &>/dev/null; then
                info "$name ($size)"
                migrated=$((migrated + 1))
            else
                fail "$name (copy appears corrupt)"
                failed=$((failed + 1))
            fi
        else
            fail "$name (cp failed)"
            failed=$((failed + 1))
        fi
    else
        printf "  %-30s %6s  ->  %s\n" "$name" "$size" "$dest"
    fi
done

# --- Summary ---
echo ""
if $APPLY; then
    echo "========================================"
    echo "  Migration complete"
    echo "========================================"
    echo "  Migrated:  $migrated"
    echo "  Skipped:   $skipped"
    echo "  Failed:    $failed"
    echo ""
    if [[ $migrated -gt 0 ]]; then
        echo "Originals preserved at $SOURCE_DIR."
        echo "Remove manually when satisfied: sudo rm -rf $SOURCE_DIR/*"
    fi
else
    echo "========================================"
    echo "  Dry run — no changes made"
    echo "========================================"
    echo "  Would migrate: $((${#repos[@]} - skipped))"
    echo "  Would skip:    $skipped"
    echo ""
    echo "Run with --apply to execute."
fi
echo ""
