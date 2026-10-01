"""Long-lived MCP task ownership with restartable task tickets."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlparse

from .client import BridgeClient, _TERMINAL_STATES
from .managed import HarnessConfigurationMismatch, HarnessLaunch, connect_harness


class TaskGateway:
    def __init__(self, registry_path: Path, *, client: BridgeClient | None = None):
        self.path = registry_path
        self.client = client or BridgeClient()
        self.stack = AsyncExitStack()
        self.lock = asyncio.Lock()
        self.jobs: dict = {}
        self.peers: dict = {}
        self.loaded = False
        self.closed = False

    def _load(self):
        if not self.loaded:
            if self.path.exists():
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if document.get("version") != 1 or not isinstance(document.get("jobs"), dict):
                    raise ValueError("Unsupported MCP task registry")
                self.jobs = document["jobs"]
            self.loaded = True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".next.json")
        temporary.write_text(json.dumps({"version": 1, "jobs": self.jobs}, default=str), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _key(launch):
        if launch.name in {"codex", "antigravity"}:
            # Scoped policies and models belong to each turn, not to the peer.
            return (launch.name, str(launch.workspace), launch.tool_policy == "full_access")
        return (launch.name, str(launch.workspace), str(launch.profile_path), launch.tool_policy, launch.model)

    async def _peer(self, launch):
        key = self._key(launch)
        if key in self.peers:
            return self.peers[key]
        try:
            peer = await self.stack.enter_async_context(connect_harness(launch))
        except HarnessConfigurationMismatch:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                launch = replace(launch, url=f"http://127.0.0.1:{listener.getsockname()[1]}")
            peer = await self.stack.enter_async_context(connect_harness(launch))
        launch = replace(launch, url=peer.url)
        self.peers[key] = (launch, peer)
        return launch, peer

    async def submit(self, launch, prompt, model, reasoning_effort, request_id):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Task gateway is closed")
            self._load()
            launch = replace(launch, task_db=launch.workspace / ".agent-shuttle" / f"tasks-{launch.name}.sqlite3")
            active, peer = await self._peer(launch)
            handle = await self.client.submit(peer.url, prompt, model, reasoning_effort=reasoning_effort,
                                              tool_policy=launch.tool_policy, request_id=request_id)
            self.jobs[handle.task_id] = {"launch": asdict(active), "context_id": handle.context_id}
            self._save()
            return {"task_id": handle.task_id, "context_id": handle.context_id, "peer": peer.url}

    async def handle(self, task_id):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Task gateway is closed")
            self._load()
            if task_id not in self.jobs:
                raise ValueError("Unknown task_id; use the ticket returned by submit_task")
            data = dict(self.jobs[task_id]["launch"])
            parsed = urlparse(data["url"])
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Task registry peer must be local HTTP")
            for key in ("workspace", "profile_path", "log_path", "task_db"):
                if data.get(key) is not None:
                    data[key] = Path(data[key])
            active, peer = await self._peer(HarnessLaunch(**data))
            self.jobs[task_id]["launch"] = asdict(active)
            self._save()
            return self.client.task(peer.url, task_id)

    async def close(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            active_urls = {peer.url for _, peer in self.peers.values()}
            for task_id, job in self.jobs.items():
                url = job["launch"]["url"]
                if url not in active_urls:
                    continue
                try:
                    async with asyncio.timeout(5):
                        if (await self.client.task_status(url, task_id)).state not in _TERMINAL_STATES:
                            await self.client.cancel_task(url, task_id)
                except Exception:
                    # Closing owned peer contexts still terminates their process trees.
                    pass
            await self.stack.aclose()
            self.peers.clear()
