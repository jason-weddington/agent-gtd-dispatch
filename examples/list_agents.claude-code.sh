#!/usr/bin/env bash
# Copy to ~/.config/agent-dispatch/list_agents.sh and make executable to enable
# agent discovery for claude-code deployments.
#
# Scans ~/.claude/agents/*.md and <cwd>/.claude/agents/*.md, extracts the
# `name` and `description` fields from YAML frontmatter, and emits one line
# per agent in the format expected by the dispatch service:
#
#   <name>\t<description>
#
# Requirements: bash, awk (no jq dependency).

set -euo pipefail

# Scan both the user-global and per-project agent directories.
for dir in "$HOME/.claude/agents" "$(pwd)/.claude/agents"; do
    [[ -d "$dir" ]] || continue

    for md in "$dir"/*.md; do
        [[ -f "$md" ]] || continue  # handles the no-match case (un-expanded glob)

        # Extract name and description from YAML frontmatter.
        # Frontmatter is the block between the first two "---" lines.
        awk '
            /^---$/ {
                if (in_front) { exit }   # second --- ends frontmatter
                in_front = 1
                next
            }
            in_front && /^name:/ {
                sub(/^name:[[:space:]]*/, "")
                gsub(/^["\x27]|["\x27]$/, "")  # strip surrounding quotes
                name = $0
            }
            in_front && /^description:/ {
                sub(/^description:[[:space:]]*/, "")
                gsub(/^["\x27]|["\x27]$/, "")  # strip surrounding quotes
                desc = $0
            }
            END {
                if (name != "") {
                    if (desc != "")
                        printf "%s\t%s\n", name, desc
                    else
                        print name
                }
            }
        ' name="" desc="" "$md"
    done
done
