"""A2A 1.x server for either local coding agent."""

from __future__ import annotations

import asyncio
import os
import inspect
import math
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
from a2a.utils.errors import InvalidParamsError
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
from .task_library import TaskManager


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
        self.cancelled_sessions: set[str] = set()
        self.lock = asyncio.Lock()

    async def run(
        self,
        session_id: str,
        prompt: str,
        model: str | None,
        reasoning_effort: str | None,
        read_only: bool,
        tool_policy: str | None = None,
        on_event=None,
    ) -> str | BackendResponse:
        settings = (model, reasoning_effort, read_only, tool_policy)
        async with self.lock:
            if session_id in self.cancelled_sessions:
                raise RuntimeError("Session was closed after cancellation; start a new session")
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
                kwargs = {"on_event": on_event} if "on_event" in inspect.signature(record.backend.ask).parameters else {}
                return await record.backend.ask(prompt, **kwargs)
            except (asyncio.CancelledError, TimeoutError):
                # The provider may have performed side effects before its turn
                # was interrupted. Never continue that same native conversation.
                record.closed = True
                self.cancelled_sessions.add(session_id)
                try:
                    await record.backend.close()
                finally:
                    raise
            finally:
                record.last_used = monotonic()

    async def close(self, session_id: str) -> bool:
        async with self.lock:
            record = self.sessions.pop(session_id, None)
        if record is None:
            return False
        async with record.lock:
            if not record.closed:
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
                if not record.closed:
                    record.closed = True
                    await record.backend.close()

    async def close_all(self) -> None:
        async with self.lock:
            session_ids = list(self.sessions)
        for session_id in session_ids:
            await self.close(session_id)


class BridgeExecutor(AgentExecutor):
    def __init__(self, task_manager: TaskManager, agent_id: str):
        self.task_manager = task_manager
        self.agent_id = agent_id

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
        await self._execute_library(task, updater, prompt, model, reasoning_effort,
                                    tool_policy or ("read_only" if read_only else None), session_id,
                                    (context.message.metadata["agent_bridge.request_id"]
                                     if "agent_bridge.request_id" in context.message.metadata else None))

    async def _execute_library(self, task, updater, prompt, model, reasoning_effort,
                               tool_policy, session_id, request_id):
        manager = self.task_manager
        assert manager is not None and self.agent_id is not None

        async def on_event(event):
            message = new_text_message(str(event.get("text", "")) or str(event.get("kind", "activity")))
            message.metadata["agent_bridge.event"] = event
            await updater.update_status(TaskState.TASK_STATE_WORKING, message)

        try:
            if session_id is not None:
                await manager.ensure_session(session_id, self.agent_id, model=model,
                                             reasoning_effort=reasoning_effort, tool_policy=tool_policy)
            core_task = await manager.dispatch(self.agent_id, prompt, model=model,
                                               reasoning_effort=reasoning_effort,
                                               tool_policy=tool_policy, session_id=session_id,
                                               request_id=request_id, task_id=task.id,
                                               event_sink=on_event)
        except Exception as exc:
            error = {"code": "backend_error", "type": type(exc).__name__,
                     "message": str(exc), "retryable": False}
            await updater.update_status(TaskState.TASK_STATE_FAILED,
                                        new_text_message(f"{type(exc).__name__}: {exc}"),
                                        metadata={"agent_bridge.error": error})
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING)
        try:
            result = await core_task.result()
        except asyncio.CancelledError:
            await core_task.cancel()
            raise
        if result.state == "completed":
            metadata = {}
            if result.usage:
                metadata["agent_bridge.usage"] = result.usage
            if result.details:
                metadata["agent_bridge.details"] = result.details
            await updater.add_artifact([new_text_part(result.text, media_type="text/plain")],
                                       name="result", metadata=metadata or None)
            await updater.update_status(TaskState.TASK_STATE_COMPLETED)
        elif result.state == "failed":
            error = result.error or {"code": "backend_error", "message": "Unknown worker error"}
            await updater.update_status(TaskState.TASK_STATE_FAILED,
                                        new_text_message(f"{error.get('type', 'Error')}: {error['message']}"),
                                        metadata={"agent_bridge.error": error})
        elif result.state == "canceled":
            await updater.update_status(TaskState.TASK_STATE_CANCELED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The A2A active-task manager cancels and joins the producer after this
        # hook returns. Backend coroutines own their native cleanup on
        # CancelledError; a terminal CANCELED state is written only after the
        # producer has finished winding down.
        return None


class _IdempotentRequestHandler(DefaultRequestHandler):
    """Deduplicate explicitly keyed submissions within this server lifetime."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._request_lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], tuple[bytes, asyncio.Task]] = {}

    async def on_message_send(self, params, context):
        request_id = (
            params.message.metadata["agent_bridge.request_id"]
            if "agent_bridge.request_id" in params.message.metadata else None
        )
        if request_id is None:
            return await super().on_message_send(params, context)
        try:
            request_id = str(uuid.UUID(request_id))
        except (TypeError, ValueError, AttributeError):
            raise InvalidParamsError("agent_bridge.request_id must be a UUID") from None
        if params.message.message_id != request_id:
            raise InvalidParamsError("message_id must match agent_bridge.request_id")
        fingerprint = params.SerializeToString(deterministic=True)
        key = (context.user.user_name, request_id)
        async with self._request_lock:
            previous = self._requests.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise InvalidParamsError("request_id is already bound to another request")
                submitted = previous[1]
            else:
                lookup = getattr(self.task_store, "request_task", None)
                if lookup is not None:
                    stored = await lookup(request_id, fingerprint, context)
                    if stored is not None:
                        return stored
                    task = new_task_from_user_message(params.message)
                    task_id, created = await self.task_store.bind_request(request_id, fingerprint, task, context)
                    if not created:
                        return await self.task_store.get(task_id, context)
                    params = type(params).FromString(params.SerializeToString())
                    params.message.task_id = task_id
                    params.message.context_id = task.context_id
                submitted = asyncio.create_task(super().on_message_send(params, context))
                self._requests[key] = (fingerprint, submitted)
        # A dropped HTTP caller must not abort the only copy of its task.
        return await asyncio.shield(submitted)


def make_app(name: str, backend: Backend, url: str, info_provider: InfoProvider | None = None,
             *, task_store=None, execution_timeout_seconds: float = 1800,
             stall_timeout_seconds: float = 1800) -> Starlette:
    for budget in (execution_timeout_seconds, stall_timeout_seconds):
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise ValueError("worker budgets must be positive finite numbers")
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
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[skill],
    )
    library_database = getattr(task_store, "path", None)
    if isinstance(library_database, Path):
        library_database = library_database.with_suffix(".library.sqlite3")
    backend_workspace = getattr(backend, "workspace", None)
    if not isinstance(backend_workspace, (str, Path)):
        backend_workspace = Path.cwd()
    manager = TaskManager({name: backend}, workspace=backend_workspace,
                          database=library_database, memory=library_database is None,
                          execution_timeout_seconds=execution_timeout_seconds,
                          stall_timeout_seconds=stall_timeout_seconds)
    executor = BridgeExecutor(manager, name)
    handler = _IdempotentRequestHandler(
        agent_executor=executor,
        task_store=task_store or InMemoryTaskStore(),
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
        database = getattr(handler.task_store, "path", None)
        result["task_storage"] = "sqlite" if isinstance(database, Path) else "memory"
        result["task_db_path"] = str(database) if isinstance(database, Path) else None
        result["supported_tool_policies"] = []
        result["default_tool_policy"] = None
        if isinstance(backend, ProfiledBackend):
            result["max_tool_policy"] = backend.profile.max_tool_policy.value
            policies = list(ToolPolicy)
            maximum = policies.index(backend.profile.max_tool_policy)
            result["supported_tool_policies"] = [p.value for p in policies[:maximum + 1]]
            result["default_tool_policy"] = backend.profile.default_tool_policy.value
        elif isinstance(backend, CodexBackend):
            result["supported_tool_policies"] = ["read_only", "workspace_write", "full_access"]
            result["default_tool_policy"] = "workspace_write"
        if isinstance(workspace, Path):
            result["workspace"] = str(workspace.resolve(strict=True))
        result["read_only_tools"] = (
            isinstance(backend, (CodexBackend, AntigravityCliBackend))
            or isinstance(backend, ProfiledBackend)
            and backend.profile.max_tool_policy in {
                ToolPolicy.READ_ONLY, ToolPolicy.WORKSPACE_WRITE, ToolPolicy.FULL_ACCESS,
            }
        )
        if isinstance(backend, AntigravityCliBackend):
            result["supported_tool_policies"] = ["no_tools", "read_only", "workspace_write"]
            result["tool_policy_enforcement"] = "agy_pre_tool_use"
            if backend.dangerously_skip_permissions:
                result["supported_tool_policies"].append("full_access")
                result["default_tool_policy"] = "full_access"
            result["tool_policy_notes"] = (
                ("This server auto-approves all tools. " if backend.dangerously_skip_permissions
                 else "With no explicit policy, agy uses its settings; headless workspace file writes may be allowed. ")
                + "Explicit scoped policies use a verified per-conversation PreToolUse hook. "
                "workspace_write allows native file edits only; shell, MCP, subagents and "
                "external paths are blocked. This is tool gating, not an OS process sandbox."
            )
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
        try:
            closed = await manager.end_session(session_id)
        except KeyError:
            closed = False
        return JSONResponse({"closed": closed})

    @asynccontextmanager
    async def lifespan(app):
        acquire = getattr(handler.task_store, "acquire_owner", None)
        if acquire is not None:
            acquire()
        async def reap_loop():
            while True:
                await asyncio.sleep(60)
                await manager.reap_idle_sessions()

        janitor = None
        try:
            recover = getattr(handler.task_store, "recover_interrupted", None)
            if recover is not None:
                await recover()
            async with manager:
                janitor = asyncio.create_task(reap_loop())
                yield
        finally:
            try:
                if janitor is not None:
                    janitor.cancel()
                    try:
                        await janitor
                    except asyncio.CancelledError:
                        pass
                await handler._active_task_registry.aclose()
                shutdown = getattr(backend, "close", None)
                if shutdown is not None:
                    await shutdown()
            finally:
                close_store = getattr(handler.task_store, "close", None)
                if close_store is not None:
                    close_store()

    app = Starlette(
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
    app.state.task_manager = manager
    return app
