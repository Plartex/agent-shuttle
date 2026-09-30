# Agent Shuttle

[![Tests](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml/badge.svg)](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![A2A Protocol 1.0](https://img.shields.io/badge/A2A-1.0_JSON--RPC-blue)](https://a2a-protocol.org/latest/)
[![MCP](https://img.shields.io/badge/MCP-tools-green)](https://modelcontextprotocol.io/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

[Russian version / Русская версия](README.ru.md)

Agent Shuttle gives Python applications one way to work with **Codex**, **Antigravity**, **OpenCode**, and **Claude Code**. It runs local agent tasks, preserves multi-turn sessions, and exposes the agents through [A2A 1.0 JSON-RPC](https://a2a-protocol.org/latest/) and [MCP](https://modelcontextprotocol.io/).

It allows agents and external applications to delegate tasks to peer agents, reuse multi-turn conversations, query live model catalogs and account quotas, and enforce tool permission boundaries—all on local loopback (`127.0.0.1`) without sharing cloud API keys.

---

## Supported Agent Harnesses

| Harness | Primary Integration Mechanism | Auth & Model Access | Key Features |
|---|---|---|---|
| **Codex** | Official `openai-codex` Python SDK | Local Codex App Server sign-in | Sandboxes (`workspace_write`, `read_only`, `full_access`), model & reasoning effort catalog, quota reporting via `account/rateLimits/read`. |
| **Antigravity** | Official `agy` CLI in headless mode (`-p` / `stream-json`) | Signed-in Antigravity account | Real-time models, efforts, and `/usage` quotas. Default settings or full access (`--dangerously-skip-permissions`). Optional legacy SDK backend. |
| **OpenCode** | Managed local HTTP server (`--pure serve`) | Local Ollama or cloud providers | Configured via JSON profile, fine-grained tool policies (`no_tools`, `read_only`, `workspace_write`, `full_access`), model variants. |
| **Claude Code** | Managed CLI in print mode (`claude -p`) | Local Ollama endpoint or Anthropic | Configured via JSON profile, isolated temporary session configs, resume support, safe mode vs full access. |

---

## Installation

Agent Shuttle requires **Python 3.11+** and runs on Windows, Linux, and macOS.

### Install from GitHub

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "git+https://github.com/Plartex/agent-shuttle.git"
```

The package is not published on PyPI yet. For development, clone the repository and install it in editable mode.

### Install from Local Checkout

You can install Agent Shuttle directly from its repository checkout into your project's virtual environment:

```powershell
# Create and activate your virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install in standard or editable mode
pip install C:\path\to\agent-shuttle
# or editable mode during development:
# pip install -e C:\path\to\agent-shuttle
```

Once installed, the CLI tools (`agent-shuttle`, `agent-shuttle-mcp`) and Python API (`agent_shuttle`) are fully accessible inside that virtual environment. The original checkout directory does not need to stay in place for runtime imports. Existing `agent_bridge` imports and `agent-bridge` commands remain supported as compatibility aliases; A2A metadata keys under `agent_bridge.*` are unchanged.

---

## Quickstart (Windows PowerShell)

For standalone development and testing inside this repository:

1. **Bootstrap dependencies:**
   ```powershell
   & .\Install-Shuttle.ps1
   ```
   *(If Python 3.11+ is not on `PATH`, set `$env:BRIDGE_BOOTSTRAP_PYTHON = 'C:\path\to\python.exe'` beforehand).*

2. **Generate MCP configuration files:**
   ```powershell
   & .\Configure-Shuttle-Mcp.ps1
   ```
   This creates `.codex/config.toml` and `.agents/mcp_config.json` with absolute paths to the environment.

3. **Start default Codex and Antigravity bridge servers:**
   ```powershell
   & .\Start-Shuttle.ps1
   ```
   This starts background servers on loopback ports:
   - Codex: `http://127.0.0.1:8765` (agent card: `http://127.0.0.1:8765/.well-known/agent-card.json`)
   - Antigravity: `http://127.0.0.1:8766` (agent card: `http://127.0.0.1:8766/.well-known/agent-card.json`)

4. **Stop background servers:**
   ```powershell
   & .\Stop-Shuttle.ps1
   ```

Runtime logs and process ID files are stored in `.runtime/` and ignored by version control.

---

## Minimal Examples

### 1. Python API

```python
import asyncio
from pathlib import Path
from agent_shuttle import ShuttleClient, HarnessLaunch, connect_harness

async def main():
    client = ShuttleClient()

    # Query live model catalog and quota information
    info = await client.info("http://127.0.0.1:8766")
    print("Selected model:", info["capabilities"]["selected_model"])

    # One-shot task
    result = await client.ask(
        "http://127.0.0.1:8766",
        "Explain the project structure and list entry points.",
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium",
    )
    print(f"[{result.state}] Task {result.task_id}:\n{result.text}")

    # Persistent multi-turn session (preserves conversation context)
    async with client.session(
        "http://127.0.0.1:8765",
        model="gpt-5.6-terra",
        reasoning_effort="high",
    ) as session:
        step1 = await session.ask("What database migrations are pending?")
        step2 = await session.ask("Generate SQL to apply the first migration.")
        print("Step 2 response:", step2.text)
        print("Step 2 token usage:", step2.usage)

    # Managed peer lifecycle: reuse existing server or start a temporary one
    launch = HarnessLaunch(
        name="antigravity",
        url="http://127.0.0.1:8766",
        workspace=Path.cwd(),
    )
    async with connect_harness(launch) as conn:
        print("Connected to:", conn.url, "(spawned temporary:", conn.started, ")")

asyncio.run(main())
```

### 2. Command-Line Interface (CLI)

```powershell
# Discover locally installed harnesses without starting models
agent-shuttle discover

# Start an A2A server for Codex
agent-shuttle serve codex --workspace . --port 8765

# Start an A2A server for Antigravity (CLI mode)
agent-shuttle serve antigravity --workspace . --port 8766

# Start an A2A server from a profile (OpenCode or Claude Code)
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Query server capabilities, models, and quota limits
agent-shuttle info http://127.0.0.1:8765

# Send a task from the command line
agent-shuttle ask http://127.0.0.1:8765 "Summarize recent changes" --model gpt-5.6-terra
```

### 3. Model Context Protocol (MCP)

Start the stdio MCP server:
```powershell
agent-shuttle-mcp
# or: python -m agent_shuttle.mcp_server
```

Available MCP tools:
- `ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?)`: Routes requests to any agent mapped in the `BRIDGE_AGENTS_JSON` environment variable.
- `get_agent_info(agent_id)`: Fetches live models and quotas for the profile agent.
- `ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`: Delegates directly to Antigravity (`BRIDGE_ANTIGRAVITY_URL` or a managed workspace server).
- `ask_codex(prompt, model?, reasoning_effort?)`: Delegates directly to Codex (`BRIDGE_CODEX_URL`).
- `get_antigravity_info(workspace?)` & `get_codex_info()`: Read live capabilities and quota without burning model turns.

---

## Key Concepts

### Harness Discovery
Run `agent-shuttle discover` (or `discover_harnesses()` in Python) to inspect local executables without launching processes or loading weights. It checks `PATH` and platform-specific standard installation directories (`%LOCALAPPDATA%\agy\bin`, npm global directories, etc.). Custom paths can be specified via environment variables (`BRIDGE_AGY_COMMAND`) or CLI flags (`--agy-command`, `--opencode-command`, `--claude-command`).

### Model & Reasoning Selection
Model parameters are passed as A2A metadata keys (`agent_bridge.model`, `agent_bridge.reasoning_effort`):
- **Antigravity:** Reasoning effort is embedded in model IDs (e.g. `gemini-3.8-flash-medium`). If both `--model` and `--effort` are passed, they must match.
- **Codex:** Model and reasoning effort are configured independently according to the catalog returned by `get_codex_info`.
- **OpenCode & Claude Code:** Profiles define `allowed_models` and optional `reasoning_efforts` (such as model variants for Ollama or CLI flags).

### Workspaces & Session Isolation
- Every server binds to a strictly validated, canonical workspace directory.
- `connect_harness()` verifies that an existing server's workspace matches the caller's target workspace before reusing it.
- **Sessions:** `BridgeSession` maintains a stateful conversation across multiple `ask()` calls. Conversation settings (model, effort, tool policy) are pinned at session creation and cannot be changed mid-session. Idle sessions are cleaned up automatically after 30 minutes.

### Safety & Tool Policies
Agent Shuttle defines four standardized tool policies:
- `no_tools`: Disables tool invocations entirely.
- `read_only`: Permits non-mutating search and file reading.
- `workspace_write`: Allows editing files within the designated workspace.
- `full_access`: Explicitly unclamps all tool restrictions and approval prompts.

> [!WARNING]
> `full_access` (or `--agy-dangerously-skip-permissions` for Antigravity) removes all tool approval gates across the entire server for all requests. Never enable this mode on untrusted tasks or expose endpoints beyond loopback. Antigravity CLI does not enforce `read_only` in headless mode and will reject such requests.

---

## Testing

Agent Shuttle provides a comprehensive offline test suite using fake backends that execute without network access, credentials, or model quota consumption:

```powershell
python -m unittest discover -s tests -v
```

Opt-in integration tests against real models can be executed by specifying target environments (e.g. `BRIDGE_LIVE_OLLAMA_MODEL=qwen3.5:9b` or `BRIDGE_LIVE_AGY_FULL_ACCESS=1`). See [CONTRIBUTING.md](CONTRIBUTING.md) for full instructions.

---

## Documentation Index

- [Getting Started Guide](docs/getting-started.md)
- [API Reference](docs/api.md)
- [Permissions & Safety Guide](docs/permissions.md)
- [Architecture & Protocol Design](docs/architecture.md)
- [Troubleshooting & Diagnostics](docs/troubleshooting.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
