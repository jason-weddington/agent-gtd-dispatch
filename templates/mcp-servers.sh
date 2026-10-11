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
#   to that GTD item. If the agent-gtd MCP server is unreachable (missing or wrong
#   AGENT_GTD_URL, a TLS certificate Node does not trust, a bad bearer), dispatch
#   appears to succeed but the agent cannot comment — and the silence IS the failure
#   signal. Fix agent-gtd first. It is registered by scripts/kb-mcp-refresh.sh, and
#   setup-dispatch-host.sh Step 4.6 dies if that registration is not in place.
#
# agent-gtd, personal-kb and team-kb are NOT defined here. Like personal-kb and team-kb,
# agent-gtd is a hosted HTTP /mcp server (<AGENT_GTD_URL>/mcp), registered by
# scripts/kb-mcp-refresh.sh, which setup-dispatch-host.sh Step 4.6 and deploy.sh both
# run (deploy.sh also probes agent-gtd). None of them needs uvx or git-ssh any more.
#
# No AGENT_GTD_API_KEY and no agent-gtd key file is baked into the registration. Its
# headersHelper reads AGENT_GTD_API_KEY from the agent's env at connect time, where
# the dispatch worker sets it per-run to the run's callback_token — a 72h JWT scoped
# to the dispatching user — falling back to the static host AGENT_GTD_API_KEY when no
# token is present (admin dispatch, legacy senders, watchdog/recovery/plan paths). A
# static key in the registration pins every run to one (admin@local) identity
# and makes per-run, per-user auth impossible. See engines.py build_env() and
# sudoers-dispatch-svc.tmpl env_keep (Phase 3, kb-03189).

MCP_SERVERS=(
  "aws-documentation-mcp-server|--scope user -t stdio -e FASTMCP_LOG_LEVEL=ERROR -e AWS_DOCUMENTATION_PARTITION=aws -- uvx awslabs.aws-documentation-mcp-server@latest"
)

# No Anthropic key is ever injected here. A literal ANTHROPIC_API_KEY in the agent's
# own process env flips Claude Code off Max/OAuth billing onto pay-per-token API
# billing (kb-01512).
