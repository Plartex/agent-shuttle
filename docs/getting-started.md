# Getting Started with Agent Shuttle

[Russian version / Русская версия](ru/getting-started.md)

This guide walks you through setting up Agent Shuttle, verifying harness availability, starting local A2A and MCP servers, and sending your first agent tasks via Python, CLI, and MCP.

---

## Prerequisites

Agent Shuttle interacts with local coding harnesses on your workstation. Make sure you have:

- **Python 3.11 or newer** installed and available on `PATH`.
- For **Codex**:
  - The Codex desktop or CLI app installed and signed in.
  - Python SDK dependency (`openai-codex`) is installed automatically by Agent Shuttle.
- For **Antigravity**:
  - The official `agy` CLI installed and authenticated (run `agy` interactively to sign in, then verify with `agy models`).
  - Test sign-in by running `agy models` in your terminal.
- For **OpenCode** (optional):
  - `opencode` installed (e.g. via npm: `npm i -g opencode-ai`).
  - For local models: [Ollama](https://ollama.ai/) running on `http://127.0.0.1:11434` with your desired model pulled (e.g. `ollama pull qwen3.5:9b`).
- For **Claude Code** (optional):
  - `claude` CLI installed (`npm install -g @anthropic-ai/claude-code`).
  - Authenticated with Anthropic or configured with a local Ollama endpoint.

---

## Setup in the Repository Checkout (Windows)

If you are developing or running Agent Shuttle directly from a git clone:

```powershell
# 1. Clone the repository
git clone https://github.com/Plartex/agent-shuttle.git
Set-Location agent-shuttle

# 2. Bootstrap virtual environment and install dependencies
& .\Install-Shuttle.ps1

# 3. Generate MCP configurations for Codex and Antigravity
& .\Configure-Shuttle-Mcp.ps1

# 4. Open your MCP client and call ask_codex or ask_antigravity.
# The A2A server starts automatically for each request if needed.
```

If you also start persistent servers manually, stop them with:
```powershell
& .\Stop-Shuttle.ps1
```

Persistent server logs and process identifiers are written to `.runtime/` (`codex.out.log`, `codex.err.log`, `antigravity.out.log`, `antigravity.err.log`).

---

## Using Agent Shuttle as a Dependency in Another Project

You can install Agent Shuttle into any other Python project without keeping the source checkout active.

```powershell
# In your project directory:
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install directly from the local checkout or built wheel
pip install C:\path\to\agent-shuttle
```

Once installed into your project's `.venv`:
- The CLI command `agent-shuttle` is installed in `.venv\Scripts\agent-shuttle.exe`.
- The MCP server `agent-shuttle-mcp` is installed in `.venv\Scripts\agent-shuttle-mcp.exe`.
- The library can be imported in Python: `from agent_shuttle import ShuttleClient, connect_harness, HarnessLaunch`.

---

## Verifying Harness Discovery

Before starting servers, inspect which harnesses are discovered on your system:

```powershell
agent-shuttle discover
```

Example JSON output:
```json
{
  "codex": "agent-shuttle",
  "antigravity": "C:\\Users\\username\\AppData\\Local\\agy\\bin\\agy.exe",
  "opencode": "C:\\Users\\username\\AppData\\Roaming\\npm\\node_modules\\opencode-ai\\bin\\opencode.exe",
  "claude_code": "C:\\Users\\username\\.local\\bin\\claude.exe"
}
```

If an executable is located outside standard search paths, you can provide an override:
```powershell
agent-shuttle discover --agy-command 'D:\tools\agy.exe'
# Or via environment variable:
$env:BRIDGE_AGY_COMMAND = 'D:\tools\agy.exe'
```

---

## Starting Servers

### Codex Server
Runs the Codex backend bound to the current project directory on port 8765:
```powershell
agent-shuttle serve codex --workspace . --port 8765
```

### Antigravity Server
Runs the Antigravity headless CLI backend on port 8766:
```powershell
agent-shuttle serve antigravity --workspace . --port 8766
```

### OpenCode or Claude Code (Ollama Profile)
OpenCode and Claude Code run using server-owned JSON profiles. Agent Shuttle includes ready-to-use profiles in `examples/`:

```powershell
# OpenCode with local Ollama
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Claude Code with local Ollama
agent-shuttle serve profile --profile .\examples\claude-code-ollama.json --workspace . --port 8768
```

The `--workspace` parameter sets the agent's target working directory and overrides any relative `workspace` setting inside the JSON profile.

---

## Verifying Server Status

Each server provides read-only HTTP endpoints on loopback:

- **Identity check** (fast, zero model execution):
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8765/bridge/identity
  ```
  Returns `{"agent": "codex", "backend": "codex_app_server", "workspace": "C:\\path\\to\\project", ...}`.

- **Capabilities & Quotas** (queries harness metadata):
  ```powershell
  agent-shuttle info http://127.0.0.1:8765
  ```

---

## Sending Your First Tasks

### Via CLI
```powershell
agent-shuttle ask http://127.0.0.1:8765 "Check the test suite and report failing tests."
```

With model and effort selection:
```powershell
agent-shuttle ask http://127.0.0.1:8766 "Explain the architecture of this repo." `
  --model gemini-3.8-flash-medium --reasoning-effort medium
```

### Via Python API
```python
import asyncio
from agent_shuttle import ShuttleClient

async def main():
    client = ShuttleClient()
    result = await client.ask(
        "http://127.0.0.1:8765",
        "List all Python entry points in pyproject.toml",
    )
    print("Task ID:", result.task_id)
    print("State:", result.state)
    print("Output:\n", result.text)

asyncio.run(main())
```

### Via Model Context Protocol (MCP)
Add Agent Shuttle to your client MCP configuration (such as Codex `.codex/config.toml` or Antigravity `.agents/mcp_config.json`).

Example configuration:
```toml
[mcp_servers.agent_shuttle]
command = "C:/path/to/project/.venv/Scripts/python.exe"
args = ["-m", "agent_shuttle.mcp_server"]
tool_timeout_sec = 1800

[mcp_servers.agent_shuttle.env]
BRIDGE_CODEX_URL = "http://127.0.0.1:8765"
BRIDGE_ANTIGRAVITY_URL = "http://127.0.0.1:8766"
BRIDGE_WORKSPACE = "C:/path/to/project"
```

No separate `agent-shuttle serve` command is needed for MCP requests. If you keep the optional fixed URLs below, a matching running server is reused; otherwise Agent Shuttle starts a temporary server for the request. Set `BRIDGE_WORKSPACE` to the project you want the agent to inspect.

Then invoke tools in your agent chats:
```text
ask_antigravity(prompt="Audit tests in tests/test_backends.py", model="gemini-3.8-flash-medium")
ask_codex(prompt="Refactor function in agent_bridge/discovery.py", model="gpt-5.6-terra")
```

---

## Next Steps

- Explore the complete [API Reference](api.md) for programmatic supervision and sessions.
- Review [Permissions & Safety Guide](permissions.md) before using `full_access`.
- Learn about internal communication and protocol flow in [Architecture](architecture.md).
- Consult [Troubleshooting](troubleshooting.md) for diagnostic procedures and common issues.
