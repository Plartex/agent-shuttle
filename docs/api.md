# Agent Shuttle API Reference

[Russian version / Русская версия](ru/api.md)

This document provides a comprehensive reference for the unified public Python API, HTTP endpoints, CLI commands, and Model Context Protocol (MCP) tools provided by Agent Shuttle.

---

## Python API Reference

Import public symbols directly from `agent_shuttle`:

```python
from agent_shuttle import (
    ShuttleClient,
    BridgeResult,
    BridgeEvent,
    BridgeSession,
    TaskHandle,
    HarnessLaunch,
    BridgeConnection,
    connect_harness,
    AgentProfile,
    ToolPolicy,
    build_profile,
    discover_harnesses,
    AntigravityPermissionDenied,
    AntigravityAuthenticationError,
)
```

### `ShuttleClient`

The primary client for querying Agent Shuttle and other task-capable A2A 1.x servers. `submit()` and `ask()` require the peer to return an A2A Task, not a message-only response.

```python
client = ShuttleClient(timeout_seconds: float = 1800)
```

#### Methods

- **`async def ask(peer_url: str, prompt: str, model: str | None = None, *, reasoning_effort: str | None = None, read_only: bool = False, tool_policy: str | None = None, session_id: str | None = None, request_id: str | None = None) -> BridgeResult`**
  Dispatches a text task and waits for its result. If the caller is cancelled or its overall timeout expires, it requests remote cancellation.
  - `prompt`: Non-empty text instruction.
  - `model`: Optional target model ID on the remote agent.
  - `reasoning_effort`: Optional provider-specific reasoning effort (e.g. `low`, `medium`, `high`, `none`).
  - `read_only`: Backward-compatible boolean flag for read-only tools.
  - `tool_policy`: One of `"no_tools"`, `"read_only"`, `"workspace_write"`, or `"full_access"`.
  - `session_id`: Optional UUID string identifying a persistent conversation.
  - `request_id`: Optional UUID for deduplicating retries within one server lifetime. Reusing it with different arguments is an error.
  - Returns: `BridgeResult`.

- **`async def submit(..., request_id: str | None = None) -> TaskHandle`**
  Accepts the same task options as `ask()` and returns as soon as A2A creates the task. Unlike `ask()`, stopping the caller does not cancel the submitted task.

- **`task(peer_url: str, task_id: str) -> TaskHandle`**
  Reopens a task from its ID while the same server instance is running.

- **`task_status(peer_url, task_id)` / `cancel_task(peer_url, task_id)`**
  Fetch the current A2A task or request its cancellation.

- **`def session(peer_url: str, model: str | None = None, *, reasoning_effort: str | None = None, read_only: bool = False, tool_policy: str | None = None) -> BridgeSession`**  
  Constructs a reusable, stateful `BridgeSession` bound to `peer_url` with pinned settings. Recommended usage is with `async with`.

- **`async def info(peer_url: str) -> dict`**  
  Returns a comprehensive snapshot including capabilities, model catalog, reasoning efforts, and live account quota buckets.

- **`async def identity(peer_url: str) -> dict`**  
  Lightweight check returning `agent`, `backend`, the actual server `pid`, `workspace`, `read_only_tools`, `max_tool_policy`, `agy_permission_mode`, and `agy_turn_timeout_seconds`. Does **not** trigger expensive model or CLI queries.

- **`async def capabilities(peer_url: str) -> dict`**  
  Returns model list, selected model, effort options, and tool policy limits.

- **`async def usage(peer_url: str) -> dict`**  
  Returns quota usage groups, remaining percentage, and reset timestamps.

- **`async def close_session(peer_url: str, session_id: str) -> bool`**  
  Explicitly closes an active session on the peer server.

---

### `TaskHandle` and `BridgeEvent`

```python
handle = await client.submit(url, "Inspect this project", request_id=my_uuid)
print(handle.task_id)
snapshot = await handle.wait(timeout=30)  # May still be WORKING; does not cancel.
if snapshot.state == "TASK_STATE_WORKING":
    handle = client.task(url, handle.task_id)  # Reopen from another client instance.
    result = await handle.result()

# To stop an active task explicitly:
# cancelled = await handle.cancel()
```

`status()` returns a `BridgeResult` snapshot. `wait(timeout)` returns the latest snapshot when the wait budget expires; `result()` waits until a terminal or input-required state. `events()` yields live `BridgeEvent(kind, task_id, state, text)` updates; after a stream disconnect, call `status()` to recover the authoritative state. Repeated `cancel()` calls on a cancelled task return its cancelled status. A task store is currently **in memory**: IDs and deduplication bindings do not survive a server restart. Cancellation of a persistent turn closes that native session; create a new session rather than silently continuing a potentially inconsistent one.

---

### `BridgeResult`

A dataclass representing the outcome of an A2A task:

```python
@dataclass(frozen=True)
class BridgeResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    usage: dict[str, int] | None = None
    details: dict | None = None
```

- `state`: Task state name (e.g. `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `message`).
- `text`: Extracted plain-text response from the agent.
- `usage`: Dictionary with normalized token counters (`input_tokens`, `output_tokens`, `total_tokens`, `thinking_tokens`, `cache_read_tokens`, `cache_write_tokens`).
- `details`: Optional provider metadata (such as cache status or preflight retry counts).

---

### `BridgeSession`

Manages a persistent conversation across multiple turns with pinned settings:

```python
async with client.session(url, model="gpt-5.6-terra") as session:
    res1 = await session.ask("What files were modified in commit abc?")
    res2 = await session.ask("Show diff for the first file.")
```

- Settings (`model`, `reasoning_effort`, `tool_policy`) are pinned on creation and cannot be changed across turns.
- Calls are serialized via an internal `asyncio.Lock`—only one turn is processed at a time per session.
- Exiting the `async with` block calls `close()` and frees backend server resources.

---

### `HarnessLaunch` & `connect_harness`

Programmatic supervision for local Bridge peer lifecycles. It connects to an existing server if its backend, canonical workspace, and permissions match, or automatically starts a temporary supervised background instance.

```python
@dataclass(frozen=True)
class HarnessLaunch:
    name: str                           # "codex", "antigravity", "opencode", "claude_code"
    url: str                            # Loopback URL, e.g. "http://127.0.0.1:8765"
    workspace: Path                     # Canonical project directory
    model: str | None = None
    command: str | None = None          # Custom binary executable path
    profile_path: Path | None = None    # Path to JSON profile (OpenCode/Claude Code)
    ollama_url: str = "http://127.0.0.1:11434"
    log_path: Path | None = None        # Custom log destination
    start_if_missing: bool = True       # If False, requires pre-existing server
    tool_policy: str | None = None      # "no_tools", "read_only", "workspace_write", "full_access"
    agy_dangerously_skip_permissions: bool = False
    agy_turn_timeout_seconds: float = 300
```

Usage:
```python
launch = HarnessLaunch(name="antigravity", url="http://127.0.0.1:8766", workspace=Path.cwd())
async with connect_harness(launch) as connection:
    # connection.url is ready
    # connection.started is True if a temporary server was spawned
    result = await ShuttleClient().ask(connection.url, "Run lint checks")
```

On exit, temporary servers and their process trees are reliably killed (using `taskkill /PID ... /T /F` on Windows).

---

### `AgentProfile` & `ToolPolicy`

Defines server-owned profiles for OpenCode and Claude Code:

```python
class ToolPolicy(str, Enum):
    NO_TOOLS = "no_tools"
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"
```

- **`AgentProfile.from_file(path: Path, *, workspace_override: Path | None = None) -> AgentProfile`**: Loads a JSON profile.
- **`AgentProfile.from_mapping(data: dict) -> AgentProfile`**: Validates a dictionary structure.
- **`profile.resolve(model, reasoning_effort, tool_policy) -> ProfileSelection`**: Verifies that requested client options do not exceed `allowed_models` or `max_tool_policy`. Prevents privilege escalation.

---

### `discover_harnesses`

```python
def discover_harnesses(commands: dict[str, str] | None = None) -> dict[str, str]
```

Scans `PATH` and platform installation paths for installed executables (`codex`, `antigravity`, `opencode`, `claude_code`). Returns a mapping of harness name to executable path. Never launches processes or queries models.

---

### `AntigravityPermissionDenied`

Exception raised when Antigravity CLI reports `status: SUCCESS` but tool invocations were soft-denied by the user's permission settings.

### `AntigravityAuthenticationError`

Exception raised when the Antigravity CLI cannot access its account from the Bridge process. If the CLI also reports access denied for its configuration, run Bridge as the signed-in user outside the caller's sandbox.

---

## HTTP Endpoints Reference

Every Agent Shuttle A2A server exposes the following endpoints on loopback (`127.0.0.1`):

| Endpoint | Method | Description |
|---|---|---|
| `/.well-known/agent-card.json` | `GET` | A2A 1.0 JSON Agent Card describing capabilities and skills. |
| `/` | `POST` | A2A 1.0 JSON-RPC endpoint for sending tasks and message streaming. |
| `/bridge/identity` | `GET` | Lightweight JSON identity check (backend, canonical workspace, permission mode, turn timeout). Fast readiness check. |
| `/bridge/info` | `GET` | Combined capabilities, model list, reasoning efforts, and live account quotas. |
| `/bridge/capabilities` | `GET` | Model list, selected model, effort options, and tool policy ceilings. |
| `/bridge/usage` | `GET` | Account quota usage, bucket limits, and reset times. |
| `/bridge/sessions/{session_id}` | `DELETE` | Closes the specified persistent session and releases resources. |

---

## Command-Line Interface (CLI)

The CLI entry point is `agent-shuttle` (or `python -m agent_shuttle`). Legacy `BridgeClient`, `agent_bridge`, and `agent-bridge` names remain available.

### `agent-shuttle serve`

Starts an A2A server:

```powershell
# Serve Codex
agent-shuttle serve codex --port 8765 [--workspace <DIR>]

# Serve Antigravity CLI
agent-shuttle serve antigravity --port 8766 [--workspace <DIR>] `
  [--agy-command <PATH>] `
  [--agy-turn-timeout-seconds 300] `
  [--agy-dangerously-skip-permissions]

# Serve OpenCode or Claude Code from a profile
agent-shuttle serve profile --profile .\profile.json --port 8767 [--workspace <DIR>]
```

### `agent-shuttle ask`

Sends a text prompt:
```powershell
agent-shuttle ask <URL> "<PROMPT>" `
  [--model <MODEL>] `
  [--reasoning-effort <EFFORT>] `
  [--tool-policy <no_tools|read_only|workspace_write|full_access>]
```

### `agent-shuttle info`

Fetches and pretty-prints JSON capabilities and quota from `<URL>`:
```powershell
agent-shuttle info http://127.0.0.1:8765
```

### `agent-shuttle discover`

Lists locally discovered harnesses:
```powershell
agent-shuttle discover `
  [--agy-command <PATH>] `
  [--opencode-command <PATH>] `
  [--claude-command <PATH>]
```

---

## Model Context Protocol (MCP)

The MCP server runs over standard I/O:
```powershell
agent-shuttle-mcp
# or: python -m agent_shuttle.mcp_server
```

### Environment Configuration

- `BRIDGE_CODEX_URL`: Loopback URL for the Codex server (e.g. `http://127.0.0.1:8765`).
- `BRIDGE_ANTIGRAVITY_URL`: Loopback URL for the Antigravity server (e.g. `http://127.0.0.1:8766`).
- `BRIDGE_ANTIGRAVITY_WORKSPACE`: Default workspace for managed Antigravity instances.
- `BRIDGE_AGENTS_JSON`: JSON map of custom agent profile IDs to local URLs:
  ```json
  {"opencode-local": "http://127.0.0.1:8767", "claude-local": "http://127.0.0.1:8768"}
  ```

### Exposed MCP Tools

1. **`ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?)`**  
   Routes a prompt to any agent defined in `BRIDGE_AGENTS_JSON`.

2. **`get_agent_info(agent_id)`**  
   Reads models, efforts, and quotas for the profile agent.

3. **`ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`**  
   Sends a task to Antigravity. If `workspace` is passed, dynamically provisions an isolated temporary Bridge server.

4. **`ask_codex(prompt, model?, reasoning_effort?)`**  
   Sends a task to Codex with optional thread model and effort overrides.

5. **`get_antigravity_info(workspace?)`**  
   Fetches Antigravity capabilities and `/usage` quotas without burning model turns.

6. **`get_codex_info()`**  
   Fetches Codex models and rate limits via `account/rateLimits/read`.
