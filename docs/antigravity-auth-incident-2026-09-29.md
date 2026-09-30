# Antigravity CLI authentication incident — 2026-09-29

## Observed failure

1. The MCP call initially could not reach `127.0.0.1:8766`: there was no running Antigravity Bridge server.
2. A temporary server started from a restricted Codex command process answered `/bridge/identity`, but its `agy` child failed before running a model turn. The CLI reported `You are not logged into Antigravity`, `Access is denied` while writing its `.gemini/antigravity-cli/mcp` cache, and `Print mode: auth timed out`.
3. Running the same installed `agy.exe models` as the normal Windows user, outside that sandbox, succeeded and listed 14 models. The account was therefore available to the CLI in the normal user context; the desktop application's sign-in was not the relevant proof.
4. The normal-user Bridge then completed both an MCP one-shot task (`РАБОТАЕТ`) and a persistent A2A session turn (`OK`). Each reported roughly 13.4k total tokens.

## Additional lifecycle fault

`Start-Bridge.ps1` saved the PID returned by the virtual-environment Python launcher. On this installation that launcher exited while the actual Python server continued as a child. For one observed start, the saved launcher PIDs were `15000` and `40240`, while the port owners were `30196` and `10756`. This left stale PID files and made `Stop-Bridge.ps1` unable to stop the servers it had started.

## Repair and verification

- Regression tests first reproduced the generic authentication error and the missing actual server PID in `/bridge/identity`.
- Bridge now raises `AntigravityAuthenticationError` with distinct guidance for an inaccessible credential/configuration store versus a CLI that may really be signed out. The one-shot, streaming-session, and metadata paths use this diagnostic; streaming waits for stderr before classifying a process that exited immediately.
- `/bridge/identity` reports the actual server PID. The PowerShell start script waits for the endpoint, verifies the server identity, workspace, port owner, and process command, and records that PID. The stop script verifies the same facts before terminating a process.
- A normal-user start recorded PIDs matching `/bridge/identity`; a second start reused them; stop terminated both; restart produced new PIDs. A subsequent MCP Antigravity task completed after restart.
- The offline suite passed: 154 tests, 5 opt-in live tests skipped, 90% branch-inclusive coverage.

## Operational boundary

This repair cannot make a process inside an OS sandbox read the user's Windows credential store. Antigravity work that requires the signed-in CLI must be served by a Bridge process started under the normal signed-in user context. A temporary Bridge launched inside a restricted caller may still fail, but now reports the correct cause. No credentials were copied, and no global sandbox or permission policy was disabled.
