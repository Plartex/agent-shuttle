# Remaining Task scope

1. Add MCP submit/status/wait/cancel/result/transcript operations. Keep managed
   servers alive between calls; close owned work and processes on gateway exit.
2. Add bounded, paged results and task transcripts to Python and MCP. Preserve
   structured task errors and event metadata. Validate wait and execution budgets.
3. Add SQLite TaskStore with persisted request bindings. On restart, retain results
   and mark interrupted execution failed; never replay side effects automatically.
4. Bound stalled workers at the executor, close interrupted native sessions, and
   record progress/lifecycle/error events. Waiting has its own budget and does not
   cancel a submitted task.
5. Exercise the contract through real A2A/MCP transports, restart/cancel races,
   and live Codex/Antigravity tasks. Update both language API guides.

Development uses TDD. Antigravity Gemini 3.8 High owns the new SQLite store and its
tests in an isolated snapshot; the coordinator reviews and runs them before
integration. Existing delegation changes are preserved.
