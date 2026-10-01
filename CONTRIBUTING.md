# Contributing to Agent Shuttle

[Russian version / Русская версия](CONTRIBUTING.ru.md)

Thank you for contributing to Agent Shuttle. This project maintains a modular, local-first bridge between heterogeneous coding agent harnesses. Please follow these guidelines for development, testing, and documentation.

---

## Development Setup

1. **Virtual Environment Setup:**
   ```powershell
   python -m venv .venv
   & .\.venv\Scripts\python.exe -m pip install -e ".[test]"
   ```
   On Linux or macOS, use `.venv/bin/python` in place of `.venv\Scripts\python.exe`.

2. **Clean Repository Hygiene:**
   - Keep account credentials, personal API keys, and local binary executables out of Git.
   - Local configuration files (`.codex/config.toml`, `.agents/mcp_config.json`) and runtime artifacts (`.runtime/`) are ignored by `.gitignore` and must never be committed.

---

## Test-Driven Development (TDD) Workflow

We follow a strict Test-Driven Development methodology for both bug fixes and new features:

1. **Write a Reproducing Test First:**  
   Before modifying runtime or backend logic, write an offline regression test in `tests/` that clearly reproduces the issue or asserts the required protocol behavior.
2. **Implement the Minimal Fix:**  
   Update the code to pass the regression test without breaking existing contracts.
3. **Verify Offline Suite:**  
   Ensure all existing and new unit tests pass cleanly.

---

## Running the Test Suite

### 1. Default Offline Test Suite

The default test suite uses mock and fake backends. It runs completely offline without network access, credentials, or model quota consumption:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Before opening a pull request or committing changes, ensure that all tests in this suite pass.

### 2. Opt-In Live Integration Tests

Live tests against real model runtimes are skipped by default. Run them only when explicitly testing local harness integrations:

- **Live Ollama Integration (OpenCode & Claude Code):**
  Requires a running Ollama daemon (`http://127.0.0.1:11434`) with the model pulled:
  ```powershell
  $env:BRIDGE_LIVE_OLLAMA_MODEL = 'qwen3.5:9b'
  # Optionally override executable locations:
  # $env:BRIDGE_LIVE_OPENCODE_COMMAND = 'C:\tools\opencode.exe'
  # $env:BRIDGE_LIVE_CLAUDE_COMMAND = 'C:\tools\claude.exe'
  .\.venv\Scripts\python.exe -m unittest tests.test_live_ollama -v
  ```

- **Live Antigravity Permissions Smoke:**
  Tests real `agy` tool execution under full access in an isolated temporary directory:
  ```powershell
  $env:BRIDGE_LIVE_AGY_FULL_ACCESS = '1'
  # Optionally test through a temporary A2A server:
  $env:BRIDGE_LIVE_AGY_A2A_FULL_ACCESS = '1'
  # Optionally specify custom binary path:
  # $env:BRIDGE_LIVE_AGY_COMMAND = 'C:\path\to\agy.exe'
  .\.venv\Scripts\python.exe -m unittest tests.test_live_antigravity_permissions -v
  ```

---

## Documentation Validation

When updating or adding documentation:

1. **Bilingual Parity:**  
   English (`README.md`, `docs/*.md`) is the canonical/default language. The Russian documentation (`README.ru.md`, `docs/ru/*.md`) must be a complete, semantically faithful counterpart.
2. **Reciprocal Language Links:**  
   Ensure every English document contains a prominent link to its Russian equivalent at the top, and vice versa.
3. **Relative Link Integrity:**  
   Check that all relative Markdown links resolve correctly.
4. **Code Verification:**  
   Verify that all CLI commands, parameter names, function signatures, and JSON schemas in documentation match the current implementation in `agent_bridge/`.
