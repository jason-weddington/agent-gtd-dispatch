#!/usr/bin/env bash
set -euo pipefail

# update-remotes.sh — Rewrite git remote URLs from jason@ubuntu-vm01 to git@ubuntu-vm01:repos/.
# One-time migration script. Run on each client machine.
#
# Usage: bash update-remotes.sh [--apply] [--scan-dir ~/git]

SCAN_DIRS=("${HOME}" "${HOME}/git")
# Also scan oh-my-zsh custom plugins/themes if present
[[ -d "${HOME}/.oh-my-zsh/custom/.git" ]] && SCAN_DIRS+=("${HOME}/.oh-my-zsh")
APPLY=false
OLD_USER="jason"
OLD_HOSTS=("ubuntu-vm01" "192.168.1.56")
OLD_BASE="/home/jason/git"
NEW_USER="git"
NEW_HOST="ubuntu-vm01"

# --- Colors ---
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    CYAN='\033[0;36m'
    RED='\033[0;31m'
    RESET='\033[0m'
else
    GREEN='' YELLOW='' RED='' CYAN='' RESET=''
fi

info()    { printf "${GREEN}[OK]${RESET} %s\n" "$1"; }
warn()    { printf "${YELLOW}[SKIP]${RESET} %s\n" "$1"; }
changed() { printf "${CYAN}[CHANGE]${RESET} %s\n" "$1"; }
fail()    { printf "${RED}[FAIL]${RESET} %s\n" "$1"; }

usage() {
    cat <<EOF
Usage: bash update-remotes.sh [--apply] [--scan-dir <path>]

Rewrites git remote URLs from the old jason@ubuntu-vm01 pattern
to the new git@ubuntu-vm01:repos/ pattern.

Options:
  --apply           Actually rewrite URLs. Default is dry run.
  --scan-dir <path> Directory to scan for repos (default: ~/git)

Handles both URL formats:
  ssh://jason@ubuntu-vm01:/home/jason/git/<repo>  (ssh:// style)
  jason@ubuntu-vm01:/home/jason/git/<repo>         (SCP style)

Both become:
  git@ubuntu-vm01:repos/<repo>
EOF
    exit 0
}

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)    APPLY=true; shift ;;
        --scan-dir) SCAN_DIRS+=("$2"); shift 2 ;;
        -h|--help)  usage ;;
        *)          echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

for dir in "${SCAN_DIRS[@]}"; do
    [[ ! -d "$dir" ]] && { echo "Error: Scan directory not found: $dir" >&2; exit 1; }
done

# --- Helper: rewrite a URL if it matches the old pattern ---
# Returns the new URL on stdout, or nothing if no match.
rewrite_url() {
    local url="$1"

    for OLD_HOST in "${OLD_HOSTS[@]}"; do
        # Pattern A: ssh://jason@<host>[:/]/home/jason/git/<repo>
        # Variants: host:/path (colon+slash), host/path (slash only)
        if [[ "$url" =~ ^ssh://${OLD_USER}@${OLD_HOST}:?${OLD_BASE}/(.+)$ ]]; then
            echo "${NEW_USER}@${NEW_HOST}:repos/${BASH_REMATCH[1]}"
            return
        fi

        # Pattern B: jason@<host>:/home/jason/git/<repo>  (SCP style)
        if [[ "$url" =~ ^${OLD_USER}@${OLD_HOST}:${OLD_BASE}/(.+)$ ]]; then
            echo "${NEW_USER}@${NEW_HOST}:repos/${BASH_REMATCH[1]}"
            return
        fi
    done
}

# --- Discover repos ---
repos_scanned=0
remotes_changed=0
remotes_unchanged=0

echo "Scanning: ${SCAN_DIRS[*]}"
echo ""

for SCAN_DIR in "${SCAN_DIRS[@]}"; do
for git_dir in "$SCAN_DIR"/*/.git; do
    [[ ! -d "$git_dir" ]] && continue
    repo_root=$(dirname "$git_dir")
    repo_name=$(basename "$repo_root")
    repos_scanned=$((repos_scanned + 1))

    repo_had_changes=false

    while IFS= read -r remote_name; do
        [[ -z "$remote_name" ]] && continue

        # Check fetch URL
        fetch_url=$(git -C "$repo_root" remote get-url "$remote_name" 2>/dev/null || true)
        [[ -z "$fetch_url" ]] && continue

        new_fetch=$(rewrite_url "$fetch_url")
        if [[ -n "$new_fetch" ]]; then
            if ! $repo_had_changes; then
                printf "${CYAN}%s${RESET}\n" "$repo_name"
                repo_had_changes=true
            fi

            echo "  $remote_name: $fetch_url"
            echo "        -> $new_fetch"

            if $APPLY; then
                git -C "$repo_root" remote set-url "$remote_name" "$new_fetch"
                # Verify
                actual=$(git -C "$repo_root" remote get-url "$remote_name")
                if [[ "$actual" == "$new_fetch" ]]; then
                    info "  $remote_name updated"
                else
                    fail "  $remote_name verification failed (got: $actual)"
                fi
            fi
            remotes_changed=$((remotes_changed + 1))
        else
            remotes_unchanged=$((remotes_unchanged + 1))
        fi

        # Check push URL (only if explicitly set differently from fetch)
        push_url=$(git -C "$repo_root" config --get "remote.${remote_name}.pushurl" 2>/dev/null || true)
        if [[ -n "$push_url" && "$push_url" != "$fetch_url" ]]; then
            new_push=$(rewrite_url "$push_url")
            if [[ -n "$new_push" ]]; then
                echo "  $remote_name (push): $push_url"
                echo "              -> $new_push"

                if $APPLY; then
                    git -C "$repo_root" remote set-url --push "$remote_name" "$new_push"
                fi
                remotes_changed=$((remotes_changed + 1))
            fi
        fi
    done < <(git -C "$repo_root" remote 2>/dev/null)
done
done

# --- Summary ---
echo ""
if $APPLY; then
    echo "========================================"
    echo "  Remotes updated"
    echo "========================================"
else
    echo "========================================"
    echo "  Dry run — no changes made"
    echo "========================================"
fi
echo "  Repos scanned:      $repos_scanned"
echo "  Remotes to rewrite:  $remotes_changed"
echo "  Remotes unchanged:   $remotes_unchanged (other hosts, github, etc.)"

if ! $APPLY && [[ $remotes_changed -gt 0 ]]; then
    echo ""
    echo "Run with --apply to execute."
fi

echo ""
echo "Manual follow-ups:"
echo "  - Update ~/scripts/setup-mcp.sh git+ssh:// URIs"
echo "  - Update Agent GTD project git_origin values in the database"
echo ""
