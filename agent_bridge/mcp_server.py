"""MCP tools that delegate to the A2A agents."""

from __future__ import annotations

import os
import json
import re
import socket
import sys
from dataclasses import replace
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from .client import BridgeClient
from .managed import HarnessConfigurationMismatch, HarnessLaunch, connect_harness
from .mcp_tasks import TaskGateway


_task_gateway: TaskGateway | None = None


def _gateway() -> TaskGateway:
    global _task_gateway
    if _task_gateway is None:
        root = Path(os.environ.get("BRIDGE_WORKSPACE") or os.getcwd())
        registry = Path(os.environ.get("BRIDGE_TASK_REGISTRY") or root / ".agent-shuttle" / "mcp-tasks.json")
        _task_gateway = TaskGateway(registry)
    return _task_gateway


@asynccontextmanager
async def _lifespan(server):
    global _task_gateway
    try:
        yield {}
    finally:
        if _task_gateway is not None:
            await _task_gateway.close()
            _task_gateway = None


def _debug(stage: str) -> None:
    if os.environ.get("BRIDGE_DEBUG") == "1":
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[agent-shuttle] {stamp} {stage}", file=sys.stderr, flush=True)


mcp = FastMCP(
    "Agent Shuttle",
    lifespan=_lifespan,
    instructions=(
        "Handle delegation setup yourself; users need not choose access policies. Use read_only "
        "for reviews/research, workspace_write for requested file edits, and no_tools for supplied-context "
        "generation when supported. Full_access requires authorization for unrestricted tools. "
        "Choose the workspace and least sufficient enforceable tool policy for the delegated task. "
        "Use get_agent_info to inspect supported_tool_policies, default_tool_policy and "
        "tool_policy_notes before choosing a policy. Never substitute full_access for an "
        "unsupported restrictive policy. Built-in Codex/Antigravity MCP calls default to "
        "read_only; configured profiles use their own defaults. Inspect each default before use. "
        "Use ask_agent for Codex, Antigravity, OpenCode, Claude Code, or configured profiles. "
        "For long tasks use submit_task, then check_task/wait_task; obtain paged output with "
        "get_result/get_transcript. A wait timeout does not cancel execution; cancel_task does. "
        "The legacy ask_antigravity and ask_codex tools remain available. "
        "Use get_antigravity_info or get_codex_info to check current models, efforts and account quotas. "
        "Each call starts a new remote task. Omit model unless the user explicitly names "
        "a model for the remote agent; never copy the caller's model or a configured "
        "selected_model into this parameter. When the user names a remote model, "
        "pass that model ID exactly. When the user names a reasoning effort, "
        "pass it exactly in reasoning_effort. "
        "Return the remote result to the user."
    ),
)


def _free_local_url() -> str:
    """Choose an isolated loopback port for a per-call managed Bridge."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def _workspace(value: str | None) -> Path:
    root = Path(value or os.environ.get("BRIDGE_WORKSPACE") or os.getcwd()).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    return root


def _local_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Agent URL must be a local HTTP loopback host and port")
    return url.rstrip("/")


def _agent_mapping() -> dict:
    try:
        mapping = json.loads(os.environ.get("BRIDGE_AGENTS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object")
    return mapping


def _legacy_custom_url(agent_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must contain only letters, digits, hyphen, or underscore")
    entry = _agent_mapping().get(agent_id)
    if agent_id not in {"codex", "antigravity", "opencode", "claude_code"} and isinstance(entry, str):
        return _local_url(entry)
    return None


def _agent_launch(agent_id: str, workspace: str | None = None,
                  model: str | None = None, tool_policy: str | None = None,
                  turn_timeout_seconds: float = 300) -> HarnessLaunch:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must contain only letters, digits, hyphen, or underscore")
    mapping = _agent_mapping()
    entry = mapping.get(agent_id, {})
    if isinstance(entry, str):
        entry = {"url": entry}
    if not isinstance(entry, dict):
        raise ValueError("Agent configuration must be an object or local URL")
    name = entry.get("harness", agent_id)
    if name not in {"codex", "antigravity", "opencode", "claude_code"}:
        raise ValueError(f"Configure harness for agent profile {agent_id!r} in BRIDGE_AGENTS_JSON")
    default_url = os.environ.get(f"BRIDGE_{name.upper()}_URL") if agent_id == name else None
    url = _local_url(entry.get("url") or default_url or _free_local_url())
    root = _workspace(workspace or entry.get("workspace") or os.environ.get(f"BRIDGE_{name.upper()}_WORKSPACE"))
    profile = entry.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ValueError("Agent profile must be a nonempty path")
    return HarnessLaunch(
        name, url, root, model=model,
        profile_path=Path(profile) if profile else None,
        tool_policy=(tool_policy if tool_policy is not None else entry.get("default_tool_policy")
                     or ("read_only" if name in {"codex", "antigravity"} else None)),
        agy_turn_timeout_seconds=turn_timeout_seconds,
    )


async def _managed_ask(launch: HarnessLaunch, prompt: str,
                       model: str | None, reasoning_effort: str | None,
                       tool_policy: str | None) -> dict:
    tool_policy = launch.tool_policy
    async def send(url: str) -> dict:
        result = await BridgeClient().ask(
            url, prompt, model=model, reasoning_effort=reasoning_effort,
            tool_policy=tool_policy,
        )
        _debug(f"{launch.name}: task returned {result.state}")
        return {
            "task_id": result.task_id, "context_id": result.context_id,
            "state": result.state, "text": result.text,
            "usage": result.usage, "details": result.details,
        }

    async def use(active: HarnessLaunch) -> dict:
        async with connect_harness(active) as peer:
            if active.name == "codex" and model and not getattr(peer, "started", True):
                try:
                    capabilities = await BridgeClient().capabilities(peer.url)
                    listed = {
                        item.get("id") for item in capabilities.get("capabilities", {}).get("models", [])
                        if isinstance(item, dict)
                    }
                except Exception as exc:
                    _debug(f"codex: existing model catalog unavailable ({type(exc).__name__})")
                    listed = set()
                if model not in listed:
                    _debug(f"codex: {model} absent from existing server catalog; starting isolated peer")
                    async with connect_harness(replace(active, url=_free_local_url())) as fresh:
                        return await send(fresh.url)
            _debug(f"{active.name}: bridge ready, sending task")
            return await send(peer.url)

    try:
        return await use(launch)
    except HarnessConfigurationMismatch:
        if not launch.start_if_missing:
            raise
        _debug(f"{launch.name}: existing peer is incompatible; starting an isolated peer")
        return await use(replace(launch, url=_free_local_url()))


@mcp.tool()
async def ask_agent(
    agent_id: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    tool_policy: str | None = None,
    workspace: str | None = None,
) -> dict:
    """Delegate; choose read_only for inspection or workspace_write for requested edits."""
    legacy_url = _legacy_custom_url(agent_id)
    if legacy_url:
        result = await BridgeClient().ask(
            legacy_url, prompt, model=model, reasoning_effort=reasoning_effort,
            tool_policy=tool_policy,
        )
        return {
            "task_id": result.task_id, "context_id": result.context_id,
            "state": result.state, "text": result.text,
            "usage": result.usage, "details": result.details,
        }
    launch = _agent_launch(agent_id, workspace, model, tool_policy)
    return await _managed_ask(launch, prompt, model, reasoning_effort, tool_policy)


@mcp.tool()
async def get_agent_info(agent_id: str, workspace: str | None = None) -> dict:
    """Read agent capabilities, launching its A2A server when absent."""
    legacy_url = _legacy_custom_url(agent_id)
    if legacy_url:
        return await BridgeClient().info(legacy_url)
    async with connect_harness(_agent_launch(agent_id, workspace)) as peer:
        return await BridgeClient().info(peer.url)


@mcp.tool()
async def ask_antigravity(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    workspace: str | None = None,
    tool_policy: str | None = None,
    turn_timeout_seconds: float = 300,
) -> dict:
    """Delegate to Antigravity. Default: read_only; choose workspace_write for file edits.

    Scoped policies enforce native file tools and block shell/MCP/subagents.
    Servers and per-conversation permission hooks are managed automatically.
    """
    launch = _agent_launch("antigravity", workspace, model, tool_policy, turn_timeout_seconds)
    return await _managed_ask(launch, prompt, model, reasoning_effort, tool_policy)


@mcp.tool()
async def ask_codex(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    workspace: str | None = None,
    tool_policy: str | None = None,
) -> dict:
    """Delegate to Codex, launching its A2A server when absent. Omit model unless requested."""
    launch = _agent_launch("codex", workspace, model, tool_policy)
    return await _managed_ask(launch, prompt, model, reasoning_effort, tool_policy)


@mcp.tool()
async def submit_task(agent_id: str, prompt: str, model: str | None = None,
                      reasoning_effort: str | None = None, tool_policy: str | None = None,
                      workspace: str | None = None, request_id: str | None = None) -> dict:
    """Start work and return a task ticket immediately; choose the least sufficient tool policy.

    The managed peer remains alive between calls. Reuse request_id for submission retries.
    Task records are stored in the workspace's .agent-shuttle directory.
    """
    launch = _agent_launch(agent_id, workspace, model, tool_policy)
    return await _gateway().submit(launch, prompt, model, reasoning_effort, request_id)


@mcp.tool()
async def check_task(task_id: str) -> dict:
    """Read the current task snapshot without waiting for the worker."""
    return asdict(await (await _gateway().handle(task_id)).status())


@mcp.tool()
async def wait_task(task_id: str, timeout_seconds: float = 180) -> dict:
    """Wait within a separate budget; expiry returns a snapshot and never cancels work."""
    return asdict(await (await _gateway().handle(task_id)).wait(timeout_seconds))


@mcp.tool()
async def cancel_task(task_id: str) -> dict:
    """Stop the delegated task and wait for backend cleanup; repeated cancellation is safe."""
    return asdict(await (await _gateway().handle(task_id)).cancel())


@mcp.tool()
async def get_result(task_id: str, cursor: int = 0, limit: int = 60000) -> dict:
    """Read a bounded result page; follow next_cursor until it is null."""
    return await (await _gateway().handle(task_id)).result_page(cursor, limit)


@mcp.tool()
async def get_transcript(task_id: str, cursor: int = 0, limit: int = 100) -> dict:
    """Read a page of task messages, progress metadata and artifacts."""
    return await (await _gateway().handle(task_id)).transcript(cursor, limit)


@mcp.tool()
async def get_antigravity_info(workspace: str | None = None) -> dict:
    """Read Antigravity's current models, effort options and account quota without an agent turn."""
    async with connect_harness(_agent_launch("antigravity", workspace)) as peer:
        return await BridgeClient().info(peer.url)


@mcp.tool()
async def get_codex_info(workspace: str | None = None) -> dict:
    """Read Codex's current models, supported efforts and account quota without an agent turn."""
    async with connect_harness(_agent_launch("codex", workspace)) as peer:
        return await BridgeClient().info(peer.url)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
