# Troubleshooting and Diagnostics

[Russian version / Русская версия](ru/troubleshooting.md)

This guide covers common operational issues, diagnostic procedures, error messages, and debugging strategies across all supported agent harnesses in Agent Shuttle.

---

## Diagnosing Antigravity CLI vs. Bridge

When an Antigravity task fails or appears to hang, it is crucial to determine whether the issue lies in Agent Shuttle or the upstream `agy` CLI.

### 1. Test Bridge Liveness First (`/bridge/identity`)

The `/bridge/identity` endpoint responds **without** spawning `agy` or querying Google model endpoints.

```powershell
Invoke-RestMethod http://127.0.0.1:8766/bridge/identity
```

- **If `/bridge/identity` responds immediately:**  
  The Bridge HTTP server and process manager are functioning correctly. Any delay or failure in an ongoing `ask()` request is occurring inside the upstream `agy` CLI process, network communication with Google's servers, or tool execution.
- **If `/bridge/identity` returns 404:**  
  The running server is an older Bridge version. Stop the process and restart it with the current release.
- **If `/bridge/identity` times out or fails to connect:**  
  A persistent Bridge process may be stopped, blocked by a local firewall, or hung. Inspect the terminal where you ran `agent-shuttle serve antigravity --workspace . --port 8766`. If you use MCP without a persistent server, no listener is expected between requests.

---

### Authentication belongs to the CLI process, not the desktop app

`AntigravityAuthenticationError` means `agy` could not use its account in the context where Bridge launched it. If the diagnostic mentions `Access is denied` under `.gemini/antigravity-cli`, a restricted or sandboxed launcher is a likely cause; it does **not** prove that the user is signed out. Check with `agy models` in an ordinary terminal under the same Windows account. If that succeeds, run `agent-shuttle serve antigravity --workspace . --port 8766` from that terminal rather than from a sandboxed executor, and retry the task. If it also fails, start `agy` interactively and complete its sign-in flow. Do not copy credentials into the project or disable sandboxing globally.

---

### 2. Antigravity Soft Denials (`AntigravityPermissionDenied`)

**Symptom:**  
The task fails with `AntigravityPermissionDenied: agy denied a required tool (...); the result may be incomplete.`

**Root Cause:**  
In headless mode (`-p`), the Antigravity CLI cannot prompt the user interactively with approval dialogs. If an action is governed by an `ask` or `deny` rule in your settings, `agy` denies the tool execution. Upstream `agy` frequently exits with return code `0` and reports `status: "SUCCESS"` even though the tool was blocked and the output is truncated or empty.

**Resolution:**
1. Check your user settings in `~/.gemini/antigravity-cli/settings.json`.
2. Remember rule precedence: `deny` > `ask` > `allow`.
3. If using headless mode, explicitly allow the required tools in `permissions.allow` (e.g. `read_file`, `list_dir`).
4. If you intentionally require full access, restart the server with `--agy-dangerously-skip-permissions` or use `HarnessLaunch(tool_policy="full_access")`.

---

### 3. Antigravity Stalls & Timeouts

**Symptom:**  
A task fails after 300 seconds with `TimeoutError: agy did not return a result within 300s`.

**Root Cause:**  
By default, the upstream `agy` CLI has an unbounded print timeout (`0s`), which can lead to indefinite hangs if a command stalls or network connectivity is interrupted. Agent Shuttle applies an explicit 300-second deadline (`--print-timeout 300s`) and kills unresponsive child processes.

**Resolution:**
- For long-running operations, increase the timeout:
  - CLI: `agent-shuttle serve antigravity --port 8766 --agy-turn-timeout-seconds 600`
  - Python: `HarnessLaunch(name="antigravity", url="...", workspace=..., agy_turn_timeout_seconds=600)`
- Persistent stream sessions have an independent 30-minute turn deadline.

---

### 4. Preflight Network / TLS Retries

**Symptom:**  
Diagnostic logs show preflight retry warnings: `eligibility check failed ... tls handshake timeout`.

**Root Cause:**  
During authentication initialization, `agy` occasionally experiences temporary TLS handshake timeouts when fetching user profile metadata before the agent turn begins.

**Resolution:**  
Agent Shuttle detects this specific retryable error pattern and automatically retries the invocation up to 3 times with exponential backoff before reporting a failure. If failures persist, verify your internet connection and run `agy` interactively to refresh its sign-in if needed.

---

### 5. Concurrent Metadata Query Contention

**Symptom:**  
Simultaneous calls to `/bridge/info` or `/bridge/capabilities` hang or fail.

**Root Cause:**  
Upstream `agy` uses shared local session locks. Running multiple concurrent CLI commands (`models`, `/model`, `/effort`, `/usage`) simultaneously can cause CLI lock contention.

**Resolution:**  
Agent Shuttle serializes all Antigravity metadata queries under an internal `asyncio.Lock` with a 45-second per-command timeout. Do not bypass Bridge to invoke multiple headless CLI processes concurrently in the same workspace.

---

## Diagnosing OpenCode

### 1. Windows `.cmd` Wrapper Shim Issues

**Symptom:**  
`RuntimeError: OpenCode .cmd launcher cannot be supervised; set runtime_command to opencode.exe`

**Root Cause:**  
On Windows, npm installs `.cmd` batch wrappers. Batch wrappers cannot receive clean process termination signals and fail process tree tracking.

**Resolution:**  
Agent Shuttle automatically checks whether the command resolves to `.cmd` and attempts to locate the real binary at `<npm_prefix>\node_modules\opencode-ai\bin\opencode.exe`. If you encounter this error, specify `runtime_command` in your profile JSON pointing directly to `opencode.exe`.

### 2. OpenCode Daemon Startup Timeout

**Symptom:**  
`RuntimeError: OpenCode server did not become healthy within 20 seconds`

**Root Cause:**  
The managed OpenCode background daemon (`--pure serve`) failed to start or bind to its assigned loopback port.

**Resolution:**  
Inspect the daemon stderr output printed in the exception. Ensure the workspace path exists and that required model provider credentials (or the local Ollama daemon) are accessible.

---

## Diagnosing Claude Code

### 1. Credential Redaction in Stderr

**Behavior:**  
If a Claude Code turn fails, Agent Shuttle inspects the process stderr and replaces detected tokens (such as `ANTHROPIC_AUTH_TOKEN` or `credential_env`) with `[REDACTED]` before raising `RuntimeError`.

### 2. Ollama Compatibility

**Symptom:**  
Claude Code fails to connect to local Ollama.

**Resolution:**  
Ensure that your profile specifies:
- `"provider": "ollama"`
- `"endpoint": "http://127.0.0.1:11434"`
Agent Shuttle sets `ANTHROPIC_BASE_URL` to the loopback URL and `ANTHROPIC_AUTH_TOKEN="ollama"`. Remote non-loopback endpoints for Ollama are rejected for security reasons.

---

## Diagnosing Codex

### 1. Missing `CODEX_HOME` on Windows

**Symptom:**  
Codex fails to authenticate or find local configuration in non-interactive shells.

**Resolution:**  
Agent Shuttle automatically checks and defaults `CODEX_HOME` to `~/.codex` if not set. If your configuration resides in a custom directory, set `$env:CODEX_HOME = 'C:\path\to\.codex'`.

### 2. `no_tools` Policy Rejection

**Symptom:**  
`ValueError: Codex cannot enforce no_tools`

**Root Cause:**  
Codex App Server requires an active sandbox environment (`read_only`, `workspace_write`, or `full_access`). Requesting `no_tools` is not supported by Codex. Use `read_only` instead if you wish to prevent file modifications.

---

## Process Cleanup and Zombie Processes

On Windows, child processes spawned by venvs or shell wrappers may remain orphaned if an application crashes.

- **Automated Process Cleanup:**  
  When using `connect_harness`, Agent Shuttle terminates the entire process tree using `taskkill /PID <pid> /T /F` on Windows before removing temporary directories.
- **Persistent Servers:**
  Stop a foreground `agent-shuttle serve` process with Ctrl+C. If it was launched by another process manager, stop it through that manager after confirming its identity.
