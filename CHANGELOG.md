# CHANGELOG

<!-- version list -->

## v1.18.1 (2026-07-13)

### Bug Fixes

- Talos worker push --no-verify to skip dev pre-push hooks
  ([`46ac186`](https://github.com/repos/agent-gtd-dispatch/commit/46ac18654141e7a81da7c87bf6c7a6d4aef2e281))


## v1.18.0 (2026-07-13)

### Features

- Enable workspace (multi-repo) dispatch to talos engines
  ([`06894ad`](https://github.com/repos/agent-gtd-dispatch/commit/06894ade4c2d0b68a9f2e0a97a61377883ade02e))

- Wheel-based deploys — install from pi-04 pypi.lab, no working copy on hosts
  ([`f9a3335`](https://github.com/repos/agent-gtd-dispatch/commit/f9a333507b69ea69159ea643001a0e3d1bb6253d))


## v1.17.0 (2026-07-11)

### Bug Fixes

- No spurious ssh-keyscan WARN for http/https git remotes
  ([`23e92ee`](https://github.com/repos/agent-gtd-dispatch/commit/23e92eeb8dd00453e7ea012476c560bbebf0d4bf))

- Reclassify pushed-but-lingered BUILD runs as succeeded, not timed_out
  ([`a0c5ad9`](https://github.com/repos/agent-gtd-dispatch/commit/a0c5ad9febead2826cf8f6d457961bbc3f69757f))

- **sudoers**: Keep talos backend/model env vars across the sudo boundary
  ([`474a7b3`](https://github.com/repos/agent-gtd-dispatch/commit/474a7b300899c8a86aacc8e7680b641d882f15b2))

- **tests**: Isolate _env fixture from ambient OLLAMA_*/TALOS_BIN env
  ([`6ff3792`](https://github.com/repos/agent-gtd-dispatch/commit/6ff37926ec3210d3a108595703e8292dc712f554))

### Chores

- Finalize talos-update.sh artifact base URL to artifacts.lab
  ([`88a4e8b`](https://github.com/repos/agent-gtd-dispatch/commit/88a4e8b7a150d92f8aa9aa909c151f47f7ab4cc4))

### Features

- --with-postgres provisions local Postgres + pgvector on dispatch hosts
  ([`860cb25`](https://github.com/repos/agent-gtd-dispatch/commit/860cb252e6a4e5f1a9f6b71c26a21aed18b12c7e))

- Idempotent setup-dispatch-host.sh + --with-talos engine provisioning
  ([`474c881`](https://github.com/repos/agent-gtd-dispatch/commit/474c88124f9449f8b5bd19b843830a8b2761e3a2))

- Raise talos --gate-timeout-secs to 900s (overridable) for cold Pi gate runs
  ([`2b35fe0`](https://github.com/repos/agent-gtd-dispatch/commit/2b35fe00ff458e76709073a7bd0acf068b26cc1a))

- Register the talos-* build-engine family (worker owns git + comment-back)
  ([`ad8785a`](https://github.com/repos/agent-gtd-dispatch/commit/ad8785ad21ee2ad1e15afc61ed5b70a8cea6e543))

- Talos-update.sh — pull the published talos binary from pi-04 to the fleet
  ([`82ebc73`](https://github.com/repos/agent-gtd-dispatch/commit/82ebc73dbd5c7ceb469e8dee51e31f22466faa8a))

- Warn on unexpected/missing keys in produce_dag tool output
  ([`87d9663`](https://github.com/repos/agent-gtd-dispatch/commit/87d96633f02ed8cacd28be56ba704d24a87cc3d8))

- Wire claude-code-glm engine (glm-5.2 via Ollama Cloud)
  ([`443c999`](https://github.com/repos/agent-gtd-dispatch/commit/443c999d06968ad0ac90324438ab7006b00ee057))

### Testing

- Flywheel guard — talos_env_overlay keys must be a subset of sudoers env_keep
  ([`443ced1`](https://github.com/repos/agent-gtd-dispatch/commit/443ced156ed3fa53ea5f9c3c95ab31e23477ea75))


## v1.16.0 (2026-07-07)

### Bug Fixes

- **dispatch**: Check-and-clean stale workspace at launch (crash-safe)
  ([`9839909`](https://github.com/repos/agent-gtd-dispatch/commit/9839909a7ec6fdd7125feebacee090473a8126c2))

### Features

- Per-run callback token in dispatch service (Phase 1 of 2)
  ([`40dedf1`](https://github.com/repos/agent-gtd-dispatch/commit/40dedf18b535f7b654e36f6bcc50ac62280d166b))

- **176502fa**: Add engine_actual to protocol RunResponse and set git identity per engine
  ([`3fe9af3`](https://github.com/repos/agent-gtd-dispatch/commit/3fe9af36a0633687c998877fa2f1f7771936ca28))

- **5f9552d7**: Engine_actual truthful end-to-end (dispatch test matrix fill)
  ([`0e8216b`](https://github.com/repos/agent-gtd-dispatch/commit/0e8216bf62cf3b7a41062f62b9a3f6140f6fee46))

- **89e45f3b**: Rollout DAG planner deterministic same-file overlap edges
  ([`3301b5e`](https://github.com/repos/agent-gtd-dispatch/commit/3301b5e116a135b06286ef337d108ef8bbb7be81))

- **db559e9f**: Set HEADLESS_BUILD_ENGINE env var on dispatched CC subprocess
  ([`53fa544`](https://github.com/repos/agent-gtd-dispatch/commit/53fa5442718d479883c3f0765ba6da787dbadf39))


## v1.15.0 (2026-06-15)

### Bug Fixes

- **0f657f97**: --dry-run no longer aborts at Step 3.5 when env file missing
  ([`2e2c151`](https://github.com/repos/agent-gtd-dispatch/commit/2e2c1515c5253616f5e12f93d2fa7965257462b2))

- **19656dc9**: Replace sudo -u with runuser to work under root-only sudoers
  ([`a780b6e`](https://github.com/repos/agent-gtd-dispatch/commit/a780b6e224febc2db7e23ad6b4f7c13f78e7d9be))

- **7ed48d77**: Portable mcp-servers.sh defaults + AGENT_GTD env injection
  ([`67630fb`](https://github.com/repos/agent-gtd-dispatch/commit/67630fb9476a7d56b236f1352e22d56f1d6c35b0))

- **afd91ed2**: Derive real primary group to fix AL2023/RHEL installer
  ([`52f673a`](https://github.com/repos/agent-gtd-dispatch/commit/52f673ab271d318fe4ea12331369dc67c3865639))

### Documentation

- Document run-as-self single-user topology (inherit developer auth)
  ([`05c6d38`](https://github.com/repos/agent-gtd-dispatch/commit/05c6d38a9889510663e0217fcae501f4490546bc))

- Genericize ubuntu-vm01 references; document externally-authenticated
  ([`df96ec5`](https://github.com/repos/agent-gtd-dispatch/commit/df96ec546b1a4e145078c2590dd0dda7040e35ae))

### Features

- Default installer git remotes to public GitHub
  ([`dd6a412`](https://github.com/repos/agent-gtd-dispatch/commit/dd6a412fdd94def008c592cd8d7e0fb4beb687da))

- Support externally-authenticated Claude Code + portability hardening
  ([`df96ec5`](https://github.com/repos/agent-gtd-dispatch/commit/df96ec546b1a4e145078c2590dd0dda7040e35ae))


## v1.14.0 (2026-06-12)

### Features

- **6eeb9550**: Bedrock provider option for the rollout planner
  ([`7cd72c6`](https://github.com/repos/agent-gtd-dispatch/commit/7cd72c66c3ee898a0da19db068ce666cfd899780))


## v1.13.0 (2026-06-12)

### Bug Fixes

- Allowlist /usr/bin/mkdir in dispatch-svc sudoers
  ([`8dc85e0`](https://github.com/repos/agent-gtd-dispatch/commit/8dc85e0954384ce14862539f80be09ba0868ff16))

- **b4268031**: Harden single-user mode — scope home-dir mutations, XDG env path
  ([`7f806c9`](https://github.com/repos/agent-gtd-dispatch/commit/7f806c95fe58e5318c926ba1a91822dc7f3cf473))

### Chores

- Drop ubuntu-pi-01 from dispatch host rotation
  ([`61e3edf`](https://github.com/repos/agent-gtd-dispatch/commit/61e3edf5bd41db0c994badd500c657e3b2690db7))

### Documentation

- Setup-audit fixes — verified against current code (workflow wf_8865dd55)
  ([`579ccd9`](https://github.com/repos/agent-gtd-dispatch/commit/579ccd98b35667b866233772abd5352aca291e3f))

- **bf37cec6**: Authentication + key pairing end-to-end
  ([`9af8355`](https://github.com/repos/agent-gtd-dispatch/commit/9af8355ae4a99fb97295f47c8c9f08b1ecc51f2b))

### Features

- **315819e4**: Workspace dispatch — multi-repo clone, service-side branch creation
  ([`bf1d298`](https://github.com/repos/agent-gtd-dispatch/commit/bf1d29832aa4d19aba69950d2961a73512285ee0))

- **8aa67eae**: Service-side push verification, multi-repo aware
  ([`c87eca8`](https://github.com/repos/agent-gtd-dispatch/commit/c87eca8692c84038309c5b0d275f2c373d95f93e))

- **ccc7aa17**: Single-user mode for setup-dispatch-host.sh via DISPATCH_SINGLE_USER=1
  ([`2ae294e`](https://github.com/repos/agent-gtd-dispatch/commit/2ae294e0fead24bc5d32ebd7018ace83e06c2752))

- **d31388bf**: Provision pre-commit template directory for the agent user
  ([`67781bb`](https://github.com/repos/agent-gtd-dispatch/commit/67781bb1a9ad36f023bf76f9c041e4375f657f79))

- **d9c20967**: Mint DISPATCH_API_KEY in setup-dispatch-host.sh when absent
  ([`c52e807`](https://github.com/repos/agent-gtd-dispatch/commit/c52e807c71bf904d00ac579e860ba8c5caa86973))

- **dc673fb4**: Multi-repo workspace prep for manage-mode runs
  ([`029d7fe`](https://github.com/repos/agent-gtd-dispatch/commit/029d7fe834aeb2dbd6bec96503f0c032b7b24bb5))

- **f9212037**: Workspace-aware manage prompt with per-repo merge + halt semantics
  ([`da235ea`](https://github.com/repos/agent-gtd-dispatch/commit/da235ea0c28ae2793a648cca17794979d9423a02))


## v1.12.0 (2026-05-31)

### Features

- **996682e2**: Structured logging for manage-recovery / watchdog / lifecycle
  ([`775d2ef`](https://github.com/repos/agent-gtd-dispatch/commit/775d2ef5a2100a7bcaffb81dd1f1071b67a149af))

- **f87043c1**: Watchdog skips a manager healthily waiting on a running build
  ([`5886630`](https://github.com/repos/agent-gtd-dispatch/commit/5886630bba6185ce0a931b542b546daf271477b1))

### Refactoring

- **28e9bc43**: Drop redundant dispatch-side terminal-run filter
  ([`484b1ec`](https://github.com/repos/agent-gtd-dispatch/commit/484b1ecea20590ec16d4e305600375f864a8567e))


## v1.11.1 (2026-05-30)

### Bug Fixes

- Raise manage watchdog stale threshold to 35 min
  ([`87b325f`](https://github.com/repos/agent-gtd-dispatch/commit/87b325fe61f487dc4fdc5f5159a128ec04a6c116))


## v1.11.0 (2026-05-30)

### Bug Fixes

- **06b17687**: Watchdog to recover stale manage-agent rollouts
  ([`e713a59`](https://github.com/repos/agent-gtd-dispatch/commit/e713a59b1cef36ad9b6b25a556abea71ef4de2a1))

- **dispatch**: Atomic capacity check + queue for over-cap dispatches
  ([`d33ba77`](https://github.com/repos/agent-gtd-dispatch/commit/d33ba7753e73c56c442918fef2fe48b5d793ef6b))

- **dispatch**: Make set-head non-fatal in prepare_manage_workspace
  ([`6a76af8`](https://github.com/repos/agent-gtd-dispatch/commit/6a76af8187f14bf2719c00d63058aa8f9bca7dbd))

- **dispatch**: Propagate ~/.local/bin in PATH across sudo boundary
  ([`5e2b5de`](https://github.com/repos/agent-gtd-dispatch/commit/5e2b5de29304515d2a23373c1e1774a5e67d1608))

- **install**: Seed agent-user known_hosts + add MAX_CONCURRENT guidance
  ([`991da21`](https://github.com/repos/agent-gtd-dispatch/commit/991da214a249356e292b7f6a02cd674182030615))

- **sudoers**: Add agent-user claude path to NOPASSWD (hotfix for 605c3ad)
  ([`f077b9f`](https://github.com/repos/agent-gtd-dispatch/commit/f077b9f8b1dd330a2e8e5b3dd51628d875546140))

- **sudoers**: Override secure_path to include agent user's ~/.local/bin
  ([`605c3ad`](https://github.com/repos/agent-gtd-dispatch/commit/605c3ad8e5ccf76d19f6cff981276de5553737a4))

### Chores

- **dispatch**: Remove _MANAGE_ALLOWED_TOOLS restriction
  ([`c072c3b`](https://github.com/repos/agent-gtd-dispatch/commit/c072c3ba275fb477c8e4d9585efb93fde45aa5da))

- **dispatch**: Remove dead 'crashed' rollout terminal state
  ([`52006ea`](https://github.com/repos/agent-gtd-dispatch/commit/52006ea8e8092ce8e6aadd657e3711d705e71680))

### Documentation

- Bootstrap full docs/ scaffold for agent-gtd-dispatch
  ([`eab28ac`](https://github.com/repos/agent-gtd-dispatch/commit/eab28ac12ee9268f911e26b3f3092be0c5db9243))

- Explain how to generate DISPATCH_API_KEY
  ([`1c71eb1`](https://github.com/repos/agent-gtd-dispatch/commit/1c71eb1cf137898eaaae9c012405b97682a58644))

### Features

- **461b2b8a**: Sonnet/haiku engines use moving aliases
  ([`d9d112f`](https://github.com/repos/agent-gtd-dispatch/commit/d9d112f68806153d1b8095cb3f0cbcca04b6f272))

- **c7ec87d5**: Register agent-gtd, personal-kb, aws-docs MCP for the agent user
  ([`db70ca8`](https://github.com/repos/agent-gtd-dispatch/commit/db70ca86009260d564e0788646209f3867d531ce))

- **c80859f8**: Claude-code engine explicitly dispatches to --model opus
  ([`a47e579`](https://github.com/repos/agent-gtd-dispatch/commit/a47e5792bb5368a844085ddb07b2b80ec0122f5f))

- **dispatch**: Plan-mode prompt — architectural awareness pre-grooming phase
  ([`87ab94a`](https://github.com/repos/agent-gtd-dispatch/commit/87ab94a52ad8d462f8ca7b52a926f71cad72d83a))

- **dispatch**: Promote DispatchMode to shared protocol package
  ([`1045d53`](https://github.com/repos/agent-gtd-dispatch/commit/1045d535964d1d66968a6afa21c7b55c5734d2c1))

- **planner**: Add cycle detection to rollout DAG
  ([`240a780`](https://github.com/repos/agent-gtd-dispatch/commit/240a78019335f4b0fbefb1a9b3df9e260c29b38a))

- **setup**: Provision team-kb and KB Anthropic key from dispatch-svc .env
  ([`ef5c0fb`](https://github.com/repos/agent-gtd-dispatch/commit/ef5c0fbe9c0cbdb59e757f198ad4ea05f63766df))


## v1.10.0 (2026-05-21)

### Bug Fixes

- **0232d8b9**: Installer round-2 gaps — fresh-box install on r7-research
  ([`ccbdaa2`](https://github.com/repos/agent-gtd-dispatch/commit/ccbdaa263fbdfcdfbf6a4a27128fd6e232d8285a))

- **5c2ce573**: Retry deploy.sh health probe for up to 30s
  ([`2047763`](https://github.com/repos/agent-gtd-dispatch/commit/2047763dd0d2c3fc445e38eb402ef313bf30658f))

- **70081a9d**: Installer gaps discovered during pironman01 migration
  ([`0f73808`](https://github.com/repos/agent-gtd-dispatch/commit/0f7380800b86c89cd044165c7d8bd89636727e2c))

- **952ef40b**: URGENT: Manager timeout uses build-mode 90min limit instead of intended 4hr
  MANAGE_TIMEOUT_SECONDS
  ([`bd32db1`](https://github.com/repos/agent-gtd-dispatch/commit/bd32db18149e42c7654ffeed584add213068c37c))

- **dispatch**: Accept item_id=null when mode=manage
  ([`6bc1bfc`](https://github.com/repos/agent-gtd-dispatch/commit/6bc1bfc12ee4eb39f24ec9f725fc53428905e117))

- **ollama**: Use --model CLI flag and Anthropic-native root URL
  ([`ee8435a`](https://github.com/repos/agent-gtd-dispatch/commit/ee8435a5d02f5d9efe4718c459939857d823b914))

### Chores

- **deploy**: Deploy to all 3 hosts by default
  ([`5c717af`](https://github.com/repos/agent-gtd-dispatch/commit/5c717af327315c5688035915c5f15a3f43644e88))

- **format**: Apply ruff-format to dispatch.py
  ([`a24df05`](https://github.com/repos/agent-gtd-dispatch/commit/a24df05668ad1cb9ca2743d3a356057e39e9cfd3))

- **lint**: Add S108 noqa to test dummy /tmp paths
  ([`bf14cd5`](https://github.com/repos/agent-gtd-dispatch/commit/bf14cd5f27cd7646f77a9f17c0b9ad0ed5da5bae))

- **ollama**: Bump default model qwen3.5:35b -> qwen3.6:35b
  ([`eb28094`](https://github.com/repos/agent-gtd-dispatch/commit/eb280945d811154c92eb825d4fbf1b30c06bc3a1))

### Documentation

- Generalize SSH-key-authorize halt message in installer
  ([`8119341`](https://github.com/repos/agent-gtd-dispatch/commit/8119341214e3447e50f12fb8cd5d65648c8b57b6))

- **manage-prompt**: Consume complete_in_wave's graph_complete + drop separate complete_item
  ([`138107f`](https://github.com/repos/agent-gtd-dispatch/commit/138107ffeeeeca78e1d40b86b9dc5a9918c7f97f))

- **manage-prompt**: Findings 6/7/12 from lead-as-manager walkthrough
  ([`2cda236`](https://github.com/repos/agent-gtd-dispatch/commit/2cda236ae1e2908482c7059393ad51351cf5b896))

### Features

- Add update_wave_state calls to manage prompt
  ([`2e6b97f`](https://github.com/repos/agent-gtd-dispatch/commit/2e6b97f9387b68639aef4bf1546daa05640a0201))

- Extend /info with capacity + per-host engines/agents
  ([`a2fc32c`](https://github.com/repos/agent-gtd-dispatch/commit/a2fc32c39849b52fe50e8cb1281cbd158e97c745))

- **00e4960e**: Claude-code-ollama dispatch engine (Claude Code with local Ollama backend)
  ([`fd4c4a4`](https://github.com/repos/agent-gtd-dispatch/commit/fd4c4a4ef5b4336a56801bfea56acf6390871849))

- **130b8e1f**: Investigate: dispatch caps at 6 concurrent despite 9 set in UI
  ([`66200a2`](https://github.com/repos/agent-gtd-dispatch/commit/66200a22c81e12dd32975712b2c7b30227fd62a7))

- **1cd581d1**: Plan-mode agent applies engine-selection rubric to set build_engine
  ([`88ead51`](https://github.com/repos/agent-gtd-dispatch/commit/88ead5187551d5780341ca753e77cd17b30af748))

- **31c07ef3**: Remove duplicate `_MAX_MANAGE_RETRIES_FOR_PROMPT` constant — import from main.py
  instead
  ([`1d97b2a`](https://github.com/repos/agent-gtd-dispatch/commit/1d97b2abd89b9f2a2444c2f9b0c297e1ab4ef011))

- **43685c5d**: Log each orphaned run ID individually on reconcile
  ([`e740c8c`](https://github.com/repos/agent-gtd-dispatch/commit/e740c8ce02cccb718023575ee23f4125062efc5d))

- **4ff92fd0**: Manage prompt should delete feature branches from origin after squash-merge
  ([`8729645`](https://github.com/repos/agent-gtd-dispatch/commit/87296453da30bfeb39300b81fbd4e625fbf52f83))

- **69e7c83e**: Enforce max_concurrent_runs at POST /dispatch (503 at cap)
  ([`f797d3a`](https://github.com/repos/agent-gtd-dispatch/commit/f797d3af4c029cbdfeee1ad585978fb97be25686))

- **7042818c**: URGENT: Manage prompt must forbid lowering coverage threshold (ratchet up only)
  ([`ba85138`](https://github.com/repos/agent-gtd-dispatch/commit/ba85138cb61992ecc0f35e13ec3dfdf6c1026f09))

- **77137fb4**: Extract shared dispatch wire-contract schemas into protocol package
  ([`6b2cda9`](https://github.com/repos/agent-gtd-dispatch/commit/6b2cda940fb5c0ac7a310e6cd21c57dabef1778a))

- **865b0e4e**: Engine name mismatch: agent-gtd `claude-code` vs dispatch service `claude`
  ([`d83da64`](https://github.com/repos/agent-gtd-dispatch/commit/d83da64afbaafa7bee7022f90060bf754ce1f8ef))

- **8939136f**: Tighten dispatch-service HTTP boundary
  ([`0bb792c`](https://github.com/repos/agent-gtd-dispatch/commit/0bb792c8c2f404936f5ca1342722ef200c4a5948))

- **92bfa404**: Mothball Ollama in plan-mode rubric: remove from routing options
  ([`c4d46a8`](https://github.com/repos/agent-gtd-dispatch/commit/c4d46a80912f91f8c185fbf8fe5bbb3ab814f2ee))

- **931eef2f**: Deduplicate make_branch_name / branch_name_for_item into the shared protocol package
  ([`c12a1a2`](https://github.com/repos/agent-gtd-dispatch/commit/c12a1a2f219598e34e3d3404127bc29d829a9404))

- **998544ac**: Two-user split: agent subprocesses run as `dispatch`, service runs as `dispatch-svc`
  (code side)
  ([`0d25617`](https://github.com/repos/agent-gtd-dispatch/commit/0d2561797df7e9919193d56f51abaae8ecfbfe53))

- **a9fc6d4b**: Cross-service cancel propagation dispatch endpoint
  ([`d466796`](https://github.com/repos/agent-gtd-dispatch/commit/d4667969782842366cd6735b15770e2b39d4b194))

- **af2edd2d**: Idempotent setup-dispatch-host.sh installer + in-repo deploy.sh
  ([`7b8a50b`](https://github.com/repos/agent-gtd-dispatch/commit/7b8a50b808f23efc254dbe7b4d85f79e6cb7453d))

- **bcb51580**: Plan-mode prompt writes structured fields via update_item
  ([`b3ef860`](https://github.com/repos/agent-gtd-dispatch/commit/b3ef8605d9a704c901c1d8c84bd536f643e48b92))

- **c1a3e167**: Build agent reported 'success' on de0faf2a without pushing any commits
  (verified-but-not-committed)
  ([`c8b4b28`](https://github.com/repos/agent-gtd-dispatch/commit/c8b4b2899fe9857757188c0c91477a75fc121a9e))

- **c6963f04**: Build prompt: minimal — system prompt + fetch GTD item, no pre-rendering
  ([`9fc6dfd`](https://github.com/repos/agent-gtd-dispatch/commit/9fc6dfd7b0d174abd44d4754ced7a8273e19f78f))

- **dispatch**: Auto-recovery of dead manage subprocess + recovery prompt addendum
  ([`3489dc5`](https://github.com/repos/agent-gtd-dispatch/commit/3489dc5d918b0c5ca7581b1b276dca76e9fbc1bf))

- **dispatch**: Capture subprocess transcript on every run
  ([`a4c568d`](https://github.com/repos/agent-gtd-dispatch/commit/a4c568d5698d82127e773da9c1393c266e3fd46f))

- **e261e681**: Claude-code-sonnet + claude-code-haiku engines + 4-way rubric
  ([`d6ab4fb`](https://github.com/repos/agent-gtd-dispatch/commit/d6ab4fb65ac330aa50413f9603578daf35f83da3))

- **ee444a91**: Validate OLLAMA_BASE_URL at config.load() — fail fast on missing scheme/port
  ([`e3288be`](https://github.com/repos/agent-gtd-dispatch/commit/e3288be79b5d3179fc90c67c9bf4315a69fa483f))

- **fc39976a**: Replace planner's free-text dependency hints with structured signals
  ([`c78825e`](https://github.com/repos/agent-gtd-dispatch/commit/c78825e5cee2ae1dba312551af74295d8b5930b9))

- **fe065a45**: Make dispatch engine fallback/swap visible to operator
  ([`6ba576a`](https://github.com/repos/agent-gtd-dispatch/commit/6ba576a4f9213efcb475d1faab81cda05d773cfd))

- **observability**: Wire attribution field through dispatch service env
  ([`38f9364`](https://github.com/repos/agent-gtd-dispatch/commit/38f936416316d4a2bcdb22705360584c419e1928))

- **transcripts**: Stream agent stdout to transcript.txt in real time + GET /runs/{id}/transcript
  ([`1c937d2`](https://github.com/repos/agent-gtd-dispatch/commit/1c937d2a83dec74b6a1cd8b3787b992554333184))

- **wave-manager**: Strip deterministic scaffolding, let manage agent reason
  ([`55792ba`](https://github.com/repos/agent-gtd-dispatch/commit/55792ba0d02e5a8f1bb987a964c0785a57a3b054))

### Refactoring

- **rollout**: Rename wave → rollout in agent-gtd-dispatch to match agent_gtd
  ([`0b34591`](https://github.com/repos/agent-gtd-dispatch/commit/0b3459136f3bd94d2a07e37694308251a04981d5))

- **wave-manager**: Flip allowlist → halt-list (default-allow + escape hatch)
  ([`d218842`](https://github.com/repos/agent-gtd-dispatch/commit/d21884289914292ec0347407326bae7c5567c21e))


## v1.9.0 (2026-05-12)

### Bug Fixes

- **dispatch**: --allowedTools must precede --print on claude command
  ([`a18f356`](https://github.com/repos/agent-gtd-dispatch/commit/a18f356c9de39424e7a52f1eb683f73920c3fad6))

- **engines**: Never leak ANTHROPIC_API_KEY to Claude Code subprocesses
  ([`b7cae4b`](https://github.com/repos/agent-gtd-dispatch/commit/b7cae4b9663405420058327528e6bde697d50574))

- **gtd-client**: Align wave-run paths with agent_gtd's actual routes
  ([`c9d65db`](https://github.com/repos/agent-gtd-dispatch/commit/c9d65db9dd2b3297d8300fdf6d5eae3a339f795d))

- **wave-manager**: Executor prompt — ignore launch item_id, pass wave_run_id
  ([`706dd72`](https://github.com/repos/agent-gtd-dispatch/commit/706dd7206cdf2d23d74f68c7bc07fd4d735b215b))

### Features

- **wave-manager**: Add allowlist YAML + comment classifier
  ([`0d0a387`](https://github.com/repos/agent-gtd-dispatch/commit/0d0a3875c42b63dd9b335876596243018e32d104))

- **wave-manager**: Add mode=manage route + executor scaffold
  ([`525f432`](https://github.com/repos/agent-gtd-dispatch/commit/525f432290c5600c3d0cc2bbeaf81b0c7abd78c2))

- **wave-manager**: Executor loop + squash_merge helper + branch_name nullable
  ([`23db1ae`](https://github.com/repos/agent-gtd-dispatch/commit/23db1ae0b8da445653e7e564f4be2a7420960010))

- **wave-manager**: Heartbeat prompt addendum + pre-merge CI gate
  ([`47dbfc9`](https://github.com/repos/agent-gtd-dispatch/commit/47dbfc9b9ba2c34103174237a9147190493d9b5a))

- **wave-manager**: Planner subroutine — POST /plan with anthropic SDK
  ([`dac56f4`](https://github.com/repos/agent-gtd-dispatch/commit/dac56f43202014e0e147ec4ab3738f4ea034dde6))


## v1.8.0 (2026-05-06)

### Chores

- Gitignore .kiro/ workspace state
  ([`aaf12e5`](https://github.com/repos/agent-gtd-dispatch/commit/aaf12e5758008df54f2efb7769c3a952742c8bc4))

### Documentation

- Add "Steering your Tech Lead Agent" section to README
  ([`42239d1`](https://github.com/repos/agent-gtd-dispatch/commit/42239d10a5eaad54399361ce75918f8393af1a02))

### Features

- Honour per-run timeout_minutes from DispatchRequest
  ([`fce1eec`](https://github.com/repos/agent-gtd-dispatch/commit/fce1eec652e8c438e6d7eb73f436dcab5c745908))

- Stage item attachments into {run_id}-attachments/ and surface them in the system prompt
  ([`fcf49d6`](https://github.com/repos/agent-gtd-dispatch/commit/fcf49d658090345893bac612b11f46122e197e82))

- Write system_prompt.md to workspace for kiro engine
  ([`010aa78`](https://github.com/repos/agent-gtd-dispatch/commit/010aa780053d2794672138b857732e711d4e958e))


## v1.7.1 (2026-04-22)

### Bug Fixes

- Attribute dispatch comments to the actual engine
  ([`46793ab`](https://github.com/repos/agent-gtd-dispatch/commit/46793ab4f99daf53525b43cd85a066c194e6e2d0))


## v1.7.0 (2026-04-22)

### Chores

- Decouple release from deploy
  ([`b5821d1`](https://github.com/repos/agent-gtd-dispatch/commit/b5821d1b262d0e6306badf1c5aa4bfb7f64afd45))

- Gitignore .claude/ session state
  ([`8228a19`](https://github.com/repos/agent-gtd-dispatch/commit/8228a194d963d78bc5488febe28b02afc7e21cf6))

### Documentation

- Refresh CLAUDE.md after release decoupling and multi-engine refactor
  ([`e0c5072`](https://github.com/repos/agent-gtd-dispatch/commit/e0c5072263d8591e449c5cbca9b24f07fe842867))

### Features

- Advertise engine identity and agent list via /info and /agents
  ([`c8a24c9`](https://github.com/repos/agent-gtd-dispatch/commit/c8a24c9a8a77dffd2a5ed3e804972210819f1c56))

### Testing

- Add coverage for build_system_prompt and cleanup_workspace
  ([`844d66d`](https://github.com/repos/agent-gtd-dispatch/commit/844d66dfba9b5e13a0c279df632fc7e7bc81d37a))

- Add coverage for gtd_client.py HTTP client functions
  ([`88295fa`](https://github.com/repos/agent-gtd-dispatch/commit/88295faf7e45332987e06c1c825057d3d279f25e))

- Add coverage for prepare_workspace
  ([`57642cd`](https://github.com/repos/agent-gtd-dispatch/commit/57642cdcad133e4613e047410712884ddddc7c07))

- Add coverage for run_agent async subprocess
  ([`e641693`](https://github.com/repos/agent-gtd-dispatch/commit/e6416930ea47c3b1a672e40e4b9fc1191a476521))


## v1.6.0 (2026-04-16)

### Chores

- Gitignore .coverage file
  ([`4b2a49d`](https://github.com/repos/agent-gtd-dispatch/commit/4b2a49d3558a4120729bb0fdcb605f3d07e25eed))

### Features

- Reconcile orphaned runs on startup
  ([`2a64eb7`](https://github.com/repos/agent-gtd-dispatch/commit/2a64eb7930c26987d3a0d55eac51ae4633490be6))


## v1.5.0 (2026-04-16)

### Chores

- Bump default max_turns from 50/20 to 100
  ([`ccc4057`](https://github.com/repos/agent-gtd-dispatch/commit/ccc4057f7447aa9b3e13e99355bc843a0f2e7309))

### Features

- Add plan mode system prompt and mode parameter support
  ([`08e0ac0`](https://github.com/repos/agent-gtd-dispatch/commit/08e0ac0aea4b18ae5acfb00dd92af1d51501f062))


## v1.4.0 (2026-04-15)

### Features

- Add progress comment milestones to dispatch system prompt
  ([`2511538`](https://github.com/repos/agent-gtd-dispatch/commit/25115380e93c7670b801b8ee09f14aa5ad5ea7b1))


## v1.3.0 (2026-04-15)

### Features

- Update dispatch prompt to set item status to review on success
  ([`1865a71`](https://github.com/repos/agent-gtd-dispatch/commit/1865a710aed34a1678566d5db573aa63bf9532ef))


## v1.2.0 (2026-04-15)

### Features

- Multi-engine dispatch with Claude and Kiro CLI support
  ([`3c6fb0d`](https://github.com/repos/agent-gtd-dispatch/commit/3c6fb0d0263dd05b58f927794aee56a09f398451))


## v1.1.0 (2026-04-15)

### Documentation

- Add CLAUDE.md and README.md for agent bootstrapping
  ([`b2de999`](https://github.com/repos/agent-gtd-dispatch/commit/b2de9994af959413782740d0b8dd8f1455434a74))

### Features

- Per-run workspace isolation with branch checkout and cleanup
  ([`aeb598c`](https://github.com/repos/agent-gtd-dispatch/commit/aeb598c8889915a39029596293ee344da0ea0840))


## v1.0.0 (2026-04-15)

- Initial Release
