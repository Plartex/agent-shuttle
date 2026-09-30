# Antigravity incident trace — 2026-09-20

## Outcome

Antigravity is operational through Agent Bridge. The failure was caused by a
headless tool permission denial that the bridge incorrectly treated as a
successful empty response. A later verification attempt also exposed an
expired OAuth session and a sandboxed server process that could not access the
Windows credential store.

## Failure trace

All times are Europe/Moscow on 2026-09-20.

1. `11:11:11` — `agy` starts in print mode with the Agent Bridge workspace.
2. `11:11:11` — silent authentication loads a valid keyring token.
3. `11:11:17` — Antigravity creates conversation
   `38bc75d2-cdde-48d7-be7d-392228ca7991` and sends the prompt.
4. `11:11:19` and `11:11:21` — the Google model endpoint returns response IDs.
5. `11:11:21` — headless mode soft-denies `RunCommand` because it cannot show
   an interactive approval prompt.
6. `11:15:53` — a second request again authenticates successfully and creates
   conversation `efe5b160-121c-4a53-a42e-34d35bcacefb`.
7. `11:16:01` and `11:16:12` — the model endpoint responds.
8. `11:16:14` — headless mode soft-denies `ViewFile`.
9. `agy` exits with code `0`, JSON status `SUCCESS`, and an empty `response`.
10. The old `AntigravityCliBackend` checked only exit code and status, returned
    the empty string, and Agent Bridge exposed the request as completed.

Relevant local CLI logs:

- `C:\Users\kkaas\.gemini\antigravity-cli\log\cli-20260920_111110.log`
- `C:\Users\kkaas\.gemini\antigravity-cli\log\cli-20260920_111551.log`

## Root cause

The primary cause was the combination of two behaviors:

- Antigravity headless mode soft-denies tools that require interactive approval,
  continues the run, and may exit successfully.
- Agent Bridge considered `status == "SUCCESS"` sufficient even when the final
  response was empty.

This was not an authentication or model-routing failure in the original calls:
the logs show successful keyring authentication, conversation creation, and
model responses before the permission denial.

## Secondary findings during repair

- The cached OAuth token later needed an interactive refresh. An interactive
  `agy` session refreshed the sign-in.
- A bridge instance started inside the Codex execution sandbox could not access
  Windows Credential Manager or write Antigravity's MCP cache. The Antigravity
  server was therefore restarted as a normal user process.
- The shared Antigravity configuration contains historical malformed multiline
  `command(...)` grants. They create warning noise but did not cause this
  incident.

## Changes

- Added a scoped `read_file(...)` allow rule for this Agent Bridge workspace to
  `C:\Users\kkaas\.gemini\antigravity-cli\settings.json`.
- Kept command execution in request-review mode; no global wildcard or
  `--dangerously-skip-permissions` was enabled.
- Hardened `AntigravityCliBackend` so invalid JSON and empty `SUCCESS` responses
  become explicit failures with the CLI diagnostic attached.
- Added regression tests for normal responses, empty headless soft-denials, and
  malformed output.

## Verification

1. Python regression suite: `13/13 OK`.
2. A2A file-tool smoke test:
   - route: Agent Bridge `:8766` → `agy` → Antigravity → `view_file`;
   - result: `TASK_STATE_COMPLETED`;
   - response: `codex-antigravity-a2a-bridge` read from `pyproject.toml`.
3. Evaluation-framework smoke test:
   - target: Python snippet `def add(a, b): return a + b`;
   - provider: `agent-bridge:antigravity`;
   - rule: `long_method`;
   - result: `1/1 passed`, `quality_score: 100.0`, `errors: 0`;
   - run ID: `a25a4623-0634-4266-a56e-c17ed56f1d97`.

## References

- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)
- [Antigravity permissions](https://www.antigravity.google/docs/permissions?tab=cli)
- [Antigravity CLI troubleshooting](https://antigravity.google/docs/cli/troubleshooting/)
