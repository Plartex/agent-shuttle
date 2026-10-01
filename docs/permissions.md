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

Before delegation, the coordinator chooses the workspace and least sufficient enforceable policy for the task. `get_agent_info` and `/bridge/identity` report `supported_tool_policies` and `default_tool_policy`; `tool_policy_notes` explains additional limitations. The list describes the current server and respects profile limits. Omitting a policy uses the harness default and does not imply read-only access. Never replace an unsupported restrictive policy with `full_access` automatically.

MCP calls to Codex and Antigravity default to `read_only`. The caller selects `workspace_write` when the user requests file edits; this guidance is delivered in MCP server instructions and tool descriptions, independently of a local `AGENTS.md` or caller account. Configured profiles retain their own default. An incompatible running peer is left intact while MCP starts a fresh peer for the requested workspace and policy.

Antigravity executes via the official `agy` CLI in headless mode (`-p` / `stream-json`):

- **Default Permission Mode (`settings`)**:
  - The CLI respects scoped permission rules defined in the user's `~/.gemini/antigravity-cli/settings.json`.
  - **Headless Confirmation Limitation:** In headless mode, the CLI cannot interactively ask the user for approval. Consequently, actions governed by `ask` or `deny` rules are rejected immediately. Rule precedence is `deny` > `ask` > `allow`.
  - **Soft-Denial Detection:** When a tool call is denied, `agy` may exit with return code 0 and emit `status: SUCCESS` with `denied_actions` or an empty response body. Agent Shuttle inspects the output JSON and raises `AntigravityPermissionDenied` or `RuntimeError` rather than returning a misleading empty or truncated response.
- **Scoped `no_tools`, `read_only`, and `workspace_write`**:
  - Agent Shuttle starts a stream session from a temporary control workspace containing its own `PreToolUse` hook. The project is accessed through exact resource grants, without loading the project's hooks into the active workspace. User-wide Antigravity settings are not changed.
  - Before sending the real task, Shuttle requests a disposable canary write with all tools blocked and verifies the hook's recorded denial. If this probe is absent or fails, the real task is not sent. The same process then receives the selected scoped policy.
  - `no_tools` denies every agent tool. `read_only` allows native `view_file`, `list_dir`, `find_by_name`, and `grep_search` within the project. `workspace_write` additionally permits native file creation/replacement there, with `accept-edits` enabled behind the hook.
  - Shell, MCP, subagents, permission changes, unknown tools, and external paths are denied. Writes to control directories such as `.git` and `.agents`, and edits through hard links, are denied. The coordinator runs required test commands in its own approved environment.
  - Scoped sessions disable the CLI's interactive permission prompts with `--dangerously-skip-permissions`; this flag does not bypass `PreToolUse`. The verified hook supplies the access boundary, so native approvals need no user allowlist. Hook errors block execution. This is a verified tool gate, not an OS sandbox, and it relies on the installed CLI and trusted user-wide runtime customizations.
  - The canary check requires an extra model turn; one-shot usage includes that overhead, also reported as `policy_probe_usage`. Temporary controls are removed after terminating the session's process tree.
- **`full_access` (`all` mode)**:
  - Enabled by starting the server with `--agy-dangerously-skip-permissions` or passing `HarnessLaunch(tool_policy="full_access")`.
  - Unscoped full-access calls use the CLI's all-permissions flag without a scoped hook. Scoped calls always use their task hook, including on servers configured for full access.

The direct Python/A2A API preserves legacy `tool_policy=None` behavior: native Antigravity settings apply, and workspace writes may be allowed. A `permissions.allow` list, planning mode, and a Git worktree do not enforce read-only access. Use an explicit scoped policy for those calls.

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

## Legacy Antigravity Settings

Scoped task policies do not require changes to Antigravity settings. For legacy calls with `tool_policy=None`, granular rules can be configured in your personal `~/.gemini/antigravity-cli/settings.json`; an allowlist alone does not establish a read-only boundary:

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
