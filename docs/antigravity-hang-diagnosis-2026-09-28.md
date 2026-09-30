# Antigravity hang diagnosis — 2026-09-28

## What was actually unresolved

The 2026-09-27 full-permission smoke eventually succeeded, but two earlier
attempts had stalled. A successful retry alone did not explain or prevent
those stalls.

## Evidence

- The first A2A smoke stopped before sending a task. Its temporary Bridge
  server started, but `connect_harness()` awaited `/bridge/capabilities`.
  That endpoint launched three concurrent `agy` processes (`models`, `/model`,
  `/effort`) even though connection verification needed only backend,
  workspace, and permission mode. The log showed overlapping authentication
  activity; no A2A task reached the model. We cannot prove which upstream CLI
  operation stalled internally.
- The first direct backend call included `--dangerously-skip-permissions`.
  The CLI acknowledged auto-approval and authenticated, but did not create a
  conversation, emit final JSON, or write the requested marker in 300 seconds.
  The local `agy --help` reports the default `--print-timeout` as `0s`, so the
  original one-shot adapter had no CLI deadline. The exact upstream stall
  after authentication remains unconfirmed.
- A later direct CLI stream showed `permission_mode: always-proceed`, a
  completed `run_command` step, and a matching marker. A later Bridge A2A run
  also completed. Thus full-access permission itself was not the cause of the
  stalls.

## TDD repair

Regression tests first reproduced the static-readiness endpoint missing, the
parallel metadata invocations (including two concurrent HTTP callers), and the
unbounded child-process wait. The
repair adds `/bridge/identity` for static verification, uses it from
`connect_harness()`, serializes metadata commands with a per-process 45-second
deadline, and gives one-shot `agy` an explicit, configurable 300-second turn
deadline. A timed-out child is killed and the caller gets `TimeoutError`.
The checker uses persistent stream sessions, so a separate regression test
also verifies that a session turn stalled after input is killed at its
30-minute Bridge deadline, rather than relying only on `agy --print-timeout`.
Existing servers without `/bridge/identity` must be restarted with the new
Bridge version rather than silently reused.

## Verification

- Offline suite: 129 tests passed, 5 opt-in live tests skipped.
- Live A2A full-access smoke after repair: temporary Bridge startup took
  **2.578 seconds**; task finished `TASK_STATE_COMPLETED`; marker matched;
  usage was 28,154 total tokens. The run is under
  `.runtime/agy-full-access-9f46c674b0f3/`.
- Real sequential metadata query completed in 25.7 seconds and returned 14
  models, selected model `gemini-3.8-flash-high`, and selected effort `high`.
- Live full-access persistent stream-session smoke after repair: completed
  in 21.5 seconds, returned `OK`, wrote the matching marker, and reported
  27,766 total tokens. The run is under
  `.runtime/agy-full-access-stream-0c64ebe9358e/`.

The repair removes an unnecessary model-info dependency from connection
startup and bounds future stalls. It does **not** claim to fix the unknown
upstream reason the CLI once stopped progressing after authentication.
