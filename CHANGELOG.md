# CHANGELOG

<!-- version list -->

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
