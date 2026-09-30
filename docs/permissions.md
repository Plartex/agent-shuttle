# Permissions and Safety Guide

[Russian version / Русская версия](ru/permissions.md)

Agent Shuttle establishes a standardized, multi-tiered security and tool policy model across heterogeneous agent harnesses. This guide explains how tool policies map to each underlying harness, how permissions are enforced, and the critical security boundaries that must be maintained.

---

## The Four Standard Tool Policies

Agent Shuttle standardizes tool permissions into four explicit tiers:

| Policy | Rank | Description |
|---|---|---|
| **`no_tools`** | 0 | Prohibits all agent tool calls. The model operates purely as a conversational/analysis text generator. |
| **`read_only`** | 1 | Allows read-only discovery tools (e.g. file reading, directory listing, code search/grep) without file mutations or shell execution. |
| **`workspace_write`** | 2 | Allows creating and modifying files within the designated project directory. Restricts access to external directories and destructive actions. |
| **`full_access`** | 3 | Disables all permission prompts and tool restrictions across the harness. |

---

## Harness-Specific Mappings

Each underlying harness enforces security policies differently. Agent Shuttle maps the unified policies to native harness primitives as follows:

### 1. Codex

Codex is managed through the official `openai-codex` Python SDK, which utilizes native sandboxes:

- **`no_tools`**: **Unsupported.** Codex App Server currently requires an active sandbox environment; requesting `no_tools` raises a `ValueError("Codex cannot enforce no_tools")`.
- **`read_only`**: Maps to `Sandbox.read_only`.
- **`workspace_write`**: Maps to `Sandbox.workspace_write`.
- **`full_access`**: Maps to `Sandbox.full_access` (disables container sandboxing and approval gates).

### 2. Antigravity CLI

Antigravity executes via the official `agy` CLI in headless mode (`-p` / `stream-json`):

- **Default Permission Mode (`settings`)**:
  - The CLI respects scoped permission rules defined in the user's `~/.gemini/antigravity-cli/settings.json`.
  - **Headless Confirmation Limitation:** In headless mode, the CLI cannot interactively ask the user for approval. Consequently, actions governed by `ask` or `deny` rules are rejected immediately. Rule precedence is `deny` > `ask` > `allow`.
  - **Soft-Denial Detection:** When a tool call is denied, `agy` may exit with return code 0 and emit `status: SUCCESS` with `denied_actions` or an empty response body. Agent Shuttle inspects the output JSON and raises `AntigravityPermissionDenied` or `RuntimeError` rather than returning a misleading empty or truncated response.
- **`full_access` (`all` mode)**:
  - Enabled by starting the server with `--agy-dangerously-skip-permissions` or passing `HarnessLaunch(tool_policy="full_access")`.
  - Passes `--dangerously-skip-permissions` to all CLI calls on that server.
- **`read_only` & `no_tools`**: **Unsupported.** Antigravity CLI does not provide a guaranteed sandbox or read-only flag in headless mode. Agent Shuttle explicitly rejects `read_only` and `no_tools` to prevent a false sense of security.

### 3. OpenCode

OpenCode is managed as an isolated background HTTP daemon (`--pure serve`) using token authentication and inline configuration via `OPENCODE_CONFIG_CONTENT`:

- **`no_tools`**: Injects `{"permission": {"*": "deny"}}` and instructions to avoid tool usage.
- **`read_only`**: Injects `{"permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "lsp": "allow"}}`.
- **`workspace_write`**: Injects `{"permission": {"*": "allow", "external_directory": "deny"}}`. Restricts mutating operations outside the workspace.
- **`full_access`**: Injects `{"permission": {"*": "allow"}}` (permitting tools and access to external directories).

### 4. Claude Code

Claude Code is managed in print mode (`claude -p`) with isolated temporary configuration directories:

- **`no_tools`**: Runs with `--safe-mode --tools "" --permission-prompts none`.
- **`read_only`**: Runs with `--safe-mode --tools "Read,Glob,Grep" --permission-prompts none`.
- **`workspace_write`**: Runs in normal mode within the specified project workspace directory.
- **`full_access`**: Runs with `--dangerously-skip-permissions`.

---

## Server-Owned Profiles & Privilege Escalation Prevention

For OpenCode and Claude Code, security policies are governed by server-owned JSON profiles (`AgentProfile`).

Each profile specifies a `max_tool_policy`:
```json
{
  "id": "code-reviewer",
  "runtime": "opencode",
  "provider": "ollama",
  "default_model": "qwen3.5:9b",
  "allowed_models": ["qwen3.5:9b"],
  "default_tool_policy": "no_tools",
  "max_tool_policy": "read_only"
}
```

- When a client sends a request with `--tool-policy`, `profile.resolve()` checks the requested policy rank against `max_tool_policy`.
- If a client requests `workspace_write` or `full_access` on a profile whose maximum is `read_only`, Agent Shuttle rejects the request immediately before starting any model turn or background process.
- No client can escalate permissions beyond the server's configured profile ceiling.

---

## Critical Risks of `full_access`

> [!CAUTION]
> The `full_access` policy removes all tool approval gates and allows the agent to execute shell commands, alter system files, and access directories outside the workspace.

1. **Server-Wide Scope:**  
   In Antigravity (`--agy-dangerously-skip-permissions`) and Codex (`Sandbox.full_access`), `full_access` applies to the **entire running server instance** and all subsequent turns. It is not limited to a single prompt, request, or session.
2. **Local Multi-Client Access:**  
   Because the local A2A server listens on loopback (`127.0.0.1`) without authentication, any local process on the machine can connect to that port and submit tasks that will run with full unapproved privileges.
3. **Never Run Untrusted Tasks:**  
   Do not feed untrusted code, external prompt injections, or unverified tasks to an agent running under `full_access`. If you must evaluate untrusted code, run the entire Agent Shuttle and harness inside a hardened OS container or virtual machine.

---

## Safe Configuration of Antigravity Settings

If you require partial tool execution for Antigravity without giving total system access (`full_access`), configure granular rules in your personal `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "read_file",
      "list_dir",
      "grep_search"
    ],
    "deny": [
      "system_restart",
      "format_disk"
    ]
  }
}
```

- Do not set rules to `"ask"` for automated or headless environments, as `ask` acts as a denial when no interactive terminal is present.
- Bridge does not modify `settings.json` automatically.

---

## Network Isolation Notice

Agent Shuttle servers bind exclusively to `127.0.0.1` (loopback). There is no TLS encryption or authentication on the A2A HTTP port. Exposing these ports to an external or local network without a reverse proxy enforcing TLS and strict authentication exposes your workstation to remote code execution.
