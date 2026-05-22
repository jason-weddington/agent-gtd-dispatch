# Installing the Dispatch Service

This guide covers bootstrapping a fresh Ubuntu host and migrating an existing single-user installation to the two-user-split architecture.

## Prerequisites

| Requirement | Notes |
|---|---|
| Ubuntu 22.04+ | Tested on 22.04 LTS (jammy) and 24.04 LTS (noble) |
| SSH access | As a user with passwordless sudo, or root |
| `sudo` privileges | Required to create system users, write systemd units, and install sudoers fragments |
| `git` | `sudo apt install git` |
| `openssh-client` | Usually pre-installed; `sudo apt install openssh-client` |
| `curl` | `sudo apt install curl` |
| `uv` | Installed automatically by the script if absent |

> **Note**: `uv` is the only non-system dependency the script installs automatically.
> All other tooling (`python3`, `visudo`, `systemctl`) ships with standard Ubuntu.

---

## Quick start — fresh host

```bash
# 1. Clone the repo
git clone git@ubuntu-vm01:repos/agent-gtd-dispatch
cd agent-gtd-dispatch

# 2. Prepare an env file (copy the template and fill in real values)
cp templates/dispatch-env.tmpl /tmp/dispatch.env
$EDITOR /tmp/dispatch.env    # set DISPATCH_API_KEY, AGENT_GTD_*, ANTHROPIC_API_KEY

# 3. Run the installer
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env

# 4. (Optional) run smoke test
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env --smoke
```

The installer is idempotent — re-running it on a configured host prints
`[SKIP] already configured` for every completed step and exits 0.

### Preview mode (dry run)

```bash
sudo ./setup-dispatch-host.sh --dry-run
```

Prints every action the script would take without touching anything.
Useful for auditing a migration before applying it.

---

## Fresh box install

On a truly fresh host (no `dispatch` user, no Claude, no git credentials), the
installer may halt after **Phase 1** with an "ACTION REQUIRED" message asking you
to add an SSH public key to the git server. This is expected — it is a two-phase
flow:

### Phase 1 — generate credentials, halt

Run the installer once. It will:

1. Create the `dispatch` and `dispatch-svc` system users.
2. Create the agent workspace (`/home/dispatch/workspace`) with group-writable permissions (mode 2775).
3. Generate a fresh `ed25519` SSH keypair for the `dispatch` user.
4. **Print the public key and exit** with instructions like:

```
========================================
  ACTION REQUIRED: Add SSH public key
========================================

  A new ed25519 keypair was generated for the 'dispatch' agent user.
  Authorize it on the git server before re-running this installer:

  Public key to add to ubuntu-vm01:~/repos/.ssh/authorized_keys :

  ssh-ed25519 AAAA... dispatch@r7-research

  Then re-run this installer with the same arguments:
    sudo ./setup-dispatch-host.sh [your original options]
```

Add the printed public key to the git server:

```bash
# On ubuntu-vm01:
echo "ssh-ed25519 AAAA... dispatch@r7-research" >> ~/repos/.ssh/authorized_keys
```

### Phase 2 — complete install

Re-run the installer with the same arguments:

```bash
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env --smoke
```

The SSH key now exists, so the installer skips key generation, copies it to
`dispatch-svc`'s `.ssh/`, clones the repos, installs Claude Code for the
`dispatch` user, sets up the systemd unit, and completes normally.

---

## Migration — pironman01 (two-user split)

pironman01 previously ran the service as the `dispatch` user. The two-user
split (`998544ac`) introduces `dispatch-svc` as the service account and
demotes `dispatch` to an unprivileged agent subprocess user.

```bash
# 1. Build a new env file from the existing one
sudo cat /home/dispatch/.env > /tmp/dispatch.env
# Add the agent subprocess user variable:
echo "DISPATCH_AGENT_SUBPROCESS_USER=dispatch" >> /tmp/dispatch.env

# 2. Run the installer (uses defaults: --agent-user dispatch --service-user dispatch-svc)
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env

# 3. Verify
sudo systemctl status dispatch-api
curl -sf http://localhost:8100/health | python3 -m json.tool
```

The old service unit (`dispatch` user) will be replaced by a new unit
(`dispatch-svc` user). The `dispatch` user remains but is no longer the
service account.

---

## Environment file reference

All variables documented in `templates/dispatch-env.tmpl`. Key variables:

| Variable | Required | Description |
|---|---|---|
| `DISPATCH_API_KEY` | ✓ | Bearer token callers must supply to the REST API |
| `AGENT_GTD_URL` | ✓ | Agent GTD API base URL (e.g. `https://r7-research:8443`) |
| `AGENT_GTD_API_KEY` | ✓ | Agent GTD API key (`agtd_…` prefix) |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API key for Claude Code subprocesses |
| `DISPATCH_AGENT_SUBPROCESS_USER` | ✓ (prod) | Agent user for user-switching (`dispatch`). Leave empty in dev to disable. |
| `DISPATCH_WORKSPACE_ROOT` | – | Override workspace root (default: `~/workspace` relative to agent user) |
| `DISPATCH_MAX_TURNS` | – | Claude Code turn cap (default: 100) |
| `DISPATCH_TIMEOUT_SECONDS` | – | Agent subprocess wall-clock timeout in seconds (default: 1800) |
| `OLLAMA_BASE_URL` | – | Root URL of an Ollama instance for `claude-code-ollama` engine dispatches |
| `OLLAMA_DEFAULT_MODEL` | – | Default Ollama model (default: `qwen3:35b`) |

The env file is installed at `/home/dispatch-svc/.env` with mode `0600`,
owned by `dispatch-svc`. Never commit it to git.

---

## MCP servers for the agent user

Step 4.6 of the installer registers three MCP servers for the `dispatch` (agent) user.
This gives dispatched Claude Code agents tool access to GTD, the personal knowledge
base, and AWS documentation — enabling proper attribution on GTD comments instead of
falling back to raw `curl` calls.

| Server | Purpose |
|---|---|
| `agent-gtd` | GTD items, comments, and dispatch (prevents `created_by="human"` regression) |
| `personal-kb` | Knowledge base lookups (decisions, lessons learned, project conventions) |
| `aws-documentation-mcp-server` | AWS docs for any AWS-related implementation work |

Registration is **per-host and per-user** using `--scope user`, which writes to
`/home/dispatch/.claude.json`.

### Config file

`templates/mcp-servers.sh` in the repo root defines the `MCP_SERVERS` array. Each
entry has the format:

```
"<name>|<args-after-claude-mcp-add-NAME>"
```

The installer sources this file during Step 4.6 and runs `claude mcp add` for each
entry with an idempotent remove-first pattern (safe to re-run).

### Adding a new MCP server

1. Append an entry to `MCP_SERVERS` in `templates/mcp-servers.sh`.
2. Re-run `sudo ./setup-dispatch-host.sh` on each host — Step 4.6 registers the new
   server and leaves existing registrations unchanged.

Or register it manually on a specific host only:
```bash
sudo -u dispatch -H bash -lc "claude mcp add <name> --scope user <args>"
```

### Verifying registration

```bash
# List registered servers on a host:
ssh <HOST> 'sudo -u dispatch -H bash -lc "cd /home/dispatch && claude mcp list"'
# → agent-gtd: ...
# → aws-documentation-mcp-server: ...
# → personal-kb: ...

# Inspect ~/.claude.json directly:
ssh <HOST> 'sudo cat /home/dispatch/.claude.json' | jq '.mcpServers | keys'
# → ["agent-gtd", "aws-documentation-mcp-server", "personal-kb"]
```

---

## Rollback procedure

To undo the installer step by step (in reverse order):

### Step 8 — Smoke test
No filesystem state created. Nothing to undo.

### Step 7 — Health check
No filesystem state created. Nothing to undo.

### Step 6 — Systemd unit
```bash
sudo systemctl stop dispatch-api
sudo systemctl disable dispatch-api
sudo rm /etc/systemd/system/dispatch-api.service
sudo systemctl daemon-reload
```

### Step 5 — Sudoers fragment
```bash
sudo rm /etc/sudoers.d/dispatch-svc
```

### Step 4 — Dependencies (uv)
```bash
sudo -u dispatch-svc rm -rf /home/dispatch-svc/.local
sudo -u dispatch rm -rf /home/dispatch/.local
```

### Step 3 — Env file
```bash
sudo rm /home/dispatch-svc/.env
```

### Step 2 — Repos
```bash
sudo rm -rf /home/dispatch-svc/agent-gtd-dispatch
sudo rm -rf /home/dispatch-svc/agent_gtd
```

### Step 1 — Users
```bash
sudo deluser --remove-home dispatch-svc
# Only remove 'dispatch' if it was created by this installer and you want a full teardown:
# sudo deluser --remove-home dispatch
```

> **Tip**: Use `sudo ./setup-dispatch-host.sh --dry-run` before rollback to
> confirm what state the installer created.

---

## Troubleshooting

### The service fails to start

**Symptom**: `systemctl status dispatch-api` shows `failed` or `activating`.

**Check the journal**:
```bash
sudo journalctl -u dispatch-api -n 100 --no-pager
```

**Common causes**:
- Missing or incomplete `.env` file — ensure all required variables are set.
  `sudo cat /home/dispatch-svc/.env | grep -v '^#' | grep '^\(DISPATCH_API_KEY\|AGENT_GTD_URL\|AGENT_GTD_API_KEY\|ANTHROPIC_API_KEY\)='`
- `uv` not found at `/home/dispatch-svc/.local/bin/uv` — re-run the installer
  or install manually: `sudo -u dispatch-svc curl -fsSL https://astral.sh/uv/install.sh | sudo -u dispatch-svc sh`
- Working directory missing — ensure `/home/dispatch-svc/agent-gtd-dispatch` exists and is a valid git repo.

---

### `visudo` validation fails during sudoers install

**Symptom**: Script exits with `visudo validation failed — sudoers fragment NOT installed`.

**Cause**: The sudoers template was rendered with an unexpected character (e.g. special characters in usernames).

**Fix**: Verify that `--agent-user` and `--service-user` contain only `[a-z0-9_-]` characters.
Inspect the rendered fragment: `sudo cat /tmp/dispatch-sudoers.*` (before the temp file is cleaned up).

---

### Agent subprocesses run as the wrong user

**Symptom**: Agent processes appear in `ps aux` under `dispatch-svc` rather than `dispatch`.

**Cause**: `DISPATCH_AGENT_SUBPROCESS_USER` is empty or missing from the env file.

**Fix**:
```bash
echo "DISPATCH_AGENT_SUBPROCESS_USER=dispatch" | sudo tee -a /home/dispatch-svc/.env
sudo systemctl restart dispatch-api
```

Also verify the sudoers fragment allows the `dispatch-svc → dispatch` transition:
```bash
sudo visudo -c -f /etc/sudoers.d/dispatch-svc
sudo cat /etc/sudoers.d/dispatch-svc
```

---

### `sudo -u dispatch` permission denied

**Symptom**: Dispatch run logs show `sudo: dispatch: command not found` or permission errors.

**Cause**: Sudoers fragment not installed, or installed with wrong content.

**Fix**:
```bash
sudo cat /etc/sudoers.d/dispatch-svc
# Should contain (among other lines):
# dispatch-svc ALL=(dispatch) NOPASSWD: /usr/bin/git, /home/dispatch/.local/bin/uv, /usr/local/bin/claude, /usr/bin/rm, /usr/bin/python3, /bin/bash
```

If missing or wrong, re-run:
```bash
sudo ./setup-dispatch-host.sh
```

The installer will detect the mismatch and reinstall the correct fragment.

---

### SSH host key verification failed during git clone

**Symptom**: Step 2 (Repos) fails with `Host key verification failed` or `The authenticity of host 'ubuntu-vm01' can't be established`.

**Cause**: The `dispatch-svc` user has an empty `~/.ssh/known_hosts` — the new service account has not connected to the git server before.

**Fix**: Re-run the installer (it seeds `known_hosts` in step 1) or manually:
```bash
sudo -u dispatch-svc ssh-keyscan ubuntu-vm01 >> /home/dispatch-svc/.ssh/known_hosts
sudo -u dispatch-svc git clone git@ubuntu-vm01:repos/agent-gtd-dispatch /home/dispatch-svc/agent-gtd-dispatch
```

---

### sudo effective-uid / privilege-escalation failures (NoNewPrivileges)

**Symptom**: Service fails immediately or agent subprocesses fail to spawn; journal shows `sudo: effective uid is not 0`.

**Cause**: A previous service unit included `NoNewPrivileges=true`, which blocks `sudo` from raising privileges. This directive is incompatible with the sudo-based user-switching pattern used by the dispatch service.

**Fix**: Ensure the systemd unit does **not** contain `NoNewPrivileges`, `ProtectSystem=strict`, or `ProtectHome=read-only`:
```bash
sudo grep -E 'NoNewPrivileges|ProtectSystem|ProtectHome|PrivateTmp' /etc/systemd/system/dispatch-api.service
# Should return empty — if it returns lines, re-run the installer to update the unit
sudo ./setup-dispatch-host.sh
sudo systemctl daemon-reload && sudo systemctl restart dispatch-api
```

---

### `sudo: /usr/bin/rm: command not allowed`

**Symptom**: Run logs show `sudo: /usr/bin/rm: command not allowed` after the agent subprocess completes. Workspace directories accumulate and are never cleaned up. The error may cascade and appear as a misleading "git clone failed" in subsequent runs.

**Cause**: The sudoers fragment did not include `/usr/bin/rm` in the NOPASSWD allowlist. The dispatch service calls `rm -rf` (via sudo) to clean up agent workspaces after each run.

**Fix**: Re-run the installer to update the sudoers fragment:
```bash
sudo ./setup-dispatch-host.sh
sudo cat /etc/sudoers.d/dispatch-svc  # verify /usr/bin/rm is listed
```

---

### `sudo: /usr/bin/claude: command not allowed` (secure_path mismatch)

**Symptom**: Agent subprocesses fail immediately with `sudo: /usr/bin/claude: command not allowed` or `No such file or directory`.

**Cause**: Claude installs to `/home/dispatch/.local/bin/claude`, but `sudo`'s `secure_path` does not include `/home/dispatch/.local/bin/`. The sudoers NOPASSWD entry must reference a path that is both on `secure_path` and exists as a binary. The installer creates a symlink at `/usr/local/bin/claude` pointing to the agent user's claude binary, and the sudoers fragment references `/usr/local/bin/claude`.

**Fix**: Ensure the symlink exists and sudoers references the right path:
```bash
ls -la /usr/local/bin/claude          # should be a symlink to /home/dispatch/.local/bin/claude
sudo grep claude /etc/sudoers.d/dispatch-svc  # should show /usr/local/bin/claude
# If symlink is missing:
sudo ln -sf /home/dispatch/.local/bin/claude /usr/local/bin/claude
# Then re-run installer to update sudoers if needed:
sudo ./setup-dispatch-host.sh
```

---

### `Not logged in · Please run /login` (env vars stripped by sudo)

**Symptom**: Claude subprocesses immediately exit with `Not logged in · Please run /login` or `ANTHROPIC_API_KEY not set`, even though the service's `.env` file contains the correct values.

**Cause**: `sudo` strips environment variables by default, including `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY`. The dispatch service loads these from its `.env` via systemd `EnvironmentFile=`, but they do not survive the `sudo -u dispatch` call unless explicitly preserved.

**Fix**: The sudoers fragment must include a `Defaults env_keep` line. Re-run the installer:
```bash
sudo ./setup-dispatch-host.sh
sudo grep env_keep /etc/sudoers.d/dispatch-svc
# Should show all 8 required variables preserved across the sudo boundary
```

---

### Health check fails after install

**Symptom**: Step 7 reports repeated failures and the installer exits non-zero.

**Cause**: Service started but is failing to bind / crashed immediately.

**Fix**:
```bash
sudo journalctl -u dispatch-api -n 50 --no-pager
# Check for: port already in use, missing env vars, Python import errors
sudo ss -tlnp | grep 8100   # confirm port is free (or in use by another process)
```

---

### Claude binary missing after Step 4.5

**Symptom**: Step 4.5 reports `Claude Code installer ran but /home/dispatch/.local/bin/claude not found`, or Step 5a warns `Agent claude binary not found`.

**Cause**: The official Claude Code installer (`claude.ai/install.sh`) failed silently, or installed to an unexpected location.

**Fix**: Install Claude Code manually as the `dispatch` user, then re-run the installer:
```bash
sudo -u dispatch bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
ls -la /home/dispatch/.local/bin/claude   # verify binary exists
sudo ./setup-dispatch-host.sh             # re-run to create symlink + sudoers
```

If Claude Code requires a different install path, set `ANTHROPIC_CLAUDE_PATH` or check the
output of `sudo -u dispatch bash -c 'which claude 2>/dev/null || echo not found'`.

---

### Installer halts at "ACTION REQUIRED: Add SSH public key"

**Symptom**: Step 1 prints a yellow banner and exits with `[ERROR] SSH public key not yet authorized`.

**Cause**: This is a normal Phase 1 halt on a fresh host. The `dispatch` agent user had no
SSH keypair, so the installer generated one and is waiting for you to authorize it.

**Fix**: This is expected — follow the [Fresh box install](#fresh-box-install) two-phase flow above:
1. Copy the printed public key.
2. Append it to `ubuntu-vm01:~/repos/.ssh/authorized_keys`.
3. Re-run the installer with the same arguments.

If you want to use an existing keypair instead of the generated one, place it at
`/home/dispatch/.ssh/id_ed25519` (and `.pub`) before running the installer.

---

### SQLite workspace permission error (dispatch-svc cannot open dispatch.db)

**Symptom**: Dispatch API starts but every run fails immediately with a SQLite error such as
`unable to open database file` or `disk I/O error`. `journalctl` shows permission denied on
`/home/dispatch/workspace/dispatch.db`.

**Cause**: On a fresh box, `/home/dispatch` is created with mode `0700` (home directory
default), so `dispatch-svc` cannot traverse the path to reach `dispatch.db` even though
it is a member of the `dispatch` group.

**Fix**: The installer now sets mode `2775` on `/home/dispatch` and `/home/dispatch/workspace`
during Step 1. If you are on an older install, fix it manually:
```bash
sudo chmod 2775 /home/dispatch /home/dispatch/workspace
# Verify dispatch-svc is in the dispatch group:
getent group dispatch | grep dispatch-svc || sudo usermod -aG dispatch dispatch-svc
# Restart the service:
sudo systemctl restart dispatch-api
```

If `dispatch.db` itself has wrong permissions:
```bash
sudo chmod g+rw /home/dispatch/workspace/dispatch.db
sudo chown dispatch:dispatch /home/dispatch/workspace/dispatch.db
```

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────┐
│ pironman01                                              │
│                                                         │
│  dispatch-svc (service account)                         │
│    /home/dispatch-svc/agent-gtd-dispatch/   ← working   │
│    /home/dispatch-svc/.env                 ← secrets    │
│    systemd: dispatch-api.service           ← FastAPI    │
│                                                         │
│  dispatch (agent subprocess user)                       │
│    /home/dispatch/workspace/{run_id}/      ← agent work │
│                                                         │
│  /etc/sudoers.d/dispatch-svc               ← allowlist  │
│    dispatch-svc → dispatch NOPASSWD git/uv/claude/...   │
└─────────────────────────────────────────────────────────┘
```

The `dispatch-svc` user runs the FastAPI process. When a dispatch request
arrives, the service calls `sudo -u dispatch -H <agent-cli>` to spawn the
agent subprocess. The sudoers fragment limits which commands `dispatch-svc`
may run as `dispatch` — no `ALL=(ALL)` escalation.

See the `## Process model` section of `README.md` for the full explanation.

---

## Security model

### POSIX user isolation is the active security boundary

The dispatch host relies on **POSIX user isolation** as its primary security boundary:

- `dispatch-svc` runs the FastAPI service and owns all service credentials (`.env`, repo).
- `dispatch` runs agent subprocesses and owns workspace directories.
- The sudoers fragment grants `dispatch-svc` a narrow, enumerated set of commands it may run as `dispatch` — no `ALL=(ALL)` escalation.

This means `dispatch-svc` cannot read agent files, and agents cannot write to service files. Linux DAC (discretionary access control) enforces this separation.

### Why `NoNewPrivileges` and `ProtectSystem=strict` were removed

The original `dispatch-api.service` unit included systemd security hardening directives. These were **removed** because they conflict with the sudo-based user-switching pattern:

| Directive | Why it was removed |
|---|---|
| `NoNewPrivileges=true` | Blocks `sudo` from raising effective UID, preventing any `sudo -u dispatch` call from succeeding. This is the primary failure mode. |
| `ProtectSystem=strict` | Makes `/run/sudo/ts/` read-only, so sudo cannot write timestamp files (ticket-based auth fails). |
| `ProtectHome=read-only` | Blocks read access to `/home/dispatch/.ssh/`, which is needed for git clone via SSH. |
| `PrivateTmp=true` | Gives a private `/tmp`; less critical but can interfere with sudo's lock files. |

### Accepted trade-off

Removing these directives reduces systemd-level sandboxing. The security trade-off is accepted because:

1. **POSIX isolation is sufficient** — the `dispatch-svc` account has no sudo access beyond the explicit allowlist. An attacker who compromises `dispatch-svc` cannot escalate beyond what the sudoers fragment permits.
2. **The directives were redundant defense-in-depth** — they did not provide isolation that POSIX permissions didn't already provide.
3. **Re-enabling them would require replacing sudo with a different user-switching mechanism** (e.g., setuid wrapper, PAM), which is out of scope.

If you want to re-enable systemd hardening in a future iteration, the correct approach is to replace the `sudo -u dispatch` calls in `dispatch.py` with a setuid helper binary that does not require `NoNewPrivileges` to be unset.
