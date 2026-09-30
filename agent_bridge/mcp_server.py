"""MCP tools that delegate to the A2A agents."""

from __future__ import annotations

import os
import json
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from .client import BridgeClient
from .managed import HarnessLaunch, connect_harness


def _debug(stage: str) -> None:
    if os.environ.get("BRIDGE_DEBUG") == "1":
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[agent-shuttle] {stamp} {stage}", file=sys.stderr, flush=True)


mcp = FastMCP(
    "Agent Shuttle",
    instructions=(
        "Use ask_agent for configured OpenCode, Claude Code, or other agent profiles. "
        "The legacy ask_antigravity and ask_codex tools remain available. "
        "Use get_antigravity_info or get_codex_info to check current models, efforts and account quotas. "
        "Each call starts a new remote task. When the user names a model for the remote agent, "
        "pass that model ID exactly in the optional model parameter. When the user names a "
        "reasoning effort, pass it exactly in reasoning_effort. "
        "Return the remote result to the user."
    ),
)


async def _ask(
    env_name: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    tool_policy: str | None = None,
) -> dict:
    url = os.environ.get(env_name)
    if not url:
        raise RuntimeError(f"Set {env_name} to the local A2A server URL")
    result = await BridgeClient().ask(
        url,
        prompt,
        model=model,
        reasoning_effort=reasoning_effort,
        tool_policy=tool_policy,
    )
    return {
        "task_id": result.task_id,
        "context_id": result.context_id,
        "state": result.state,
        "text": result.text,
        "usage": result.usage,
        "details": result.details,
    }


def _free_local_url() -> str:
    """Choose an isolated loopback port for a per-call managed Bridge."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


async def _managed_antigravity(
    prompt: str, workspace: str, model: str | None,
    reasoning_effort: str | None, tool_policy: str | None,
    turn_timeout_seconds: float,
) -> dict:
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    launch = HarnessLaunch(
        "antigravity", _free_local_url(), root,
        model=model, tool_policy=tool_policy,
        agy_turn_timeout_seconds=turn_timeout_seconds,
    )
    _debug("antigravity: starting temporary bridge")
    async with connect_harness(launch) as peer:
        _debug("antigravity: bridge ready, sending task")
        result = await BridgeClient().ask(
            peer.url, prompt, model=model, reasoning_effort=reasoning_effort,
            tool_policy=tool_policy,
        )
        _debug(f"antigravity: task returned {result.state}")
    _debug("antigravity: temporary bridge stopped")
    return {
        "task_id": result.task_id, "context_id": result.context_id,
        "state": result.state, "text": result.text,
        "usage": result.usage, "details": result.details,
    }


def _agent_url(agent_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must contain only letters, digits, hyphen, or underscore")
    configured = os.environ.get("BRIDGE_AGENTS_JSON", "{}")
    try:
        mapping = json.loads(configured)
    except json.JSONDecodeError as exc:
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object")
    url = mapping.get(agent_id)
    if url is None:
        raise ValueError(f"Unknown agent profile {agent_id!r} in BRIDGE_AGENTS_JSON")
    if not isinstance(url, str):
        raise ValueError("Agent URL must be a string")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Agent URL must use local HTTP loopback")
    return url.rstrip("/")


@mcp.tool()
async def ask_agent(
    agent_id: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    tool_policy: str | None = None,
) -> dict:
    """Ask a configured agent profile from BRIDGE_AGENTS_JSON."""
    result = await BridgeClient().ask(
        _agent_url(agent_id), prompt, model=model,
        reasoning_effort=reasoning_effort, tool_policy=tool_policy,
    )
    return {
        "task_id": result.task_id, "context_id": result.context_id,
        "state": result.state, "text": result.text,
        "usage": result.usage, "details": result.details,
    }


@mcp.tool()
async def get_agent_info(agent_id: str) -> dict:
    """Read capabilities and quota information for a configured agent profile."""
    return await BridgeClient().info(_agent_url(agent_id))


@mcp.tool()
async def ask_antigravity(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    workspace: str | None = None,
    tool_policy: str | None = None,
    turn_timeout_seconds: float = 300,
) -> dict:
    """Delegate to Antigravity; workspace starts an isolated temporary Bridge."""
    selected_workspace = workspace if workspace is not None else os.environ.get("BRIDGE_ANTIGRAVITY_WORKSPACE")
    if selected_workspace is not None:
        return await _managed_antigravity(
            prompt, selected_workspace, model, reasoning_effort,
            tool_policy, turn_timeout_seconds,
        )
    if turn_timeout_seconds != 300:
        raise ValueError("turn_timeout_seconds requires a managed Antigravity workspace")
    return await _ask("BRIDGE_ANTIGRAVITY_URL", prompt, model, reasoning_effort, tool_policy)


@mcp.tool()
async def ask_codex(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Delegate to Codex. model and reasoning_effort select its thread settings."""
    return await _ask("BRIDGE_CODEX_URL", prompt, model, reasoning_effort)


@mcp.tool()
async def get_antigravity_info(workspace: str | None = None) -> dict:
    """Read Antigravity's current models, effort options and account quota without an agent turn."""
    selected_workspace = workspace if workspace is not None else os.environ.get("BRIDGE_ANTIGRAVITY_WORKSPACE")
    if selected_workspace is not None:
        root = Path(selected_workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        async with connect_harness(HarnessLaunch("antigravity", _free_local_url(), root)) as peer:
            return await BridgeClient().info(peer.url)
    url = os.environ.get("BRIDGE_ANTIGRAVITY_URL")
    if not url:
        raise RuntimeError("Set BRIDGE_ANTIGRAVITY_URL to the local A2A server URL")
    return await BridgeClient().info(url)


@mcp.tool()
async def get_codex_info() -> dict:
    """Read Codex's current models, supported efforts and account quota without an agent turn."""
    url = os.environ.get("BRIDGE_CODEX_URL")
    if not url:
        raise RuntimeError("Set BRIDGE_CODEX_URL to the local A2A server URL")
    return await BridgeClient().info(url)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
