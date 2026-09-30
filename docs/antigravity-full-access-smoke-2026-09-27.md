# Antigravity full-permission live smoke — 2026-09-27

The user explicitly requested a live model run with full tool approval. All
prompts used an otherwise empty, newly created workspace under `.runtime/` and
asked the model to create only a nonce marker via `RunCommand`. No global
Antigravity permission rules were changed.

## Observed sequence

1. An initial A2A attempt stalled during `/bridge/capabilities`, before a task
   was sent. The CLI logs showed credential refresh activity. The run was
   stopped; the temporary server was no longer listening afterward. This does
   not establish a permission failure or a root cause for the delay.
2. An initial direct backend attempt used a longer prompt. The CLI logged that
   `--dangerously-skip-permissions` was active, but produced no final JSON or
   marker within 300 seconds. The test timed out and killed the CLI process.
3. A tool-free direct CLI probe returned `SUCCESS` and `OK`.
4. A direct `agy --output-format stream-json --dangerously-skip-permissions`
   probe reported `permission_mode: always-proceed`, a completed `run_command`
   step, `SUCCESS`, and an exact marker in `probe2.txt`.
5. The opt-in live backend test passed with the shorter prompt: final response
   `OK`, exact marker on disk, 28,098 total tokens.
6. The opt-in A2A test passed end to end: `/bridge/capabilities` reported
   `agy_permission_mode: all`, the task finished `TASK_STATE_COMPLETED`, and
   the exact marker was present. Reported usage: 28,125 total tokens.

The successful A2A run left a harmless trace under
`.runtime/agy-full-access-a06b21c6b084/`; its `bridge.log` shows successful
capabilities and A2A requests. The temporary server was verified stopped after
the test. The earlier stalls are distinct from the permission-denial bug; their
precise upstream cause remains unconfirmed.

The repeatable opt-in test is `tests/test_live_antigravity_permissions.py`.
