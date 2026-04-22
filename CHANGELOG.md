# CHANGELOG

<!-- version list -->

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
