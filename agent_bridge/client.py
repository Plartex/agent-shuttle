"""Small Python API for sending text tasks to any A2A 1.x agent."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest, TaskState

from .profiles import ToolPolicy


@dataclass(frozen=True)
class BridgeResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    usage: dict[str, int] | None = None
    details: dict | None = None


class BridgeClient:
    def __init__(self, timeout_seconds: float = 1800):
        self.timeout_seconds = timeout_seconds

    async def _get(self, peer_url: str, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            response = await http.get(peer_url.rstrip("/") + path)
            response.raise_for_status()
            return response.json()

    async def info(self, peer_url: str) -> dict:
        """Read the peer's current model catalog, effort options, and account quotas."""
        return await self._get(peer_url, "/bridge/info")

    async def identity(self, peer_url: str) -> dict:
        """Read local server identity and safety mode without starting a model CLI."""
        try:
            return await asyncio.wait_for(
                self._get(peer_url, "/bridge/identity"),
                timeout=min(self.timeout_seconds, 10.0),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(
                    f"Bridge at {peer_url} lacks /bridge/identity; restart it with the current version"
                ) from exc
            raise

    async def capabilities(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/bridge/capabilities")

    async def usage(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/bridge/usage")

    def session(
        self,
        peer_url: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> "BridgeSession":
        """Create an isolated conversation; use with ``async with`` for cleanup."""
        return BridgeSession(self, peer_url, model, reasoning_effort, read_only, tool_policy)

    async def close_session(self, peer_url: str, session_id: str) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            response = await http.delete(
                peer_url.rstrip("/") + "/bridge/sessions/" + str(uuid.UUID(session_id))
            )
            response.raise_for_status()
            return bool(response.json()["closed"])

    async def ask(
        self,
        peer_url: str,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        session_id: str | None = None,
    ) -> BridgeResult:
        if not prompt.strip():
            raise ValueError("prompt must contain text")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty string when provided")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a nonempty string when provided")
        if tool_policy is not None:
            try:
                ToolPolicy(tool_policy)
            except ValueError as exc:
                raise ValueError("tool_policy must be no_tools, read_only, workspace_write, or full_access") from exc
            if read_only and tool_policy != ToolPolicy.READ_ONLY.value:
                raise ValueError("read_only conflicts with tool_policy")
        if session_id is not None:
            session_id = str(uuid.UUID(session_id))
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                message = new_text_message(
                    prompt, context_id=session_id, role=Role.ROLE_USER
                )
                if model is not None:
                    message.metadata["agent_bridge.model"] = model.strip()
                if reasoning_effort is not None:
                    message.metadata["agent_bridge.reasoning_effort"] = reasoning_effort.strip()
                if read_only:
                    message.metadata["agent_bridge.read_only"] = True
                if tool_policy is not None:
                    message.metadata["agent_bridge.tool_policy"] = tool_policy
                if session_id is not None:
                    message.metadata["agent_bridge.session_id"] = session_id
                request = SendMessageRequest(message=message)
                last = None
                async for item in client.send_message(request):
                    last = item
                if last is None:
                    raise RuntimeError("A2A agent returned no response")
                which = last.WhichOneof("payload")
                if which == "message":
                    return BridgeResult(peer_url, None, last.message.context_id or None, "message", _parts(last.message.parts))
                if which != "task":
                    raise RuntimeError(f"Unexpected A2A response: {which}")
                task = last.task
                state = TaskState.Name(task.status.state)
                text = "\n".join(_parts(artifact.parts) for artifact in task.artifacts).strip()
                usage = None
                details = None
                for artifact in task.artifacts:
                    if artifact.HasField("metadata"):
                        artifact_metadata = MessageToDict(artifact.metadata)
                        candidate = artifact_metadata.get("agent_bridge.usage")
                        if isinstance(candidate, dict):
                            usage = {
                                key: int(value) for key, value in candidate.items()
                                if isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                and value >= 0
                                and float(value).is_integer()
                            }
                        candidate_details = artifact_metadata.get("agent_bridge.details")
                        if isinstance(candidate_details, dict):
                            details = candidate_details
                if not text and task.status.HasField("message"):
                    text = _parts(task.status.message.parts)
                return BridgeResult(peer_url, task.id, task.context_id, state, text, usage, details)
            finally:
                await client.close()


class BridgeSession:
    """Reusable per-agent conversation with explicit lifetime and pinned settings."""

    def __init__(
        self,
        client: BridgeClient,
        peer_url: str,
        model: str | None,
        reasoning_effort: str | None,
        read_only: bool,
        tool_policy: str | None,
    ):
        self.client = client
        self.peer_url = peer_url
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.read_only = read_only
        self.tool_policy = tool_policy
        self.id = str(uuid.uuid4())
        self._closed = False
        self._started = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "BridgeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def ask(self, prompt: str) -> BridgeResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Bridge session is closed")
            self._started = True
            return await self.client.ask(
                self.peer_url,
                prompt,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                read_only=self.read_only,
                tool_policy=self.tool_policy,
                session_id=self.id,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._started:
                await self.client.close_session(self.peer_url, self.id)


def _parts(parts) -> str:
    return "\n".join(part.text for part in parts if part.WhichOneof("content") == "text")
