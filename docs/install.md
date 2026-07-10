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

> **Note**: The script auto-installs `uv`, Claude Code for the agent user (Step 4.5,
> via the official `claude.ai/install.sh` installer), and `pre-commit` (Step 4.7, via
> `uv tool install`). All other tooling (`python3`, `visudo`, `systemctl`) ships with
> standard Ubuntu.
>
> **Single-user mode** (`DISPATCH_SINGLE_USER=1`) does **not** require creating extra
> system users or installing a sudoers fragment — it runs the service and agent under
> your own login account.

---

## Quick start — fresh host

> **Installing on a personal/dev machine** where everything should run under your own
> account? Use single-user mode (`DISPATCH_SINGLE_USER=1`) — read
> [Single-user mode](#single-user-mode) **before** running step 3. Running the default
> two-user installer first creates system users and a sudoers fragment that trip the
> mode-mismatch guard on every later single-user attempt until you do a full rollback.

```bash
# 1. Clone the repo (public GitHub; substitute your fork if you have one)
git clone https://github.com/jason-weddington/agent-gtd-dispatch
cd agent-gtd-dispatch

# 2. Prepare an env file (copy the template and fill in real values)
cp templates/dispatch-env.tmpl /tmp/dispatch.env
$EDITOR /tmp/dispatch.env    # set AGENT_GTD_*, ANTHROPIC_API_KEY  (DISPATCH_API_KEY is auto-minted by Step 3.5 if you leave it empty)

# 3. Run the installer
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env

# 4. (Optional) run smoke test
sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env --smoke
```

The installer is idempotent — re-running it on a configured host prints
`[SKIP] already configured` for every completed step and exits 0.

### Adapting to your own git host

Step 2's clones **default to public GitHub** (anonymous https) — no credentials or
overrides needed to install the maintainer's published code. The installer reads two
environment-variable overrides (also listed in `--help`) to point the clones at a fork
or a self-hosted origin instead:

| Variable | Default | Purpose |
|---|---|---|
| `DISPATCH_REPO_URL` | `https://github.com/jason-weddington/agent-gtd-dispatch` | Remote for the dispatch service repo |
| `AGENT_GTD_REPO_URL` | `https://github.com/jason-weddington/agent-gtd` | Remote for the agent_gtd repo |

Pass them on the installer command line:

```bash
sudo DISPATCH_REPO_URL=git@your-git-host:you/agent-gtd-dispatch \
     AGENT_GTD_REPO_URL=git@your-git-host:you/agent_gtd \
     ./setup-dispatch-host.sh --env-file /tmp/dispatch.env
```

> ⚠️ **GitHub is release-cadence.** The public repos are pushed at release boundaries,
> so a host that must run **tip-of-main** (e.g. a maintainer's own infra) should override
> both variables to point at the origin that carries main, not rely on the GitHub default.

> **known_hosts is seeded automatically for your configured git host(s).** The
> installer derives the host from `DISPATCH_REPO_URL` / `AGENT_GTD_REPO_URL` (the
> defaults above) and runs `ssh-keyscan` against each, so overriding the remotes is
> sufficient — no manual `known_hosts` step. (Note: `ssh-keyscan` only handles SSH
> remotes on the default port 22; for a non-standard SSH port, seed `known_hosts`
> yourself with `ssh-keyscan -p <port> <host>`.)

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
  Put the public key wherever you host your repos —
  e.g. authorized_keys on a local git server, or GitHub Settings → SSH keys.

  Public key:

  ssh-ed25519 AAAA... dispatch@<hostname>

  Then re-run this installer with the same arguments:
    sudo ./setup-dispatch-host.sh [your original options]
```

(The key comment is `dispatch@$(hostname -s)` — your host's short name.)

Authorize the printed public key on your git host. On a self-hosted git server
that means appending it to the repo user's `authorized_keys`:

```bash
# Self-hosted git server example — on <your-git-host>:
echo "ssh-ed25519 AAAA... dispatch@<hostname>" >> ~/repos/.ssh/authorized_keys
```

On GitHub or another forge, add it as a deploy key / account SSH key instead.

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

## Single-user mode

On personal machines where a POSIX two-user split is unavailable or unwanted — such as a
developer workstation where everything must run under your own login account — install in
**single-user mode** by setting `DISPATCH_SINGLE_USER=1`:

```bash
# Canonical form (matches README.md and setup.md): name the var explicitly so
# sudo's env-stripping doesn't drop it
sudo --preserve-env=DISPATCH_SINGLE_USER DISPATCH_SINGLE_USER=1 ./setup-dispatch-host.sh --env-file /tmp/dispatch.env
```

> **Note**: Single-user mode must be invoked **via `sudo` from your non-root login
> account**: the installer resolves the target user from `SUDO_USER` and dies with
> `requires invocation via sudo from a non-root login user` if run from a root shell
> or via direct root SSH.

### What changes in single-user mode

| | Two-user split (default) | Single-user mode |
|---|---|---|
| Service user | `dispatch-svc` | `$SUDO_USER` (your login) |
| Agent user | `dispatch` | `$SUDO_USER` (same) |
| Service home | `/home/dispatch-svc` | Your home directory |
| Sudoers fragment | `/etc/sudoers.d/dispatch-svc` installed | Not installed |
| `DISPATCH_AGENT_SUBPROCESS_USER` | Set to `dispatch` | Stripped from `.env` |
| POSIX isolation | `dispatch-svc` cannot read agent files and vice-versa | **None** |
| User creation | `dispatch-svc` and `dispatch` created | Skipped (user already exists) |

### Security trade-off

> ⚠️ **No POSIX isolation between service and agent.** In single-user mode the dispatched
> Claude Code subprocess runs with full access to the dispatch service's `.env` file
> (including `ANTHROPIC_API_KEY`, `AGENT_GTD_API_KEY`, `DISPATCH_API_KEY`) and can
> modify `/etc/systemd/system/dispatch-api.service` if the account has sudo access.

- Accept this trade-off only on personal machines where you trust all processes running
  under your account.
- **The default mode remains the two-user split** — single-user is opt-in via
  `DISPATCH_SINGLE_USER=1`.

### Run-as-self (enterprise: inherit your own auth)

Single-user mode has a second, powerful use beyond a personal machine: running as
**your real, already-authenticated developer identity**. This is often the cleanest
setup behind a corporate boundary, because the dispatched agent inherits **both**:

- your interactive **Claude Code login** — so you need neither `CLAUDE_CODE_OAUTH_TOKEN`
  nor `ANTHROPIC_API_KEY` for the agent (the `claude` binary is authenticated out of
  band by your login or your org's managed distribution); and
- your **git auth to internal repos** (e.g. GitFarm) — so the agent clones and pushes
  internal code *as you*, with no separate deploy key or service account.

Pair it with a Bedrock planner (`DISPATCH_PLANNER_PROVIDER=bedrock` + `AWS_REGION`, AWS
credentials via the standard chain) and the host needs **no Anthropic credentials at
all** — every LLM call authenticates out of band.

Because the installer then runs as *you*, it is deliberately conservative with your
home directory: it touches only `$AGENT_WORKSPACE`, never your `$HOME` (a group-writable
home trips sshd `StrictModes` → SSH lockout), and in single-user mode it does **not**
chown/chmod or generate keys in your `~/.ssh` — it uses your existing ssh + git auth
as-is. If a git host the agent must clone from is not yet in your `known_hosts`, seed it
yourself: `ssh-keyscan <host> >> ~/.ssh/known_hosts`.

### Mode mismatch protection

The installer refuses to create a mixed state. If you run single-user mode on a host
already configured for two-user mode (or vice versa), it exits immediately with a clear
explanation listing the conflicting artifacts. To switch modes, perform a full rollback
first (see [Rollback procedure](#rollback-procedure)), then re-run with the new mode.

### Dry-run preview

```bash
sudo --preserve-env=DISPATCH_SINGLE_USER DISPATCH_SINGLE_USER=1 ./setup-dispatch-host.sh --dry-run
```

The banner will show `Mode: SINGLE-USER (user=<your-login>)` followed by `Would:` lines
for every step. Note: mode-mismatch checks **do fire** under `--dry-run` — if the host
has two-user artifacts, the dry run exits non-zero (same as a real run would).

### Side effects on your account

The following actions are applied to the **login user's own home directory** when the
installer runs in single-user mode. Review them before the first run; use `--dry-run` to
preview exactly what the script would touch.

**(a) Workspace directory permissions.** The installer may `chmod` and adjust group
ownership of `~/workspace` (creating it if absent). Specifically, it sets mode `2775`
(group-writable + setgid) on the workspace directory so that any service process can
read run artifacts. Verify the expected mutations with
`sudo --preserve-env=DISPATCH_SINGLE_USER DISPATCH_SINGLE_USER=1 ./setup-dispatch-host.sh --dry-run`
before applying.

**(b) Env file placement — `~/.env` is NOT read.** The installer writes secrets to
`${HOME}/.config/agent-gtd-dispatch/env` (mode `0600`, directory mode `0700`). This file
is what the systemd unit loads via `EnvironmentFile=`. Any pre-existing `~/.env` in your
home directory is **not** consulted and will not collide — but if you previously stored
service variables there you will need to migrate them to the new path.

**(c) Phase 1 SSH halt also fires in single-user mode.** On a truly fresh box, the
installer generates a fresh `ed25519` keypair under `~/.ssh/` for your login account and
halts with an ACTION REQUIRED banner — identical to the two-user flow described in
[Fresh box install](#fresh-box-install) above. Authorize the printed public key on your
git host, then re-run the installer with the same arguments to complete Phase 2.

### Architecture — single-user layout

```
┌─────────────────────────────────────────────────────────┐
│ personal-box                                            │
│                                                         │
│  alice (service + agent — same account)                 │
│    /home/alice/agent-gtd-dispatch/       ← working      │
│    /home/alice/.config/agent-gtd-dispatch/env ← secrets │
│    /home/alice/workspace/{run_id}/       ← agent work   │
│    systemd: dispatch-api.service         ← FastAPI      │
│                                                         │
│  (no /etc/sudoers.d/dispatch-svc)                       │
└─────────────────────────────────────────────────────────┘
```

---

## Authentication & pairing

Before the service can dispatch agents, two credentials must always be present in the env
file (`AGENT_GTD_API_KEY` and `DISPATCH_API_KEY`, below), plus an agent-auth credential
**unless Claude Code is already authenticated by the environment** (see the next section).

### `CLAUDE_CODE_OAUTH_TOKEN` — agent subprocess auth

**What it is**: The OAuth token Claude Code uses to authenticate against Anthropic's API.
Dispatched agents run as unattended headless subprocesses — `claude login` cannot be
called interactively at dispatch time — so the token must be pre-populated in the service
env file.

> **Environments where Claude Code is already authenticated.** If you run an
> enterprise/managed Claude Code distribution, an internal wrapper, or a Bedrock-backed
> login (e.g. corporate setups where `claude` "just works" with no token or API key),
> **skip this token entirely** — leave both `CLAUDE_CODE_OAUTH_TOKEN` and
> `ANTHROPIC_API_KEY` unset. The dispatch service no longer gates Claude Code engines on
> these vars; it attempts the run and lets the binary authenticate however it normally
> does. The trade-off: on a host where the binary is *not* externally authenticated and
> neither var is set, the engine still reports available and the run fails at exec time —
> so for a plain homelab install, set `CLAUDE_CODE_OAUTH_TOKEN` as below. If your wrapper
> authenticates via its own environment variables, those are not forwarded to the agent
> subprocess by default (only the keys in `engines.py::COMMON_ENV_KEYS` / the engine's
> `env_keys` are) — file an issue if you need a passthrough allowlist.

**How to obtain it**: On a machine with a browser (your workstation, not the dispatch
host), run:

```bash
claude setup-token
```

This opens a browser to complete OAuth, then prints the token. Copy the token value
(begins with a long opaque string, not `sk-ant-…`).

**Why only this token — not `ANTHROPIC_API_KEY`?** The `engines.py` module allowlists
only `CLAUDE_CODE_OAUTH_TOKEN` for the subprocess environment of Claude Code engines
(`src/agent_gtd_dispatch/engines.py`, lines 218–263). `ANTHROPIC_API_KEY` is deliberately
excluded from the subprocess env: if it reached the subprocess, Claude Code would switch
from the user's Max subscription to pay-as-you-go API billing (see kb-01512).
`ANTHROPIC_API_KEY` is read in-process by the rollout planner only and never forwarded to
agent subprocesses.

**Where to paste it** — depends on your install mode:

| Mode | Env file path |
|---|---|
| Two-user split (default) | `/home/dispatch-svc/.env` |
| Single-user | `${HOME}/.config/agent-gtd-dispatch/env` |

```bash
# Example line in the env file (either mode):
CLAUDE_CODE_OAUTH_TOKEN=<your-oauth-token>
```

**Token expiry**: OAuth tokens issued via `claude setup-token` expire around February 2027
(kb-01318). Refresh by re-running `claude setup-token` on a browser-capable machine and
updating the env file, then restarting the service (`sudo systemctl restart dispatch-api`).

---

### `AGENT_GTD_API_KEY` — GTD API auth

**What it is**: The API key that authorises the dispatch service to call the Agent GTD API
(fetch items, post comments, update run status). All values carry the `agtd_` prefix.

**Where to mint it**: In the Agent GTD web app, go to **Settings → API keys** and click
**New key**. Copy the displayed value — it is shown only once. On a fresh Agent GTD
install where the web UI is not yet accessible, the database seed script prints an initial
key to stdout during first setup.

**Worked example**:

```bash
# 1. Mint the key in the GTD app: Settings → API keys → New key
#    You will see something like:
#    agtd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 2. Paste it into the service env file:
#    Two-user:   /home/dispatch-svc/.env
#    Single-user: ~/.config/agent-gtd-dispatch/env
AGENT_GTD_API_KEY=agtd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Do **not** commit this value to git. The env file has mode `0600` and is listed in
`.gitignore` by default.

---

### `DISPATCH_API_KEY` — REST API bearer token

This key authorises callers (the GTD system, your shell) to make requests to *this*
dispatch service. It is auto-minted by Step 3.5 of the installer if absent — you do not
need to generate it manually on installed hosts. See
[DISPATCH_API_KEY auto-minting (Step 3.5)](#dispatch_api_key-auto-minting-step-35) for
how it is generated, how to register it in the GTD UI, and how to rotate it.

---

## Environment file reference

All variables documented in `templates/dispatch-env.tmpl`. Key variables:

| Variable | Required | Description |
|---|---|---|
| `DISPATCH_API_KEY` | ✓ | Bearer token callers must supply to the REST API (auto-minted by Step 3.5 if absent; see below) |
| `AGENT_GTD_URL` | ✓ | Agent GTD API base URL (e.g. `https://r7-research:8443`) |
| `AGENT_GTD_API_KEY` | ✓ | Agent GTD API key (`agtd_…` prefix) |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API key for Claude Code subprocesses |
| `DISPATCH_AGENT_SUBPROCESS_USER` | ✓ (prod) | Agent user for user-switching (`dispatch`). Leave empty in dev to disable. |
| `DISPATCH_WORKSPACE_ROOT` | – | Override workspace root (default: `~/workspace` relative to agent user) |
| `DISPATCH_MAX_TURNS` | – | Claude Code turn cap (default: 100) |
| `DISPATCH_TIMEOUT_SECONDS` | – | Agent subprocess wall-clock timeout in seconds (default: 1800) |
| `OLLAMA_BASE_URL` | – | Root URL of an Ollama instance for `claude-code-ollama` engine dispatches |
| `OLLAMA_DEFAULT_MODEL` | – | Default Ollama model (default: `qwen3.6:35b`) |
| `DISPATCH_PLANNER_PROVIDER` | – | Planner LLM provider: `anthropic` (default) or `bedrock`. See [Bedrock planner provider](#bedrock-planner-provider-corporateport-environments) below. |
| `DISPATCH_PLANNER_BEDROCK_MODEL` | – | Bedrock model ID (default: `global.anthropic.claude-sonnet-4-6`). Only used when `DISPATCH_PLANNER_PROVIDER=bedrock`. |
| `AWS_REGION` | – | AWS region for Bedrock API calls (default: `us-east-1` per SDK fallback). Only used when `DISPATCH_PLANNER_PROVIDER=bedrock`. |
| `TEAM_KB_DATABASE_URL` | – | Team KB Postgres connection string. Read by installer Step 4.6 (not the service) and injected into the `team-kb` MCP server's per-server env; if unset, `team-kb` registration is skipped |
| `KB_ANTHROPIC_API_KEY` | – | Anthropic key for the KB MCP servers' own LLM calls. Read by installer Step 4.6 and injected per-server as `ANTHROPIC_API_KEY` — deliberately NOT named `ANTHROPIC_API_KEY` in `.env`, so it never reaches the agent's process env (which would flip Claude Code billing off the Max subscription) |

The env file is installed at `/home/dispatch-svc/.env` with mode `0600`,
owned by `dispatch-svc`. In single-user mode it is installed at
`${HOME}/.config/agent-gtd-dispatch/env` instead (mode `0700` on the directory,
`0600` on the file). Never commit it to git.

### Bedrock planner provider (corporate/port environments)

In environments where the Anthropic API is unreachable through corporate egress
(e.g. internal ports where Claude access is routed through Amazon Bedrock),
set `DISPATCH_PLANNER_PROVIDER=bedrock`. This affects the in-process rollout
planner (`POST /plan`) only — Claude Code agent subprocess execution is
unchanged.

**Credential resolution:** AWS credentials are resolved from the standard AWS
credential chain (`AWS_PROFILE`, environment variables, instance metadata, etc.).
Set `AWS_PROFILE` in the service `.env` to select a named profile.

**Region gotcha:** the anthropic SDK reads `AWS_REGION` for the Bedrock region;
if unset it defaults to `us-east-1`. `AWS_PROFILE` alone does **NOT** supply the
region — the SDK does not read `~/.aws/config` for the region. Set `AWS_REGION`
explicitly in the service `.env`.

**Model ID:** the default `global.anthropic.claude-sonnet-4-6` uses the Bedrock
global cross-region inference endpoint. Use the `us.` regional CRIS variant
(e.g. `us.anthropic.claude-sonnet-4-6`) if your environment requires data
residency guarantees (+10% pricing applies). Do NOT reuse the Anthropic
first-party model id (`claude-sonnet-4-6`) on the Bedrock client — it will error.

---

## Talos engine provisioning (--with-talos)

Step 4.5b of the installer optionally builds and installs the `talos` binary for the agent user. This binary powers the `talos-*` engine family (`talos-haiku`, `talos-sonnet`, `talos-opus`, `talos-qwen`, `talos-glm`). The step is **opt-in** — pass `--with-talos` to enable it. Without the flag the step prints a single `[SKIP]` line and mutates nothing.

### When to use it

Add `--with-talos` when you want to dispatch `talos-*` engines on this host. Skip it on hosts that dispatch only `claude-code-*` engines — the `/info` advertisement (`is_engine_available`) will simply omit the talos engines.

### Prerequisites

- The agent user's SSH key must be authorised on the git host that serves `HARNESS_DESIGN_REPO_URL` (default: `git@ubuntu-vm01:repos/harness-design`). If the key is not yet authorised the installer fails with a clear `[ERROR]` and instructions before touching anything.
- Internet access for `rustup` and `cargo-binstall` installers (or pre-install Rust for the agent user manually before running).

### What the step does

Seven sub-steps, all idempotent:

| Sub-step | Action | Skip condition |
|---|---|---|
| **A** build-essential | `apt-get install -y build-essential` | `dpkg-query` reports already installed |
| **B** rustup | Installs Rust toolchain for `AGENT_USER` via `sh.rustup.rs` | `~/.cargo/bin/rustup` exists |
| **C** cargo-nextest | Installs `cargo-binstall` then `cargo-nextest` for `AGENT_USER` | `~/.cargo/bin/cargo-nextest` exists |
| **D** harness-design clone/pull | Clones or fast-forward pulls `HARNESS_DESIGN_REPO_URL` → `~/harness-design` as `AGENT_USER` | Always runs (pull is idempotent) |
| **E** cargo build | `cargo build --release -p talos` inside `~/harness-design` as `AGENT_USER` | Always runs (Cargo incremental makes re-run near-instant) |
| **F** install binary | Copies built binary to `~/.local/bin/talos` with `install -m 0755` | Skipped when destination is byte-identical to the built binary |
| **G** TALOS_BIN in .env | Writes `TALOS_BIN=/home/dispatch/.local/bin/talos` into `SERVICE_ENV` | Skipped when value already matches |

### Environment variable override

`HARNESS_DESIGN_REPO_URL` controls the git remote for the harness-design clone. Default: `git@ubuntu-vm01:repos/harness-design`. Override on the installer command line:

```bash
sudo HARNESS_DESIGN_REPO_URL=git@your-git-host:path/harness-design \
     ./setup-dispatch-host.sh --with-talos
```

### Install path and sudoers

The binary is installed as a **copy** (not a symlink) to `/home/dispatch/.local/bin/talos`. The sudoers fragment (`/etc/sudoers.d/dispatch-svc`) always includes this path in its `NOPASSWD` allowlist, regardless of whether `--with-talos` was passed — this avoids a sudoers update on first talos install. The fragment also includes `/home/dispatch/.cargo/bin` in `secure_path` so `cargo` and `cargo-nextest` are reachable across the sudo boundary.

### TALOS_BIN in the env file

Sub-step G writes `TALOS_BIN=/home/dispatch/.local/bin/talos` to the service env file. The dispatch service reads this at startup and uses it as the absolute path to the `talos` binary. If you place the binary at a non-standard location, override `TALOS_BIN` in the env file directly instead of relying on the installer.

### Verifying talos install

```bash
# Binary accessible as the agent user:
sudo -u dispatch -H bash -lc 'talos --version'
# → talos X.Y.Z  (RC 0)

# TALOS_BIN set in the service env:
sudo grep '^TALOS_BIN=' /home/dispatch-svc/.env
# → TALOS_BIN=/home/dispatch/.local/bin/talos

# talos engines appear in /info:
curl -sf http://localhost:8100/info | python3 -m json.tool | grep talos
# → "talos-sonnet", "talos-haiku", etc. listed under available_engines
```

### Rollback (Step 4.5b)

```bash
sudo -u dispatch rm -f /home/dispatch/.local/bin/talos
# Optionally remove the harness-design clone:
sudo -u dispatch rm -rf /home/dispatch/harness-design
# Remove TALOS_BIN from the env file:
sudo sed -i '/^TALOS_BIN=/d' /home/dispatch-svc/.env
sudo systemctl restart dispatch-api
```

---

## MCP servers for the agent user

Step 4.6 of the installer registers up to four MCP servers for the `dispatch` (agent)
user. This gives dispatched Claude Code agents tool access to GTD, the knowledge
bases, and AWS documentation — enabling proper attribution on GTD comments instead of
falling back to raw `curl` calls.

| Server | Purpose |
|---|---|
| `agent-gtd` | GTD items, comments, and dispatch (prevents `created_by="human"` regression) |
| `personal-kb` | Knowledge base lookups (decisions, lessons learned, project conventions) |
| `team-kb` | Team knowledge base — **conditional**: only registered when `TEAM_KB_DATABASE_URL` is set in the service `.env` |
| `aws-documentation-mcp-server` | AWS docs for any AWS-related implementation work |

Step 4.6 reads `TEAM_KB_DATABASE_URL` and `KB_ANTHROPIC_API_KEY` out of the installed
service `.env` and exports them before sourcing `templates/mcp-servers.sh`, so the KB
servers get their secrets in their **per-server** MCP env blocks (see the
[environment file reference](#environment-file-reference)). If either is unset the
installer prints a `[WARN]` and continues — `team-kb` is skipped entirely, and the KB
servers register without an Anthropic key (LLM features degraded).

Registration is **per-host and per-user** using `--scope user`, which writes to
`/home/dispatch/.claude.json` in the two-user split. **In single-user mode the agent
user is your own login account, so `--scope user` writes to YOUR `~/.claude.json`.**

> ⚠️ **Adapt this to your environment.** The entries in `templates/mcp-servers.sh`
> hardcode homelab-specific values: `uvx` sources pointing at
> `git+ssh://git@<your-git-host>/home/git/repos/...` and KB identities
> (`KB_CONTRIBUTOR=jason`, `KB_TEAM=grit-mile`). On any other environment these
> register successfully but **fail at runtime** — the Step 4.6 smoke test only greps
> for the server name in `claude mcp list`, so it passes regardless. Edit
> `templates/mcp-servers.sh` to point at your git host and KB identities (or trim the
> array to just the servers you need) **before** running the installer.

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
# → team-kb: ...                (only if TEAM_KB_DATABASE_URL was set at install time)

# Inspect ~/.claude.json directly:
ssh <HOST> 'sudo cat /home/dispatch/.claude.json' | jq '.mcpServers | keys'
# → ["agent-gtd", "aws-documentation-mcp-server", "personal-kb"]
# → (plus "team-kb" on hosts where TEAM_KB_DATABASE_URL was set)
```

---

## DISPATCH_API_KEY auto-minting (Step 3.5)

Step 3.5 of the installer mints a fresh `DISPATCH_API_KEY` into `/home/dispatch-svc/.env` if the value is absent, empty, or the legacy `changeme` placeholder — and **skips if any other value is already present**.

### Why this matters

`DISPATCH_API_KEY` is the Bearer token the REST API checks on every incoming dispatch request. Without it, the service starts but rejects all calls with HTTP 401. Previously, operators had to mint the key by hand and remember to paste it into the GTD UI's dispatch-host settings. Forgetting either step left hosts unreachable.

The never-clobber rule is equally important: silently rotating the key on a true-up run would break the app-side pairing until the operator manually re-registers the new value in the GTD UI. Step 3.5 refuses to clobber an existing value — rotation is always intentional and manual.

### What the step does

1. **Checks** that `$SERVICE_ENV` (`/home/dispatch-svc/.env`) exists — dies if not (Step 3 invariant).
2. **Reads** the current value of `DISPATCH_API_KEY` from the env file (using the shared `_read_env_var` helper, which strips surrounding quotes).
3. **Skips** if the value is non-empty AND not the legacy `changeme` placeholder. Prints a `[SKIP]` message. `changeme` is treated as absent and replaced with a freshly minted key (so old-template hosts migrate automatically).
4. **Mints** if absent, empty, or `changeme`: generates a 43-char URL-safe key via `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, rewrites the file atomically via `mktemp` + `install -m 0600`, then prints an **ACTION REQUIRED** banner with the minted key value and instructions to register it in the GTD UI before the service restarts in Step 6.
5. **Dry-run**: prints a `[DRY] Would: mint DISPATCH_API_KEY …` line and makes zero mutations (no key is generated).

### Verifying the minted key

```bash
sudo grep '^DISPATCH_API_KEY=' /home/dispatch-svc/.env
# → DISPATCH_API_KEY=<43-char-url-safe-value>
```

### Rotating the key

To rotate `DISPATCH_API_KEY` on an existing host:

```bash
# 1. Clear the line (leave the key name, empty the value):
sudo sed -i 's/^DISPATCH_API_KEY=.*/DISPATCH_API_KEY=/' /home/dispatch-svc/.env

# 2. Re-run the installer — Step 3.5 will mint a new key and print the banner:
sudo ./setup-dispatch-host.sh

# 3. Copy the printed key and re-register it in:
#    Agent GTD Settings → Dispatch hosts → this host's API Key

# 4. Restart the service to pick up the new key:
sudo systemctl restart dispatch-api
```

Do **not** clear the value while the service is handling live traffic without immediately completing steps 3–4, or dispatches will return 401 during the window.

---

## Pre-commit template directory (Step 4.7)

Step 4.7 of the installer sets up git's template directory for the `dispatch` (agent) user so that every repository the agent clones inherits pre-commit hook shims automatically.

### Why this matters

Dispatched build agents clone repositories fresh for every run. Without hook shims, they bypass the same lint/format/typecheck gates (`ruff`, `ruff-format`, `mypy`) that the lead developer's squash-merge triggers. This divergence surfaced in two consecutive overnight dispatch waves (kb-01785, kb-01790): a mypy redefinition error the agent could not see, and a `noqa: S603` comment removed as "unused" (RUF100 in the agent's environment) that was load-bearing under the repo's hook configuration.

The fix is applied at the **host provisioning level** — not per-repo scripts and not dispatch-service code — so it covers every present and future repository the agent works in.

### What the step does

Three sub-actions, all targeting the `dispatch` (AGENT_USER) account:

1. **Install pre-commit** — `uv tool install pre-commit` places the binary at `/home/dispatch/.local/bin/pre-commit`. Skipped if already installed.
2. **Set `init.templateDir`** — writes the absolute path `/home/dispatch/.git-template` into `dispatch`'s global git config. Every subsequent `git clone` or `git init` by the agent user copies hooks from this directory. Skipped if already set to the correct value.
3. **Render hook shims** — `pre-commit init-templatedir -t pre-commit -t commit-msg -t pre-push /home/dispatch/.git-template` writes the shim files. The three `-t` flags are the union of hook types used across the fleet. Always runs (idempotent re-render of the shims).

### Safety: `--skip-on-missing-config`

The shim files written by `init-templatedir` include a `--skip-on-missing-config` flag by default. This means:

- Repositories that **have** `.pre-commit-config.yaml` → hooks run normally.
- Repositories that **do not** have `.pre-commit-config.yaml` (e.g. scratch repos, probe dirs) → hooks exit 0 silently, commit succeeds untouched.

### Known cost: first-commit venv build

The first `git commit` in a fresh clone on a newly-provisioned host triggers pre-commit to build its per-hook virtual environments. On a Raspberry Pi this can take 30–60 seconds. After that, `~/.cache/pre-commit` is warm and shared across all clones on the same host, so subsequent commits are fast. Do not attempt to pre-warm the cache in the script — the build happens automatically on first use.

### Verifying the pre-commit template install

After re-running the installer on a host, paste these commands to confirm all three sub-actions took effect:

```bash
# (a) pre-commit binary is accessible as the dispatch user
sudo -u dispatch -H bash -lc 'pre-commit --version'
# → pre-commit X.Y.Z  (RC 0)

# (b) init.templateDir is set to the correct absolute path
sudo -u dispatch -H git config --global --get init.templateDir
# → /home/dispatch/.git-template

# (c) hook shim files are present in the template directory
ls /home/dispatch/.git-template/hooks/
# → contains pre-commit, commit-msg, pre-push

# (d) a new git init picks up the shims (confirms init.templateDir is honoured)
sudo -u dispatch -H bash -lc 'cd /tmp && rm -rf hook-probe && git init hook-probe && ls hook-probe/.git/hooks/'
# → contains pre-commit, commit-msg, pre-push

# (e) OPTIONAL — a fresh clone has the shims. Step (d)'s local `git init` probe is the
#     canonical check; this one additionally proves clone-over-SSH works. The example
#     uses the homelab git server — replace with any repo on your git host that
#     contains .pre-commit-config.yaml (requires the dispatch key authorized there):
sudo -u dispatch -H bash -lc 'cd /tmp && rm -rf agent_gtd_probe && git clone git@<your-git-host>:repos/agent_gtd agent_gtd_probe && ls agent_gtd_probe/.git/hooks/'
# → contains pre-commit, commit-msg, pre-push

# (f) a commit in a config-less repo succeeds — shims no-op via --skip-on-missing-config
sudo -u dispatch -H bash -lc 'cd /tmp/hook-probe && git commit --allow-empty -m "probe"'
# → exits 0; also confirm the shim body contains the flag:
grep -l skip-on-missing-config /home/dispatch/.git-template/hooks/*
# → lists pre-commit, commit-msg, pre-push (all shims carry it)
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

### Step 5b — Sudoers fragment
```bash
sudo rm /etc/sudoers.d/dispatch-svc
```

### Step 5a — Claude symlink
```bash
sudo rm /usr/local/bin/claude
```

### Step 4.7 — Pre-commit template
```bash
sudo -u dispatch -H git config --global --unset init.templateDir
sudo -u dispatch -H rm -rf /home/dispatch/.git-template
sudo -u dispatch -H bash -lc 'uv tool uninstall pre-commit'
```
(`uv` lives at `/home/dispatch/.local/bin/uv`, which is not on sudo's search path —
the `bash -lc` login shell is required, same as the installer itself uses.)

### Step 4.6 — MCP servers
```bash
# Per server (agent-gtd, personal-kb, aws-documentation-mcp-server, and team-kb if registered):
sudo -u dispatch -H bash -lc 'claude mcp remove <name> --scope user'
# Or remove all registrations at once:
sudo rm /home/dispatch/.claude.json
```

### Step 4.5 — Claude Code
No separate rollback — Step 4's removal of `/home/dispatch/.local` also deletes the
`claude` binary (and the `pre-commit` tool from Step 4.7).

### Step 4 — Dependencies (uv)
```bash
sudo -u dispatch-svc rm -rf /home/dispatch-svc/.local
sudo -u dispatch rm -rf /home/dispatch/.local
```

### Step 3.5 — DISPATCH_API_KEY
```bash
# No separate rollback — the key lives inside ${SERVICE_ENV}; removing the env file (Step 3 rollback) deletes it.
# To rotate without full rollback:
sudo sed -i 's/^DISPATCH_API_KEY=.*/DISPATCH_API_KEY=/' /home/dispatch-svc/.env && sudo ./setup-dispatch-host.sh
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

### Rollback — single-user mode

The steps above are two-user specific (`/home/dispatch-svc/...`, `deluser`, sudoers).
On a single-user host none of those paths exist — the artifacts live under **your own
account** instead. To fully roll back (e.g. before switching to two-user mode):

```bash
# Systemd unit (Step 6) — same as two-user
sudo systemctl stop dispatch-api
sudo systemctl disable dispatch-api
sudo rm /etc/systemd/system/dispatch-api.service
sudo systemctl daemon-reload

# MCP registrations (Step 4.6) — registered in YOUR ~/.claude.json
claude mcp remove agent-gtd --scope user
claude mcp remove personal-kb --scope user
claude mcp remove aws-documentation-mcp-server --scope user
claude mcp remove team-kb --scope user   # only if it was registered

# Pre-commit template (Step 4.7)
git config --global --unset init.templateDir
rm -rf ~/.git-template
uv tool uninstall pre-commit

# Env file, repos, workspace (Steps 3 / 2 / 1)
rm -rf ~/.config/agent-gtd-dispatch
rm -rf ~/agent-gtd-dispatch ~/agent_gtd ~/workspace
```

There is no sudoers fragment, no `/usr/local/bin/claude` symlink (Step 5a is skipped
in single-user mode), and no system users to delete. Do **not** `rm -rf ~/.local` —
unlike the dedicated `dispatch` user's home, your `~/.local` holds your own tools
(`uv` and Claude Code were installed there and you likely want to keep them).

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
# dispatch-svc ALL=(dispatch) NOPASSWD: /usr/bin/git, /home/dispatch/.local/bin/uv, /home/dispatch/.local/bin/claude, /usr/local/bin/claude, /usr/bin/rm, /usr/bin/python3, /bin/bash, /usr/bin/mkdir
```

If missing or wrong, re-run:
```bash
sudo ./setup-dispatch-host.sh
```

The installer will detect the mismatch and reinstall the correct fragment.

---

### SSH host key verification failed during git clone

**Symptom**: Step 2 (Repos) fails with `Host key verification failed` or `The authenticity of host '<git-host>' can't be established`.

**Cause**: The `dispatch-svc` user's `~/.ssh/known_hosts` is missing your git server's host key — the new service account has not connected to it before. The installer seeds `known_hosts` for the host(s) derived from `DISPATCH_REPO_URL` / `AGENT_GTD_REPO_URL`, so this usually means those overrides weren't set (the clone pointed at the homelab default) or the host uses a non-standard SSH port.

**Fix**: Re-run the installer with the correct `DISPATCH_REPO_URL` / `AGENT_GTD_REPO_URL` — it seeds `known_hosts` for whatever host they resolve to. For a non-standard SSH port, seed it manually:
```bash
ssh-keyscan -p <port> <your-git-host> | sudo tee -a /home/dispatch-svc/.ssh/known_hosts
sudo -u dispatch-svc git clone git@<your-git-host>:<path>/agent-gtd-dispatch /home/dispatch-svc/agent-gtd-dispatch
```
See [Adapting to your own git host](#adapting-to-your-own-git-host) for the `DISPATCH_REPO_URL` / `AGENT_GTD_REPO_URL` overrides.

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

The installer expects the binary at exactly `/home/dispatch/.local/bin/claude` (no
override exists). If it installed elsewhere, check the actual location with
`sudo -u dispatch bash -c 'which claude 2>/dev/null || echo not found'` and symlink it
to the expected path.

---

### Installer halts at "ACTION REQUIRED: Add SSH public key"

**Symptom**: Step 1 prints a yellow banner and exits with `[ERROR] SSH public key not yet authorized`.

**Cause**: This is a normal Phase 1 halt on a fresh host. The `dispatch` agent user had no
SSH keypair, so the installer generated one and is waiting for you to authorize it.

**Fix**: This is expected — follow the [Fresh box install](#fresh-box-install) two-phase flow above:
1. Copy the printed public key.
2. Authorize it on your git host (self-hosted: append to `<your-git-host>:~/repos/.ssh/authorized_keys`; GitHub: add as a deploy key / account SSH key).
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

### Two-user split (default)

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

### Single-user mode (`DISPATCH_SINGLE_USER=1`)

```
┌─────────────────────────────────────────────────────────┐
│ personal-box                                            │
│                                                         │
│  alice (service + agent — same account)                 │
│    /home/alice/agent-gtd-dispatch/          ← working   │
│    /home/alice/.config/agent-gtd-dispatch/env ← secrets │
│    /home/alice/workspace/{run_id}/          ← agent work│
│    systemd: dispatch-api.service            ← FastAPI   │
│                                                         │
│  (no /etc/sudoers.d/dispatch-svc)                       │
│  (no separate dispatch/dispatch-svc users)              │
└─────────────────────────────────────────────────────────┘
```

The login user runs both the FastAPI process and agent subprocesses directly
— no `sudo -u` boundary. All files (service config, agent workspaces) are
owned by the same account. See [## Single-user mode](#single-user-mode) for
the security trade-offs.

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
