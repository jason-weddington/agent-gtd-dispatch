# mcp-servers.sh — MCP servers to register for the dispatch agent user.
# Sourced by setup-dispatch-host.sh during host setup (Step 4.6).
#
# Format: "<name>|<args-after-claude-mcp-add-NAME>"
#   The name is the server identifier (used in `claude mcp add <name> ...`).
#   The args are everything that follows the name on the `claude mcp add` command line.
#
# To add a new server:
#   1. Append an entry to the MCP_SERVERS array below.
#   2. Re-run setup-dispatch-host.sh (or run the equivalent `claude mcp add` command
#      manually as the agent user with --scope user).
#
# To remove a server:
#   1. Delete its entry from MCP_SERVERS.
#   2. Re-run setup-dispatch-host.sh (the remove-then-add loop will leave it absent).
#      OR run: sudo -u dispatch -H bash -lc "claude mcp remove <name> --scope user"
#
# Registration is per-host and per-user (--scope user writes to ~/.claude.json of
# the agent user — /home/dispatch/.claude.json in production).
#
# personal-kb inherits KB_DATABASE_URL (pointing at the personal KB) from the agent
# subprocess environment, which the dispatch worker passes through (COMMON_ENV_KEYS in
# engines.py). team-kb is the same personal_kb package pointed at the *team* database,
# so it needs a different connection string — see the conditional block below.
#
# Both KB servers make their own Anthropic LLM calls (query planning, graph enrichment,
# summary answers), so each needs ANTHROPIC_API_KEY in its OWN MCP env block. It must
# NOT reach Claude Code's process environment — an ANTHROPIC_API_KEY there flips billing
# from the Max subscription (OAuth) to pay-per-token API (see engines.py / kb-01512).
# That is why it is injected per-server here from KB_ANTHROPIC_API_KEY (a name the
# dispatch worker's env passthrough does NOT forward), never named ANTHROPIC_API_KEY in
# the .env. setup-dispatch-host.sh reads KB_ANTHROPIC_API_KEY and exports it before
# sourcing this file; if unset, the key is simply omitted (KB LLM features degrade).
kb_anthropic_flag=""
if [[ -n "${KB_ANTHROPIC_API_KEY:-}" ]]; then
  kb_anthropic_flag="-e ANTHROPIC_API_KEY=${KB_ANTHROPIC_API_KEY}"
fi

MCP_SERVERS=(
  "agent-gtd|--scope user -t stdio -- uvx --python 3.13 --from git+ssh://git@ubuntu-vm01/home/git/repos/agent_gtd agent-gtd-mcp"
  "personal-kb|--scope user -t stdio ${kb_anthropic_flag} -- uvx --python 3.13 --from git+ssh://git@ubuntu-vm01/home/git/repos/personal_kb[postgres] personal-kb"
  "aws-documentation-mcp-server|--scope user -t stdio -e FASTMCP_LOG_LEVEL=ERROR -e AWS_DOCUMENTATION_PARTITION=aws -- uvx awslabs.aws-documentation-mcp-server@latest"
)

# team-kb's connection string is a secret, so it is NOT hardcoded here (gitleaks would
# block the commit, and committing DB passwords is wrong regardless). It is injected
# from TEAM_KB_DATABASE_URL, which setup-dispatch-host.sh reads out of the dispatch-svc
# .env and exports before sourcing this file. If the var is unset, team-kb is skipped
# rather than registered with an empty URL.
if [[ -n "${TEAM_KB_DATABASE_URL:-}" ]]; then
  MCP_SERVERS+=(
    "team-kb|--scope user -t stdio -e KB_DATABASE_URL=${TEAM_KB_DATABASE_URL} -e KB_INSTANCE_ROLE=team -e KB_CONTRIBUTOR=jason -e KB_TEAM=grit-mile ${kb_anthropic_flag} -- uvx --python 3.13 --from git+ssh://git@ubuntu-vm01/home/git/repos/personal_kb[postgres] personal-kb"
  )
fi
