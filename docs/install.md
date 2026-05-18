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
# Should contain exactly:
# dispatch-svc ALL=(dispatch) NOPASSWD: /usr/bin/git, /home/dispatch/.local/bin/uv, /usr/bin/claude, /usr/bin/python3, /bin/bash
```

If missing or wrong, re-run:
```bash
sudo ./setup-dispatch-host.sh
```

The installer will detect the mismatch and reinstall the correct fragment.

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
