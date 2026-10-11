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
#   2. Re-run setup-dispatch-host.sh — Step 4.6 unconditionally de-registers the fixed
#      set of known server names before the add loop, so a server dropped from this
#      file disappears on the next run even though it is no longer in MCP_SERVERS.
#      OR run: sudo -u dispatch -H bash -lc "claude mcp remove <name> --scope user"
#
# Registration is per-host and per-user (--scope user writes to ~/.claude.json of
# the agent user — /home/dispatch/.claude.json in production).
#
# IMPORTANT — agent-gtd is LOAD-BEARING for Step 4 verification (START-HERE.md):
#   The verification step dispatches an item and checks whether the agent comments back
#   to that GTD item. If agent-gtd MCP fails to start (wrong source URL, missing
#   AGENT_GTD_URL, missing AGENT_GTD_API_KEY), dispatch appears to succeed but the agent
#   cannot comment — and the silence IS the failure signal. Fix agent-gtd first.
#
# personal-kb / team-kb are NOT defined here. They are hosted HTTP /mcp servers,
# registered (and their 0600 key files written) by scripts/kb-mcp-refresh.sh, which
# setup-dispatch-host.sh Step 4.6 and deploy.sh both run. The KB servers no longer
# need uvx or git-ssh.
#
# --- agent-gtd source ---
# Defaults to public GitHub so any host can install without homelab access.
# Override via AGENT_GTD_MCP_SRC (read from service .env by setup-dispatch-host.sh)
# for private/local mirrors: e.g. git+ssh://git@<host>/path/to/agent_gtd
_agent_gtd_mcp_src="${AGENT_GTD_MCP_SRC:-git+https://github.com/jason-weddington/agent-gtd}"

# --- agent-gtd env flags (AGENT_GTD_URL only) ---
# Read from the service .env by setup-dispatch-host.sh and exported before sourcing
# this file. Injected into the MCP server's subprocess env (NOT Claude Code's env).
#
# AGENT_GTD_API_KEY is deliberately NOT baked in here. A literal value in the MCP
# server's `env` block takes precedence over the subprocess environment, which would
# pin every agent to the host's static (admin@local) key and make per-run, per-user
# auth impossible. Instead the key is INHERITED from the agent subprocess env, where
# the dispatch worker sets it per-run to the run's callback_token — a 72h JWT scoped
# to the dispatching user — falling back to the static host AGENT_GTD_API_KEY when no
# token is present (admin dispatch, legacy senders, watchdog/recovery/plan paths).
# See engines.py build_env() and sudoers-dispatch-svc.tmpl env_keep (Phase 3, kb-03189).
_agent_gtd_flags=""
if [[ -n "${AGENT_GTD_URL:-}" ]]; then
    _agent_gtd_flags+="-e AGENT_GTD_URL=${AGENT_GTD_URL} "
fi

MCP_SERVERS=(
  # agent-gtd: LOAD-BEARING — must resolve for Step 4 verification to pass.
  # Source defaults to public GitHub; override via AGENT_GTD_MCP_SRC in service .env.
  "agent-gtd|--scope user -t stdio ${_agent_gtd_flags}-- uvx --python 3.13 --from ${_agent_gtd_mcp_src} agent-gtd-mcp"
  "aws-documentation-mcp-server|--scope user -t stdio -e FASTMCP_LOG_LEVEL=ERROR -e AWS_DOCUMENTATION_PARTITION=aws -- uvx awslabs.aws-documentation-mcp-server@latest"
)

# No Anthropic key is ever injected here. A literal ANTHROPIC_API_KEY in the agent's
# own process env flips Claude Code off Max/OAuth billing onto pay-per-token API
# billing (kb-01512).
