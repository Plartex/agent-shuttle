"""Claude Code print-mode runtime with isolated resumable sessions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid

from .backends import BackendResponse
from .profiles import AgentProfile, ProfileSelection, ToolPolicy


_ESSENTIAL_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
}


def _environment(profile: AgentProfile, config_dir: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in _ESSENTIAL_ENV}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if profile.provider == "ollama":
        env["ANTHROPIC_BASE_URL"] = profile.endpoint.rstrip("/")
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
    else:
        if profile.endpoint:
            env["ANTHROPIC_BASE_URL"] = profile.endpoint
        if profile.credential_env:
            value = os.environ.get(profile.credential_env)
            if not value:
                raise RuntimeError(f"Missing credential environment variable {profile.credential_env}")
            env[profile.credential_env] = value
    return env


def _usage(result: dict, provider: str) -> tuple[dict[str, int], dict]:
    raw = result.get("usage") or {}
    mapping = {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "cache_read_tokens": raw.get("cache_read_input_tokens"),
        "cache_write_tokens": raw.get("cache_creation_input_tokens"),
    }
    usage = {key: value for key, value in mapping.items() if type(value) is int and value >= 0}
    if "input_tokens" in usage and "output_tokens" in usage:
        usage["total_tokens"] = sum(
            usage.get(key, 0) for key in
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        )
    details = {
        "usage_source": "claude_code_result",
        "cache_status": "not_supported" if provider == "ollama" else
        ("reported" if "cache_read_input_tokens" in raw else "unknown"),
        "input_includes_cache": False,
        "estimated": provider == "ollama",
        "raw_usage": raw,
    }
    return usage, details


class ClaudeCodeRuntime:
    def __init__(self, profile: AgentProfile):
        if profile.runtime != "claude_code":
            raise ValueError("ClaudeCodeRuntime requires a claude_code profile")
        self.profile = profile
        self.sessions: set[ClaudeCodeSession] = set()

    async def open_session(self, selection: ProfileSelection) -> "ClaudeCodeSession":
        session = ClaudeCodeSession(self.profile, selection, self)
        self.sessions.add(session)
        return session

    async def discover(self) -> dict:
        process = await asyncio.create_subprocess_exec(
            self.profile.runtime_command or "claude", "--version",
            cwd=str(self.profile.workspace), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"Claude Code version check failed: {stderr.decode(errors='replace')[-1000:]}")
        return {"healthy": True, "version": stdout.decode(errors="replace").strip()}

    async def close(self) -> None:
        for session in list(self.sessions):
            await session.close()


class ClaudeCodeSession:
    def __init__(self, profile: AgentProfile, selection: ProfileSelection, runtime: ClaudeCodeRuntime):
        self.profile = profile
        self.selection = selection
        self.runtime = runtime
        self.session_id = str(uuid.uuid4())
        self.config_dir = tempfile.TemporaryDirectory(prefix="agent-bridge-claude-")
        self.started = False
        self.closed = False

    def _command(self) -> list[str]:
        command = [
            self.profile.runtime_command or "claude", "-p", "--output-format", "json",
            "--bare", "--strict-mcp-config", "--setting-sources", "",
            "--permission-prompts", "none", "--model", self.selection.model,
        ]
        if self.selection.tool_policy is ToolPolicy.FULL_ACCESS:
            command.append("--dangerously-skip-permissions")
        elif self.selection.tool_policy is not ToolPolicy.WORKSPACE_WRITE:
            command.append("--safe-mode")
            command.extend([
                "--tools", "" if self.selection.tool_policy is ToolPolicy.NO_TOOLS else "Read,Glob,Grep",
            ])
        if self.selection.reasoning_effort is not None:
            command.extend(["--effort", self.selection.reasoning_effort])
        if self.started:
            command.extend(["--resume", self.session_id])
        else:
            command.extend(["--session-id", self.session_id])
        return command

    async def ask(self, prompt: str) -> BackendResponse:
        if self.closed:
            raise RuntimeError("Claude Code session is closed")
        env = _environment(self.profile, self.config_dir.name)
        process = await asyncio.create_subprocess_exec(
            *self._command(), cwd=str(self.profile.workspace), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self.profile.turn_timeout_seconds,
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            detail = stderr.decode(errors="replace")[-4000:]
            for key in (self.profile.credential_env, "ANTHROPIC_AUTH_TOKEN"):
                secret = env.get(key) if key else None
                if secret and secret != "ollama":
                    detail = detail.replace(secret, "[REDACTED]")
            raise RuntimeError(f"Claude Code failed ({process.returncode}): {detail}")
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Claude Code returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("type") != "result":
            raise RuntimeError("Claude Code returned an unexpected result envelope")
        if result.get("is_error") or result.get("subtype") in {"error", "error_max_turns", "error_during_execution"}:
            raise RuntimeError(f"Claude Code turn failed: {result.get('result') or result.get('subtype')}")
        if result.get("session_id") != self.session_id:
            raise RuntimeError("Claude Code returned a different session ID")
        text = result.get("result")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Claude Code returned no text response")
        self.started = True
        usage, details = _usage(result, self.profile.provider)
        return BackendResponse(text, usage, details)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.runtime.sessions.discard(self)
        self.config_dir.cleanup()
