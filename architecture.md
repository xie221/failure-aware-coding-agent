# Architecture

```text
mini-SWE Agent model.query() ─┐
                             ├─ guarded LiteLLM completion ─ HTTPHandler
Enhanced observation feedback┘                           │
                                      HTTPX BudgetedTransport (12 attempts)
                                                         │
                                                local vLLM endpoint

Agent action → BubblewrapEnvironment → workspace-pinned wrapper
             → unshare(user + network) → Bubblewrap
             → isolated bash action (workspace is sole persistent writable bind)
```

## Request path

The runtime constructs the model through mini-SWE-agent's `get_model()` factory, injects one `HTTPHandler` before Agent construction, and installs the Stage 6F attempt adapter before any model call. The adapter rejects a missing/different client, wrong route, streaming/async request, or nonzero retry configuration. Agent and any future reflection request kind must use the same in-process `AttemptBudget` instance. Current Failure-Aware feedback itself makes no reflection request.

The attempt ledger records request kind, logical call/attempt identifiers, method, timestamp, status or exception class, and elapsed time. It does not store request body, URL, query, headers, or credentials. HTTPX trusts no ambient proxy environment and transport retries are zero.

## Tool path

Each task receives a generated wrapper with literal absolute paths for its task workspace, audit counter, mini Python environment, and mini-SWE source. It checks the exact argv contract emitted by the installed Bubblewrap Environment. It does not accept model-selected mount paths. The outer user/network namespace denies host/Internet networking to Shell actions; Bubblewrap exposes read-only runtime mounts plus the task worktree. No hidden tests, reference fixes, home directory, Docker socket, or benchmark root is mounted.

## Failure feedback

The enhanced condition wraps the Bubblewrap Environment and examines only the command and observation already returned to the Agent. It detects unavailable interactive editors, missing paths, permission/sudo attempts, pytest collection/assertion failures, repeated failed commands, and successful public tests near the request budget. Its feedback is appended to the visible observation. Control mode uses the same environment without this module.

Enhanced additionally has a deterministic `RepeatFailureGuard`. After a nonzero Shell result, it stores a hash of the normalized command and a workspace fingerprint covering paths, file bytes, and permission bits (runtime caches excluded). If the exact command is requested again while the fingerprint is unchanged, the wrapper returns a synthetic, visible “retry skipped” observation instead of invoking Bubblewrap. No code is edited and no model request is added. If workspace state changed, the same command is allowed. Symlinks/read errors make fingerprinting inconclusive and fail open. `retry_guard.json` stores only event names and hashes; task results and scores separately count skipped retries. Thus action count is auditable as wrapper invocations plus guard skips.

## Process and scoring boundaries

The launcher uses one clean subprocess per task, a 180-second process-group deadline, TERM then bounded KILL, and a process-group survivor check. Each task has a fresh workspace and its own logs/trajectory. Independent public/hidden scoring runs only after the Agent exits, in separate evaluator-only directories.
