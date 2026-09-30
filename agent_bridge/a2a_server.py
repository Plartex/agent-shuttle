"""A2A 1.x server for either local coding agent."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from pathlib import Path

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from a2a.utils.errors import TaskNotCancelableError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backends import (
    AntigravityCliBackend, AntigravitySdkBackend, Backend, BackendResponse,
    BackendSession, CodexBackend,
)
from .info import InfoProvider
from .profiled import ProfiledBackend
from .profiles import ToolPolicy


@dataclass
class _SessionRecord:
    backend: BackendSession
    settings: tuple[str | None, str | None, bool, str | None]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=monotonic)
    closed: bool = False


class SessionManager:
    """Own backend conversations; one active turn at a time per session."""

    def __init__(self, backend: Backend, idle_seconds: float = 1800):
        self.backend = backend
        self.idle_seconds = idle_seconds
        self.sessions: dict[str, _SessionRecord] = {}
        self.lock = asyncio.Lock()

    async def run(
        self,
        session_id: str,
        prompt: str,
        model: str | None,
        reasoning_effort: str | None,
        read_only: bool,
        tool_policy: str | None = None,
    ) -> str | BackendResponse:
        settings = (model, reasoning_effort, read_only, tool_policy)
        async with self.lock:
            record = self.sessions.get(session_id)
            if record is None:
                kwargs = {"reasoning_effort": reasoning_effort, "read_only": read_only}
                if tool_policy is not None:
                    kwargs["tool_policy"] = tool_policy
                session = await self.backend.open_session(model, **kwargs)
                record = _SessionRecord(session, settings)
                self.sessions[session_id] = record
            elif record.settings != settings:
                raise ValueError("Model, reasoning effort, and tool policy cannot change within a session")
            record.last_used = monotonic()
        async with record.lock:
            if record.closed:
                raise RuntimeError("Session was closed during a concurrent request")
            record.last_used = monotonic()
            try:
                return await record.backend.ask(prompt)
            finally:
                record.last_used = monotonic()

    async def close(self, session_id: str) -> bool:
        async with self.lock:
            record = self.sessions.pop(session_id, None)
        if record is None:
            return False
        async with record.lock:
            record.closed = True
            await record.backend.close()
        return True

    async def reap_idle(self) -> None:
        cutoff = monotonic() - self.idle_seconds
        async with self.lock:
            expired = [
                (session_id, record) for session_id, record in self.sessions.items()
                if record.last_used < cutoff and not record.lock.locked()
            ]
            for session_id, _ in expired:
                self.sessions.pop(session_id)
        for _, record in expired:
            async with record.lock:
                record.closed = True
                await record.backend.close()

    async def close_all(self) -> None:
        async with self.lock:
            session_ids = list(self.sessions)
        for session_id in session_ids:
            await self.close(session_id)


class BridgeExecutor(AgentExecutor):
    def __init__(self, backend: Backend, sessions: SessionManager):
        self.backend = backend
        self.sessions = sessions

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = get_message_text(context.message).strip()
        if not prompt and not context.current_task:
            await event_queue.enqueue_event(new_text_message("A text task is required"))
            return
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        if not prompt:
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("A text task is required"),
            )
            return
        model = None
        if "agent_bridge.model" in context.message.metadata:
            model = context.message.metadata["agent_bridge.model"]
            if not isinstance(model, str) or not model.strip():
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_bridge.model must be a nonempty string"),
                )
                return
            model = model.strip()
        reasoning_effort = None
        if "agent_bridge.reasoning_effort" in context.message.metadata:
            reasoning_effort = context.message.metadata["agent_bridge.reasoning_effort"]
            if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_bridge.reasoning_effort must be a nonempty string"),
                )
                return
            reasoning_effort = reasoning_effort.strip()
        read_only = False
        if "agent_bridge.read_only" in context.message.metadata:
            read_only = context.message.metadata["agent_bridge.read_only"]
        if not isinstance(read_only, bool):
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("agent_bridge.read_only must be a boolean"),
            )
            return
        tool_policy = None
        if "agent_bridge.tool_policy" in context.message.metadata:
            tool_policy = context.message.metadata["agent_bridge.tool_policy"]
            if not isinstance(tool_policy, str) or tool_policy not in {p.value for p in ToolPolicy}:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_bridge.tool_policy is invalid"),
                )
                return
            if read_only and tool_policy != ToolPolicy.READ_ONLY.value:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("read_only conflicts with tool_policy"),
                )
                return
        session_id = (
            context.message.metadata["agent_bridge.session_id"]
            if "agent_bridge.session_id" in context.message.metadata else None
        )
        if session_id is not None:
            try:
                session_id = str(uuid.UUID(session_id))
            except (TypeError, ValueError, AttributeError):
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_bridge.session_id must be a UUID"),
                )
                return
            if context.message.context_id != session_id:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("session_id must match the A2A context_id"),
                )
                return
        await updater.update_status(TaskState.TASK_STATE_WORKING)
        try:
            if session_id is None:
                kwargs = {"reasoning_effort": reasoning_effort, "read_only": read_only}
                if tool_policy is not None:
                    kwargs["tool_policy"] = tool_policy
                answer = await self.backend.run(
                    prompt, model, **kwargs,
                )
            else:
                answer = await self.sessions.run(
                    session_id, prompt, model, reasoning_effort, read_only, tool_policy,
                )
        except Exception as exc:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message(f"{type(exc).__name__}: {exc}"),
            )
            return
        metadata = None
        if isinstance(answer, BackendResponse):
            metadata = {}
            if answer.usage:
                metadata["agent_bridge.usage"] = answer.usage
            if answer.details:
                metadata["agent_bridge.details"] = answer.details
            metadata = metadata or None
            answer = answer.text
        await updater.add_artifact(
            [new_text_part(answer, media_type="text/plain")],
            name="result",
            metadata=metadata,
        )
        await updater.update_status(TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # One-shot backend calls cannot currently be cancelled safely via A2A.
        raise TaskNotCancelableError()


def make_app(name: str, backend: Backend, url: str, info_provider: InfoProvider | None = None) -> Starlette:
    sessions = SessionManager(backend)
    skill = AgentSkill(
        id=f"run_{name}",
        name=f"Run {name} task",
        description=(
            f"Delegate a coding task to the local {name} agent and return its result. "
            "Optional message metadata agent_bridge.model and agent_bridge.reasoning_effort "
            "select the backend model and reasoning effort."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["coding", "delegation", name],
    )
    card = AgentCard(
        name=f"{name.title()} local agent",
        description=(
            f"Local {name} agent exposed through A2A by Agent Shuttle. "
            "Live models, reasoning efforts and account quotas are available at /bridge/info."
        ),
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[skill],
    )
    handler = DefaultRequestHandler(
        agent_executor=BridgeExecutor(backend, sessions),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    def identity_data() -> dict:
        if isinstance(backend, ProfiledBackend):
            backend_name = backend.profile.runtime
            workspace = backend.profile.workspace
        else:
            backend_name = (
                "codex_app_server" if isinstance(backend, CodexBackend) else
                "agy_cli" if isinstance(backend, AntigravityCliBackend) else
                "antigravity_sdk" if isinstance(backend, AntigravitySdkBackend) else name
            )
            workspace = getattr(backend, "workspace", None)
        result = {"agent": name, "backend": backend_name, "pid": os.getpid()}
        if isinstance(backend, ProfiledBackend):
            result["max_tool_policy"] = backend.profile.max_tool_policy.value
        if isinstance(workspace, Path):
            result["workspace"] = str(workspace.resolve(strict=True))
        result["read_only_tools"] = (
            isinstance(backend, CodexBackend)
            or isinstance(backend, ProfiledBackend)
            and backend.profile.max_tool_policy in {
                ToolPolicy.READ_ONLY, ToolPolicy.WORKSPACE_WRITE, ToolPolicy.FULL_ACCESS,
            }
        )
        if isinstance(backend, AntigravityCliBackend):
            result["agy_permission_mode"] = (
                "all" if backend.dangerously_skip_permissions else "settings"
            )
            result["agy_turn_timeout_seconds"] = backend.turn_timeout_seconds
        return result

    async def bridge_identity(request):
        return JSONResponse(identity_data())

    async def bridge_info(request):
        if info_provider is None:
            return JSONResponse({"error": "Info provider is not configured"}, status_code=503)
        path = request.url.path
        capabilities = path != "/bridge/usage"
        usage = path != "/bridge/capabilities"
        try:
            result = await info_provider.fetch(capabilities=capabilities, usage=usage)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=503)
        if capabilities:
            result.update(identity_data())
        return JSONResponse(result)

    async def close_session(request):
        try:
            session_id = str(uuid.UUID(request.path_params["session_id"]))
        except ValueError:
            return JSONResponse({"error": "session_id must be a UUID"}, status_code=400)
        return JSONResponse({"closed": await sessions.close(session_id)})

    @asynccontextmanager
    async def lifespan(app):
        async def reap_loop():
            while True:
                await asyncio.sleep(60)
                await sessions.reap_idle()

        janitor = asyncio.create_task(reap_loop())
        try:
            yield
        finally:
            janitor.cancel()
            try:
                await janitor
            except asyncio.CancelledError:
                pass
            await sessions.close_all()
            shutdown = getattr(backend, "close", None)
            if shutdown is not None:
                await shutdown()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/bridge/identity", bridge_identity),
            Route("/bridge/info", bridge_info),
            Route("/bridge/capabilities", bridge_info),
            Route("/bridge/usage", bridge_info),
            Route("/bridge/sessions/{session_id}", close_session, methods=["DELETE"]),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
        ]
    )
