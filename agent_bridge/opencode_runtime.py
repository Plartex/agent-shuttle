"""OpenCode HTTP runtime with per-policy managed servers."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .backends import BackendResponse
from .profiles import AgentProfile, ProfileSelection, ToolPolicy


_READ_TOOLS = {"read", "glob", "grep", "lsp"}
_KNOWN_TOOLS = _READ_TOOLS | {
    "edit", "bash", "task", "webfetch", "websearch", "skill", "question",
    "todowrite", "todoread", "write", "patch", "mcp",
}


def _resolve_command(command: str) -> str:
    """Prefer the real executable over an npm .cmd shim we cannot supervise."""
    found = shutil.which(command) or command
    path = Path(found)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        executable = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if executable.is_file():
            return str(executable)
        raise RuntimeError("OpenCode .cmd launcher cannot be supervised; set runtime_command to opencode.exe")
    return found


def _permissions(policy: ToolPolicy) -> dict:
    if policy is ToolPolicy.NO_TOOLS:
        return {"*": "deny"}
    if policy is ToolPolicy.READ_ONLY:
        return {"*": "deny", **{tool: "allow" for tool in _READ_TOOLS}}
    if policy is ToolPolicy.WORKSPACE_WRITE:
        return {"*": "allow", "external_directory": "deny"}
    return {"*": "allow"}


def _inline_config(profile: AgentProfile, policy: ToolPolicy) -> str:
    permission = _permissions(policy)
    bridge_agent = {
        "mode": "primary",
        "description": "Agent Shuttle constrained analysis runtime",
        "prompt": (
            "You are a concise analysis assistant. Answer the user's request directly. "
            "Do not call tools or ask for permissions."
            if policy is ToolPolicy.NO_TOOLS else
            "You are a read-only analysis assistant. Use read and search tools only when needed. "
            "Never modify files or execute commands."
            if policy is ToolPolicy.READ_ONLY else
            "You are an analysis assistant. Follow the user's task and use tools when needed."
        ),
        "permission": permission,
    }
    config: dict = {
        "permission": permission,
        "agent": {"build": {"permission": permission}, "bridge": bridge_agent},
        "share": "disabled",
    }
    if profile.provider == "ollama":
        config["provider"] = {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {"baseURL": profile.endpoint.rstrip("/") + "/v1"},
                "models": {
                    model: {
                        "name": model,
                        "variants": {
                            effort: {"reasoningEffort": effort}
                            for effort in profile.reasoning_efforts
                        },
                    }
                    for model in profile.allowed_models
                },
            }
        }
    return json.dumps(config, ensure_ascii=False)


def _child_env(profile: AgentProfile, policy: ToolPolicy, password: str) -> dict[str, str]:
    # Keep only process essentials; never inherit unrelated cloud provider keys.
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["OPENCODE_CONFIG_CONTENT"] = _inline_config(profile, policy)
    env["OPENCODE_SERVER_PASSWORD"] = password
    if profile.credential_env:
        if profile.credential_env not in os.environ:
            raise RuntimeError(f"Missing credential environment variable {profile.credential_env}")
        env[profile.credential_env] = os.environ[profile.credential_env]
    return env


def _usage(info: dict) -> tuple[dict[str, int], dict]:
    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    mapping = {
        "input_tokens": tokens.get("input"),
        "output_tokens": tokens.get("output"),
        "thinking_tokens": tokens.get("reasoning"),
        "cache_read_tokens": cache.get("read"),
        "cache_write_tokens": cache.get("write"),
    }
    usage = {key: value for key, value in mapping.items() if type(value) is int and value >= 0}
    if "input_tokens" in usage and "output_tokens" in usage:
        usage["total_tokens"] = sum(
            usage.get(key, 0) for key in
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        )
    return usage, {
        "usage_source": "opencode_message",
        "cache_status": "reported" if isinstance(tokens.get("cache"), dict) else "unknown",
        "input_includes_cache": False,
        "raw_usage": tokens,
    }


@dataclass
class _Server:
    client: httpx.AsyncClient
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task | None = None
    stderr_tail: bytes = b""


class OpenCodeRuntime:
    def __init__(self, profile: AgentProfile, *, transport: httpx.AsyncBaseTransport | None = None):
        if profile.runtime != "opencode":
            raise ValueError("OpenCodeRuntime requires an opencode profile")
        self.profile = profile
        self.transport = transport
        self._servers: dict[ToolPolicy, _Server] = {}
        self._lock = asyncio.Lock()

    async def _server(self, policy: ToolPolicy) -> _Server:
        async with self._lock:
            if policy in self._servers:
                return self._servers[policy]
            if self.profile.runtime_url:
                if self.transport is None:
                    raise RuntimeError("An attached OpenCode server cannot enforce the profile tool policy")
                server = _Server(httpx.AsyncClient(
                    base_url=self.profile.runtime_url,
                    transport=self.transport,
                    timeout=self.profile.turn_timeout_seconds,
                ))
                self._servers[policy] = server
                return server
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            password = secrets.token_urlsafe(32)
            command = _resolve_command(self.profile.runtime_command or "opencode")
            process = await asyncio.create_subprocess_exec(
                command, "--pure", "serve", "--hostname", "127.0.0.1", "--port", str(port),
                cwd=str(self.profile.workspace), env=_child_env(self.profile, policy, password),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            server = _Server(httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", auth=("opencode", password),
                timeout=self.profile.turn_timeout_seconds,
            ), process=process)
            server.stderr_task = asyncio.create_task(self._drain_stderr(server))
            try:
                for _ in range(100):
                    if process.returncode is not None:
                        raise RuntimeError(
                            f"OpenCode server exited ({process.returncode}): "
                            + server.stderr_tail.decode(errors="replace")
                        )
                    try:
                        response = await server.client.get("/global/health", timeout=2)
                        response.raise_for_status()
                        if response.json().get("healthy"):
                            self._servers[policy] = server
                            return server
                    except (httpx.HTTPError, ValueError):
                        pass
                    await asyncio.sleep(0.2)
                raise RuntimeError("OpenCode server did not become healthy within 20 seconds")
            except BaseException:
                await self._stop_server(server)
                raise

    @staticmethod
    async def _drain_stderr(server: _Server) -> None:
        assert server.process is not None and server.process.stderr is not None
        while chunk := await server.process.stderr.read(4096):
            server.stderr_tail = (server.stderr_tail + chunk)[-4000:]

    async def open_session(self, selection: ProfileSelection) -> "OpenCodeSession":
        server = await self._server(selection.tool_policy)
        response = await server.client.post("/session", json={"title": "Agent Shuttle"})
        response.raise_for_status()
        session_id = response.json().get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("OpenCode did not return a session ID")
        return OpenCodeSession(
            server.client, session_id, selection, self.profile.turn_timeout_seconds,
        )

    async def discover(self) -> dict:
        server = await self._server(self.profile.default_tool_policy)
        response = await server.client.get("/global/health")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        async with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            await self._stop_server(server)

    @staticmethod
    async def _stop_server(server: _Server) -> None:
        await server.client.aclose()
        if server.process and server.process.returncode is None:
            server.process.terminate()
            try:
                await asyncio.wait_for(server.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                server.process.kill()
                await server.process.wait()
        if server.stderr_task:
            try:
                await asyncio.wait_for(server.stderr_task, timeout=2)
            except asyncio.TimeoutError:
                server.stderr_task.cancel()
                try:
                    await server.stderr_task
                except asyncio.CancelledError:
                    pass


class OpenCodeSession:
    def __init__(
        self, client: httpx.AsyncClient, session_id: str,
        selection: ProfileSelection, timeout_seconds: float,
    ):
        self.client = client
        self.session_id = session_id
        self.selection = selection
        self.timeout_seconds = timeout_seconds
        self.closed = False

    async def ask(self, prompt: str) -> BackendResponse:
        if self.closed:
            raise RuntimeError("OpenCode session is closed")
        provider, model = self.selection.model.split("/", 1)
        payload: dict = {
            "agent": "build" if self.selection.tool_policy in {ToolPolicy.WORKSPACE_WRITE, ToolPolicy.FULL_ACCESS} else "bridge",
            "model": {"providerID": provider, "modelID": model},
            "parts": [{"type": "text", "text": prompt}],
        }
        if self.selection.reasoning_effort is not None:
            payload["variant"] = self.selection.reasoning_effort
        if self.selection.tool_policy not in {ToolPolicy.WORKSPACE_WRITE, ToolPolicy.FULL_ACCESS}:
            payload["tools"] = {
                tool: self.selection.tool_policy is ToolPolicy.READ_ONLY and tool in _READ_TOOLS
                for tool in _KNOWN_TOOLS
            }
        path = f"/session/{quote(self.session_id, safe='')}/message"
        try:
            response = await asyncio.wait_for(
                self.client.post(path, json=payload), timeout=self.timeout_seconds,
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            try:
                await asyncio.wait_for(
                    self.client.post(f"/session/{quote(self.session_id, safe='')}/abort"),
                    timeout=5,
                )
            except (httpx.HTTPError, asyncio.TimeoutError):
                pass
            raise
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("OpenCode returned an invalid result envelope")
        info = result.get("info") or {}
        if info.get("error"):
            raise RuntimeError(f"OpenCode turn failed: {info['error']}")
        text = "".join(
            part.get("text", "") for part in result.get("parts", [])
            if part.get("type") == "text" and isinstance(part.get("text"), str)
        )
        if not text.strip():
            steps = [
                {"type": part.get("type"), "reason": part.get("reason")}
                for part in result.get("parts", []) if isinstance(part, dict)
            ]
            raise RuntimeError(
                f"OpenCode returned no text response (steps: {steps}, "
                f"tokens: {(info.get('tokens') or {})})"
            )
        usage, details = _usage(info)
        return BackendResponse(text, usage, details)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        response = await asyncio.wait_for(
            self.client.delete(f"/session/{quote(self.session_id, safe='')}"),
            timeout=min(5, self.timeout_seconds),
        )
        response.raise_for_status()
