"""Python task lifecycle API for Agent Shuttle and task-capable A2A peers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import (
    CancelTaskRequest, GetTaskRequest, Role, SendMessageConfiguration,
    SendMessageRequest, SubscribeToTaskRequest, TaskState,
)
from a2a.utils.errors import TaskNotCancelableError

from .profiles import ToolPolicy


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    usage: dict[str, int] | None = None
    details: dict | None = None


@dataclass(frozen=True)
class BridgeEvent:
    kind: str
    task_id: str
    state: str | None = None
    text: str = ""


_TERMINAL_STATES = frozenset({
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
})


@dataclass(frozen=True)
class TaskHandle:
    """A task identifier that remains usable after the submitting call returns."""

    client: "BridgeClient"
    peer_url: str
    task_id: str
    context_id: str | None

    async def status(self) -> BridgeResult:
        return await self.client.task_status(self.peer_url, self.task_id)

    async def wait(self, timeout: float | None = None) -> BridgeResult:
        """Wait for a settled task; a wait timeout never cancels execution."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else monotonic() + timeout
        last = BridgeResult(self.peer_url, self.task_id, self.context_id,
                            "TASK_STATE_SUBMITTED", "")
        while True:
            if deadline is None:
                result = await self.status()
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return last
                try:
                    result = await asyncio.wait_for(self.status(), timeout=remaining)
                except asyncio.TimeoutError:
                    return last
            last = result
            if result.state in _TERMINAL_STATES:
                return result
            if deadline is not None and monotonic() >= deadline:
                return result
            delay = 0.2 if deadline is None else min(0.2, max(0, deadline - monotonic()))
            await asyncio.sleep(delay)

    async def result(self) -> BridgeResult:
        return await self.wait()

    async def cancel(self) -> BridgeResult:
        return await self.client.cancel_task(self.peer_url, self.task_id)

    async def events(self) -> AsyncIterator[BridgeEvent]:
        """Observe live A2A updates; use status() to recover after disconnect."""
        async for event in self.client.task_events(self.peer_url, self.task_id):
            yield event


class BridgeClient:
    def __init__(self, timeout_seconds: float = 1800):
        self.timeout_seconds = timeout_seconds

    def task(self, peer_url: str, task_id: str) -> TaskHandle:
        """Reopen a live-server task using the ID returned by submit()."""
        if not task_id:
            raise ValueError("task_id must be nonempty")
        return TaskHandle(self, peer_url, task_id, None)

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

    async def submit(
        self, peer_url: str, prompt: str, model: str | None = None, *,
        reasoning_effort: str | None = None, read_only: bool = False,
        tool_policy: str | None = None, session_id: str | None = None,
        request_id: str | None = None,
    ) -> TaskHandle:
        """Start an A2A task and return its handle before the agent finishes."""
        message = _request_message(prompt, model, reasoning_effort, read_only,
                                   tool_policy, session_id, request_id)
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                last = None
                async for item in client.send_message(request):
                    last = item
                if last is None or last.WhichOneof("payload") != "task":
                    raise RuntimeError("A2A agent did not return a task")
                task = last.task
                return TaskHandle(self, peer_url, task.id, task.context_id or None)
            finally:
                await client.close()

    async def _task_call(self, peer_url: str, method: str, request) -> BridgeResult:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                task = await getattr(client, method)(request)
                return _task_result(peer_url, task)
            finally:
                await client.close()

    async def task_status(self, peer_url: str, task_id: str) -> BridgeResult:
        return await self._task_call(peer_url, "get_task", GetTaskRequest(id=task_id))

    async def cancel_task(self, peer_url: str, task_id: str) -> BridgeResult:
        try:
            return await self._task_call(peer_url, "cancel_task", CancelTaskRequest(id=task_id))
        except TaskNotCancelableError:
            state = await self.task_status(peer_url, task_id)
            if state.state == "TASK_STATE_CANCELED":
                return state
            raise

    async def task_events(self, peer_url: str, task_id: str) -> AsyncIterator[BridgeEvent]:
        async with httpx.AsyncClient(timeout=None) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=True, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                async for item in client.subscribe(SubscribeToTaskRequest(id=task_id)):
                    which = item.WhichOneof("payload")
                    if which == "task":
                        yield BridgeEvent("task", task_id,
                                          TaskState.Name(item.task.status.state))
                    elif which == "status_update":
                        status = item.status_update.status
                        yield BridgeEvent("status", task_id, TaskState.Name(status.state),
                                          _parts(status.message.parts) if status.HasField("message") else "")
                    elif which == "artifact_update":
                        yield BridgeEvent("artifact", task_id, text=_parts(item.artifact_update.artifact.parts))
            finally:
                await client.close()

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
        request_id: str | None = None,
    ) -> BridgeResult:
        submission = asyncio.create_task(self.submit(
            peer_url, prompt, model, reasoning_effort=reasoning_effort,
            read_only=read_only, tool_policy=tool_policy, session_id=session_id,
            request_id=request_id,
        ))
        try:
            # Keep the submission alive long enough to learn the task ID even
            # if the caller is cancelled as the backend begins its turn.
            handle = await asyncio.shield(submission)
        except asyncio.CancelledError:
            try:
                handle = await asyncio.wait_for(submission, timeout=min(10, self.timeout_seconds))
                await asyncio.wait_for(handle.cancel(), timeout=min(10, self.timeout_seconds))
            except Exception:
                log.exception("Could not cancel a task after ask() was interrupted during submit")
            raise
        try:
            result = await handle.wait(timeout=self.timeout_seconds)
            if result.state not in _TERMINAL_STATES:
                raise TimeoutError(f"Task {handle.task_id} did not finish within {self.timeout_seconds:g}s")
            return result
        except (asyncio.CancelledError, TimeoutError):
            # With an explicit task ID, the blocking convenience API can stop
            # the remote turn instead of leaving it orphaned after its caller
            # is cancelled. submit() itself remains detached by design.
            try:
                await asyncio.wait_for(handle.cancel(), timeout=min(10, self.timeout_seconds))
            except Exception:
                log.exception("Could not cancel task %s after ask() stopped waiting", handle.task_id)
            raise


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
        self._tainted = False
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
            if self._tainted:
                raise RuntimeError("Bridge session was cancelled; start a new session")
            self._started = True
            try:
                return await self.client.ask(
                    self.peer_url,
                    prompt,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                    read_only=self.read_only,
                    tool_policy=self.tool_policy,
                    session_id=self.id,
                )
            except (asyncio.CancelledError, TimeoutError):
                self._tainted = True
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._started:
                await self.client.close_session(self.peer_url, self.id)


def _parts(parts) -> str:
    return "\n".join(part.text for part in parts if part.WhichOneof("content") == "text")


def _request_message(prompt, model, reasoning_effort, read_only, tool_policy, session_id,
                     request_id=None):
    if not isinstance(prompt, str) or not prompt.strip():
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
    if request_id is not None:
        request_id = str(uuid.UUID(request_id))
    message = new_text_message(prompt, context_id=session_id, role=Role.ROLE_USER)
    if request_id is not None:
        message.message_id = request_id
        message.metadata["agent_bridge.request_id"] = request_id
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
    return message


def _task_result(peer_url, task) -> BridgeResult:
    state = TaskState.Name(task.status.state)
    content = "\n".join(_parts(artifact.parts) for artifact in task.artifacts).strip()
    usage = None
    details = None
    for artifact in task.artifacts:
        if artifact.HasField("metadata"):
            artifact_metadata = MessageToDict(artifact.metadata)
            candidate = artifact_metadata.get("agent_bridge.usage")
            if isinstance(candidate, dict):
                usage = {
                    key: int(value) for key, value in candidate.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and value >= 0 and float(value).is_integer()
                }
            candidate_details = artifact_metadata.get("agent_bridge.details")
            if isinstance(candidate_details, dict):
                details = candidate_details
    if not content and task.status.HasField("message"):
        content = _parts(task.status.message.parts)
    return BridgeResult(peer_url, task.id, task.context_id, state, content, usage, details)
