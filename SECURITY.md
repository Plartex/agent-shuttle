# Security Policy

[Russian version / Русская версия](SECURITY.ru.md)

This document outlines the security architecture, trust boundaries, known operational risks, and reporting procedures for Agent Shuttle.

---

## Localhost Binding & Lack of Network Authentication

- **Loopback Only:** Agent Shuttle servers bind strictly to `127.0.0.1` (loopback).
- **No Network Authentication:** The A2A HTTP endpoints (`/`, `/bridge/*`) have **no built-in authentication or encryption**. Any process or local user running on the same host can connect to an open Bridge port and submit tasks or inspect live account quota data.
- **Do Not Expose Externally:** Never bind Agent Shuttle ports to `0.0.0.0` or expose them over a local network or the internet without placing them behind a reverse proxy (e.g. Nginx, Caddy) that enforces TLS termination and strong authentication.

---

## Explicit Risks of `full_access`

The `full_access` policy (and `--agy-dangerously-skip-permissions` for Antigravity) completely removes tool approval gates:

1. **Server-Wide Scope:**  
   Enabling `full_access` affects the entire running Bridge server instance and all subsequent task turns. It is not confined to a single request or directory.
2. **Arbitrary Command Execution:**  
   Under `full_access`, the agent can execute shell commands, read/write files outside the workspace, and mutate host system configuration with the permissions of the user running the process.
3. **Untrusted Tasks:**  
   Never pass untrusted code, external prompt injections, or unverified instructions to an agent running under `full_access`. If you must analyze untrusted code, run the entire Agent Shuttle setup inside an isolated virtual machine or container sandbox.

---

## Tool Policies vs. Operating System Sandboxes

- Agent Shuttle tool policies (`no_tools`, `read_only`, `workspace_write`) constrain tool selection at the harness policy level.
- **They are not an operating system sandbox.** While `read_only` prevents tool-assisted modifications, internal runtime processes might still create temporary files or caches.
- **Antigravity CLI Limitation:** Antigravity CLI does not provide a reliable read-only tool sandbox in headless mode. Agent Shuttle deliberately rejects `read_only` and `no_tools` requests for Antigravity before model execution to prevent false assumptions of safety.
- For true filesystem and network containment, run tasks within an OS container (e.g. Docker, Podman) or hypervisor sandbox.

---

## Credential and Secret Management

- **No Secrets in Profiles:** Do not hardcode API keys, passwords, or authentication tokens into `profile.json` files. Use the `credential_env` field to reference environment variables.
- **Loopback Ollama Restriction:** Agent Shuttle enforces that Ollama endpoints must use local HTTP loopback (`127.0.0.1`, `localhost`, `::1`). Remote unauthenticated Ollama endpoints are rejected.
- **Runtime Logs:** Inspect `.runtime/*.log` before sharing diagnostic files; prompts, responses, and stack traces may contain private source code or environment variables.

---

## Vulnerability Reporting

- **Reporting Channel:**  
  No dedicated private security reporting email address is currently configured for this repository.
- If you discover a potential security vulnerability, use GitHub's private vulnerability reporting feature if it is enabled for the repository, or contact the repository owner privately.
- **Responsible Disclosure:**  
  Do **not** disclose security vulnerabilities, access tokens, API credentials, or exploitable payloads in public issue discussions.
