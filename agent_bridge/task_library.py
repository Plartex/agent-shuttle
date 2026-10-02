"""Library-owned task and session lifecycle, independent of MCP and A2A.

Transport adapters may present these objects over their respective protocols,
but a Python caller can dispatch, inspect and cancel work directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any

from .backends import BackendResponse
from .profiles import ToolPolicy


_FINAL = frozenset({"completed", "failed", "canceled", "rejected"})


@dataclass(frozen=True)
class TaskStatus:
    id: str
    agent_id: str
    session_id: str | None
    state: str
    created_at: float
    updated_at: float
    silent_for_seconds: float | None
    error: dict | None = None


@dataclass(frozen=True)
class TaskResult(TaskStatus):
    text: str = ""
    usage: dict | None = None
    details: dict | None = None
    requested_model: str | None = None
    observed_model: str | None = None
    warnings: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    files_changed_state: str = "pending"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    agent_id: str
    model: str | None
    reasoning_effort: str | None
    tool_policy: str | None
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class AgentInfo:
    id: str
    backend: str
    supports_sessions: bool
    supports_resume_after_restart: bool
    tool_policies: tuple[str, ...]


@dataclass(frozen=True)
class Task:
    manager: "TaskManager"
    id: str

    async def status(self) -> TaskStatus:
        return await self.manager.status(self.id)

    async def wait(self, timeout: float | None = None) -> TaskStatus:
        return await self.manager.wait(self.id, timeout)

    async def result(self) -> TaskResult:
        await self.wait()
        return await self.manager.result(self.id)

    async def cancel(self) -> TaskStatus:
        return await self.manager.cancel(self.id)

    async def result_page(self, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.manager.result_page(self.id, cursor, limit)

    async def transcript(self, cursor: int = 0, limit: int = 100) -> dict:
        return await self.manager.transcript(self.id, cursor, limit)

    async def event_page(self, seq: int, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.manager.event_page(self.id, seq, cursor, limit)

    async def events(self, cursor: int = 0):
        """Replay journal entries and follow new events until the task is terminal."""
        while True:
            page = await self.transcript(cursor)
            for event in page["items"]:
                cursor = event["seq"] + 1
                yield event
            if not page["items"] and (await self.status()).state in _FINAL:
                return
            if not page["items"]:
                await asyncio.sleep(0.05)


@dataclass(frozen=True)
class Session:
    manager: "TaskManager"
    id: str

    async def dispatch(self, prompt: str, *, request_id: str | None = None) -> Task:
        return await self.manager.dispatch_session(self.id, prompt, request_id=request_id)

    async def end(self) -> bool:
        return await self.manager.end_session(self.id)


class _NativeSession:
    def __init__(self, session):
        self.session = session
        self.lock = asyncio.Lock()


class TaskManager:
    """Own task execution, sessions, events and durable results for one workspace."""

    def __init__(self, backends: dict[str, Any], *, workspace: Path | str,
                 database: Path | str | None = None,
                 memory: bool = False,
                 info_providers: dict[str, Any] | None = None,
                 execution_timeout_seconds: float = 1800,
                 stall_timeout_seconds: float = 1800):
        for value in (execution_timeout_seconds, stall_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Execution and stall budgets must be positive finite numbers")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        self.backends = dict(backends)
        self.info_providers = dict(info_providers or {})
        self.database = (None if memory else
                         Path(database or self.workspace / ".agent-shuttle" / "library-tasks.sqlite3").resolve())
        self.execution_timeout_seconds = execution_timeout_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        self._conn: sqlite3.Connection | None = None
        self._owner = None
        self._active: dict[str, asyncio.Task] = {}
        self._native_sessions: dict[str, _NativeSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._event_sinks: dict[str, Any] = {}
        self._closed = False

    @classmethod
    def for_workspace(cls, workspace: Path | str, *, antigravity_command: str = "agy",
                      database: Path | str | None = None) -> "TaskManager":
        """Create the built-in local workers without starting an A2A server."""
        from .backends import AntigravityCliBackend, CodexBackend
        from .info import AntigravityCliInfo, CodexInfo

        root = Path(workspace).resolve(strict=True)
        return cls({"codex": CodexBackend(root),
                    "antigravity": AntigravityCliBackend(root, antigravity_command)},
                   workspace=root, database=database,
                   info_providers={"codex": CodexInfo(root),
                                   "antigravity": AntigravityCliInfo(root, antigravity_command)})

    async def __aenter__(self) -> "TaskManager":
        if self._conn is not None or self._closed:
            raise RuntimeError("TaskManager is already open or closed")
        if self.database is not None:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        owner = (self.database.with_suffix(self.database.suffix + ".owner").open("a+b")
                 if self.database is not None else None)
        conn = None
        try:
            if owner is not None:
                if owner.seek(0, 2) == 0:
                    owner.write(b"0")
                    owner.flush()
                owner.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = sqlite3.connect(self.database or ":memory:", timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT,
                    request_id TEXT UNIQUE, fingerprint TEXT, prompt TEXT NOT NULL,
                    model TEXT, reasoning_effort TEXT, tool_policy TEXT,
                    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    last_activity_at REAL, text TEXT NOT NULL DEFAULT '',
                    usage TEXT, details TEXT, error TEXT,
                    observed_model TEXT, warnings TEXT, files_changed TEXT,
                    files_changed_state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_library_tasks_session ON tasks(session_id);
                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp REAL NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(task_id, seq)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, model TEXT,
                    reasoning_effort TEXT, tool_policy TEXT, state TEXT NOT NULL,
                    native_id TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    agent_id TEXT PRIMARY KEY, model TEXT, reasoning_effort TEXT
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "native_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN native_id TEXT")
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "warnings" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN warnings TEXT")
            now = time()
            interrupted = conn.execute(
                "SELECT id, session_id FROM tasks WHERE state IN ('submitted','working')"
            ).fetchall()
            interrupted_sessions = {row["session_id"] for row in interrupted if row["session_id"]}
            for row in interrupted:
                error = {"code": "worker_interrupted", "type": "WorkerInterrupted",
                         "message": "Worker stopped before task completion; execution was not replayed",
                         "retryable": False}
                conn.execute("UPDATE tasks SET state='failed', updated_at=?, error=?, files_changed_state='unavailable' WHERE id=?",
                             (now, json.dumps(error), row["id"]))
                self._append(conn, row["id"], "failed", error)
            for session in conn.execute("SELECT id, agent_id, native_id FROM sessions WHERE state IN ('open','suspended')").fetchall():
                backend = self.backends.get(session["agent_id"])
                resumable = (session["native_id"] is not None and session["id"] not in interrupted_sessions
                             and callable(getattr(backend, "resume_session", None)))
                conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                             ("suspended" if resumable else "interrupted", now, session["id"]))
            conn.commit()
            self._owner, self._conn = owner, conn
            return self
        except BaseException:
            if conn is not None:
                conn.close()
            if owner is not None:
                owner.close()
            raise

    async def __aexit__(self, *_):
        active = list(self._active.values())
        for running in active:
            running.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._conn is not None:
            for row in self._conn.execute("SELECT id FROM tasks WHERE state IN ('submitted','working')").fetchall():
                await self._update(row["id"], "canceled", {"reason": "manager closed"})
        for native in list(self._native_sessions.values()):
            await native.session.close()
        self._native_sessions.clear()
        if self._conn is not None:
            for row in self._conn.execute("SELECT id, agent_id, native_id FROM sessions WHERE state='open'").fetchall():
                backend = self.backends.get(row["agent_id"])
                resumable = row["native_id"] is not None and callable(getattr(backend, "resume_session", None))
                self._conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                                   ("suspended" if resumable else "interrupted", time(), row["id"]))
            self._conn.commit()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._owner is not None:
            self._owner.close()
            self._owner = None
        self._closed = True

    def _db(self) -> sqlite3.Connection:
        if self._conn is None or self._closed:
            raise RuntimeError("TaskManager must be used inside 'async with'")
        return self._conn

    @staticmethod
    def _append(conn, task_id: str, kind: str, data: dict):
        seq = conn.execute("SELECT COALESCE(MAX(seq), -1)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                     (task_id, seq, time(), kind, json.dumps(data, ensure_ascii=False, default=str)))

    def _row(self, task_id: str):
        row = self._db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task {task_id}")
        return row

    @staticmethod
    def _status(row) -> TaskStatus:
        silent = None if row["state"] in _FINAL or row["last_activity_at"] is None else max(0, time() - row["last_activity_at"])
        return TaskStatus(row["id"], row["agent_id"], row["session_id"], row["state"],
                          row["created_at"], row["updated_at"], silent,
                          json.loads(row["error"]) if row["error"] else None)

    async def dispatch(self, agent_id: str, prompt: str, *, model: str | None = None,
                       reasoning_effort: str | None = None, tool_policy: str | None = None,
                       session_id: str | None = None, request_id: str | None = None,
                       task_id: str | None = None, event_sink=None) -> Task:
        conn = self._db()
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        if session_id is None:
            preference = await self.get_preference(agent_id)
            if model is None:
                model = preference["model"]
            if reasoning_effort is None:
                reasoning_effort = preference["reasoning_effort"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty")
        if tool_policy is not None and tool_policy not in {policy.value for policy in ToolPolicy}:
            raise ValueError("Unknown tool_policy")
        if request_id is not None:
            request_id = str(uuid.UUID(request_id))
        if session_id is not None:
            session_id = str(uuid.UUID(session_id))
            session = self._session_row(session_id)
            if session["state"] not in {"open", "suspended"}:
                raise RuntimeError("Session was closed after cancellation; start a new session")
            if (session["agent_id"], session["model"], session["reasoning_effort"], session["tool_policy"]) != (agent_id, model, reasoning_effort, tool_policy):
                raise ValueError("Session agent, model, effort and tool policy are pinned")
        fingerprint = hashlib.sha256(json.dumps(
            [agent_id, prompt, model, reasoning_effort, tool_policy, session_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        if request_id is not None:
            existing = conn.execute("SELECT id, fingerprint FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("request_id is already bound to another request")
                return Task(self, existing["id"])
        task_id = str(uuid.UUID(task_id)) if task_id is not None else str(uuid.uuid4())
        now = time()
        conn.execute("""INSERT INTO tasks
            (id, agent_id, session_id, request_id, fingerprint, prompt, model,
             reasoning_effort, tool_policy, state, created_at, updated_at, last_activity_at)
            VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?,?)""",
            (task_id, agent_id, session_id, request_id, fingerprint, prompt, model,
             reasoning_effort, tool_policy, now, now, now))
        conn.execute("UPDATE tasks SET files_changed_state='pending' WHERE id=?", (task_id,))
        self._append(conn, task_id, "submitted", {"prompt": prompt})
        conn.commit()
        if event_sink is not None:
            self._event_sinks[task_id] = event_sink
        running = asyncio.create_task(self._execute(task_id))
        self._active[task_id] = running
        running.add_done_callback(lambda _: (self._active.pop(task_id, None),
                                             self._event_sinks.pop(task_id, None)))
        return Task(self, task_id)

    async def list_agents(self) -> list[AgentInfo]:
        self._db()
        result = []
        for agent_id, backend in sorted(self.backends.items()):
            name = type(backend).__name__
            if name == "CodexBackend":
                policies = ("read_only", "workspace_write", "full_access")
            elif name == "AntigravityCliBackend":
                policies = ("no_tools", "read_only", "workspace_write")
                if getattr(backend, "dangerously_skip_permissions", False):
                    policies += ("full_access",)
            elif name == "ProfiledBackend":
                ordered = tuple(policy.value for policy in ToolPolicy)
                maximum = backend.profile.max_tool_policy.value
                policies = ordered[:ordered.index(maximum) + 1]
            else:
                policies = ()
            supports_sessions = name != "AntigravitySdkBackend" and callable(getattr(backend, "open_session", None))
            result.append(AgentInfo(agent_id, name, supports_sessions,
                                    callable(getattr(backend, "resume_session", None)), policies))
        return result

    async def agent_info(self, agent_id: str, *, capabilities: bool = True,
                         usage: bool = True) -> dict:
        infos = {item.id: item for item in await self.list_agents()}
        if agent_id not in infos:
            raise ValueError(f"Unknown agent {agent_id!r}")
        from dataclasses import asdict

        result = asdict(infos[agent_id])
        provider = self.info_providers.get(agent_id)
        if provider is not None:
            result.update(await provider.fetch(capabilities=capabilities, usage=usage))
        return result

    async def get_preference(self, agent_id: str) -> dict:
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        row = self._db().execute("SELECT model, reasoning_effort FROM preferences WHERE agent_id=?",
                                 (agent_id,)).fetchone()
        return {"model": row["model"] if row else None,
                "reasoning_effort": row["reasoning_effort"] if row else None}

    async def set_preference(self, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None) -> dict:
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        for value in (model, reasoning_effort):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("Model and reasoning effort must be nonempty strings")
        conn = self._db()
        conn.execute("INSERT OR REPLACE INTO preferences VALUES (?,?,?)",
                     (agent_id, model, reasoning_effort))
        conn.commit()
        return await self.get_preference(agent_id)

    async def get(self, task_id: str) -> Task:
        self._row(task_id)
        return Task(self, task_id)

    async def list_tasks(self, *, session_id: str | None = None) -> list[TaskStatus]:
        conn = self._db()
        if session_id is None:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY created_at, id", (session_id,)).fetchall()
        return [self._status(row) for row in rows]

    async def status(self, task_id: str) -> TaskStatus:
        return self._status(self._row(task_id))

    async def wait(self, task_id: str, timeout: float | None = None) -> TaskStatus:
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and nonnegative")
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            status = await self.status(task_id)
            if status.state in _FINAL or (deadline is not None and monotonic() >= deadline):
                return status
            await asyncio.sleep(0.05 if deadline is None else min(0.05, max(0, deadline - monotonic())))

    async def result(self, task_id: str) -> TaskResult:
        row = self._row(task_id)
        status = self._status(row)
        return TaskResult(**status.__dict__, text=row["text"],
                          usage=json.loads(row["usage"]) if row["usage"] else None,
                          details=json.loads(row["details"]) if row["details"] else None,
                          requested_model=row["model"], observed_model=row["observed_model"],
                          warnings=tuple(json.loads(row["warnings"])) if row["warnings"] else (),
                          files_changed=tuple(json.loads(row["files_changed"])) if row["files_changed"] else (),
                          files_changed_state=row["files_changed_state"])

    async def cancel(self, task_id: str) -> TaskStatus:
        status = await self.status(task_id)
        if status.state in _FINAL:
            return status
        running = self._active.get(task_id)
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if (await self.status(task_id)).state not in _FINAL:
            await self._update(task_id, "canceled", {"reason": "explicit cancellation"})
        session_id = self._row(task_id)["session_id"]
        if session_id is not None:
            await self._interrupt_session(session_id)
        return await self.status(task_id)

    @staticmethod
    def _page(cursor: int, limit: int, maximum: int):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or limit < 1 or limit > maximum:
            raise ValueError(f"cursor must be nonnegative and limit between 1 and {maximum}")

    async def result_page(self, task_id: str, cursor: int = 0, limit: int = 60000) -> dict:
        self._page(cursor, limit, 60000)
        row = self._row(task_id)
        end = min(cursor + limit, len(row["text"]))
        return {"task_id": task_id, "state": row["state"], "text": row["text"][cursor:end],
                "next_cursor": end if end < len(row["text"]) else None,
                "total_size": len(row["text"])}

    async def transcript(self, task_id: str, cursor: int = 0, limit: int = 100) -> dict:
        self._page(cursor, limit, 100)
        self._row(task_id)
        rows = self._db().execute(
            "SELECT seq, timestamp, kind, data FROM events WHERE task_id=? AND seq>=? ORDER BY seq LIMIT ?",
            (task_id, cursor, limit),
        ).fetchall()
        items = []
        remaining = 60000
        for row in rows:
            data = row["data"]
            overhead = len(row["kind"]) + 200
            if len(data) + overhead > remaining:
                if remaining < 500 and items:
                    break
                preview_size = max(0, remaining - overhead - 200)
                item_data = {"preview": data[:preview_size], "truncated": True}
                truncated = True
            else:
                item_data = json.loads(data)
                truncated = False
            items.append({"seq": row["seq"], "timestamp": row["timestamp"],
                          "kind": row["kind"], "data": item_data,
                          "data_truncated": truncated})
            remaining -= min(len(data), max(0, remaining - overhead)) + overhead
            if remaining < 500:
                break
        total = self._db().execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        next_cursor = items[-1]["seq"] + 1 if items and items[-1]["seq"] + 1 < total else None
        return {"task_id": task_id, "items": items, "next_cursor": next_cursor, "total_size": total}

    async def event_page(self, task_id: str, seq: int, cursor: int = 0,
                         limit: int = 60000) -> dict:
        self._page(cursor, limit, 60000)
        if type(seq) is not int or seq < 0:
            raise ValueError("seq must be nonnegative")
        self._row(task_id)
        row = self._db().execute("SELECT data FROM events WHERE task_id=? AND seq=?",
                                 (task_id, seq)).fetchone()
        if row is None:
            raise KeyError(f"Unknown event {seq} for task {task_id}")
        data = row["data"]
        end = min(cursor + limit, len(data))
        return {"task_id": task_id, "seq": seq, "text": data[cursor:end],
                "next_cursor": end if end < len(data) else None,
                "total_size": len(data)}

    async def _update(self, task_id: str, state: str, data: dict | None = None,
                      *, text: str | None = None, usage: dict | None = None,
                      details: dict | None = None, error: dict | None = None,
                      observed_model: str | None = None,
                      warnings: tuple[str, ...] | None = None,
                      files_changed: tuple[str, ...] | None = None,
                      files_changed_state: str | None = None):
        conn = self._db()
        old = self._row(task_id)
        if old["state"] in _FINAL:
            return
        if state in _FINAL and files_changed_state is None:
            files_changed_state = "unavailable"
        now = time()
        conn.execute("""UPDATE tasks SET state=?, updated_at=?, last_activity_at=?,
            text=COALESCE(?,text), usage=COALESCE(?,usage), details=COALESCE(?,details),
            error=COALESCE(?,error), observed_model=COALESCE(?,observed_model),
            warnings=COALESCE(?,warnings), files_changed=COALESCE(?,files_changed),
            files_changed_state=COALESCE(?,files_changed_state) WHERE id=?""",
            (state, now, now, text, json.dumps(usage) if usage is not None else None,
             json.dumps(details) if details is not None else None,
             json.dumps(error) if error is not None else None,
             observed_model, json.dumps(warnings) if warnings is not None else None,
             json.dumps(files_changed) if files_changed is not None else None,
             files_changed_state, task_id))
        self._append(conn, task_id, state if data is None else data.get("kind", state), data or {})
        conn.commit()

    async def _execute(self, task_id: str):
        row = self._row(task_id)
        await self._update(task_id, "working")
        exclusive = sum(not running.done() for running in self._active.values()) <= 1
        try:
            before_files = await self._workspace_changes() if exclusive else None
        except asyncio.CancelledError:
            if row["session_id"]:
                await self._interrupt_session(row["session_id"])
            await self._update(task_id, "canceled", {"reason": "cancelled"})
            return
        started = monotonic()
        last_activity = started

        async def on_event(event):
            nonlocal last_activity
            last_activity = monotonic()
            await self._update(task_id, "working", event)
            sink = self._event_sinks.get(task_id)
            if sink is not None:
                try:
                    await sink(event)
                except Exception:
                    # A detached transport subscriber cannot change worker outcome.
                    self._event_sinks.pop(task_id, None)

        async def invoke():
            backend = self.backends[row["agent_id"]]
            kwargs = {"reasoning_effort": row["reasoning_effort"],
                      "read_only": row["tool_policy"] == "read_only"}
            target = backend.open_session if row["session_id"] else backend.run
            if row["tool_policy"] is not None and "tool_policy" in inspect.signature(target).parameters:
                kwargs["tool_policy"] = row["tool_policy"]
            if row["session_id"]:
                session_id = row["session_id"]
                lock = self._session_locks.setdefault(session_id, asyncio.Lock())
                async with lock:
                    session_row = self._session_row(session_id)
                    if session_row["state"] not in {"open", "suspended"}:
                        raise RuntimeError("Session was closed after cancellation; start a new session")
                    native = self._native_sessions.get(session_id)
                    if native is None:
                        if session_row["state"] == "suspended":
                            opened = await backend.resume_session(session_row["native_id"], row["model"], **kwargs)
                        else:
                            opened = await backend.open_session(row["model"], **kwargs)
                        native = _NativeSession(opened)
                        self._native_sessions[session_id] = native
                        native_id = getattr(opened, "native_id", None)
                        self._db().execute("UPDATE sessions SET state='open', native_id=?, updated_at=? WHERE id=?",
                                           (native_id, time(), session_id))
                        self._db().commit()
                    if "on_event" in inspect.signature(native.session.ask).parameters:
                        return await native.session.ask(row["prompt"], on_event=on_event)
                    return await native.session.ask(row["prompt"])
            if "on_event" in inspect.signature(backend.run).parameters:
                kwargs["on_event"] = on_event
            return await backend.run(row["prompt"], row["model"], **kwargs)

        running = asyncio.create_task(invoke())
        try:
            while not running.done():
                now = monotonic()
                remaining = min(self.execution_timeout_seconds - (now - started),
                                self.stall_timeout_seconds - (now - last_activity))
                if remaining <= 0:
                    if now - started >= self.execution_timeout_seconds:
                        raise TimeoutError("Worker exceeded its execution budget")
                    raise TimeoutError("Worker exceeded its inactivity budget")
                await asyncio.wait({running}, timeout=remaining)
            answer = await running
            if isinstance(answer, BackendResponse):
                text, usage, details = answer.text, answer.usage, answer.details
            else:
                text, usage, details = str(answer), None, None
            observed_model = details.get("observed_model") if isinstance(details, dict) else None
            if not isinstance(observed_model, str) or not observed_model:
                observed_model = None
            warning_data = details.get("warnings") if isinstance(details, dict) else None
            warnings = tuple(item for item in warning_data if isinstance(item, str)) if isinstance(warning_data, (list, tuple)) else ()
            exclusive = exclusive and sum(not running.done() for running in self._active.values()) <= 1
            after_files = await self._workspace_changes() if exclusive and before_files == set() else None
            collected = after_files is not None
            changed = tuple(sorted(after_files)[:200]) if collected else ()
            await self._update(task_id, "completed", {"text": text}, text=text,
                               usage=usage, details=details,
                               observed_model=observed_model, warnings=warnings,
                               files_changed=changed if collected else None,
                               files_changed_state="collected" if collected else "unavailable")
        except asyncio.CancelledError:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if row["session_id"]:
                await self._interrupt_session(row["session_id"])
            await self._update(task_id, "canceled", {"reason": "cancelled"})
        except Exception as exc:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if row["session_id"]:
                await self._interrupt_session(row["session_id"])
            code = ("worker_stalled" if isinstance(exc, TimeoutError) and "inactivity" in str(exc)
                    else "worker_timeout" if isinstance(exc, TimeoutError) else "backend_error")
            error = {"code": code, "type": type(exc).__name__, "message": str(exc),
                     "retryable": False}
            await self._update(task_id, "failed", error, error=error)
        finally:
            if row["session_id"]:
                self._db().execute("UPDATE sessions SET updated_at=? WHERE id=? AND state='open'",
                                   (time(), row["session_id"]))
                self._db().commit()

    async def _workspace_changes(self) -> set[str] | None:
        if not any((directory / ".git").exists() for directory in (self.workspace, *self.workspace.parents)):
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(self.workspace), "-c", "core.quotePath=false",
                "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return None
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
        except OSError:
            return None
        if process.returncode != 0:
            return None
        chunks = output.split(b"\0")
        paths = set()
        database_relative = None
        if self.database is not None:
            try:
                database_relative = self.database.relative_to(self.workspace).as_posix()
            except ValueError:
                pass
        index = 0
        while index < len(chunks) and chunks[index]:
            item = chunks[index]
            if len(item) < 4:
                return None
            path = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
            if database_relative is None or not (path == database_relative or path.startswith(database_relative + ".")
                                                 or path.startswith(database_relative + "-")):
                paths.add(path)
            if b"R" in item[:2] or b"C" in item[:2]:
                index += 1  # Porcelain -z includes the original name after a rename/copy.
            index += 1
        return paths

    async def _interrupt_session(self, session_id: str):
        conn = self._db()
        conn.execute("UPDATE sessions SET state='interrupted', updated_at=? WHERE id=? AND state IN ('open','suspended')",
                     (time(), session_id))
        conn.commit()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            native = self._native_sessions.pop(session_id, None)
            if native is not None:
                await native.session.close()

    def _session_row(self, session_id: str):
        row = self._db().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown session {session_id}")
        return row

    @staticmethod
    def _session_info(row) -> SessionInfo:
        return SessionInfo(row["id"], row["agent_id"], row["model"], row["reasoning_effort"],
                           row["tool_policy"], row["state"], row["created_at"], row["updated_at"])

    async def create_session(self, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None,
                             tool_policy: str | None = None) -> Session:
        conn = self._db()
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        preference = await self.get_preference(agent_id)
        if model is None:
            model = preference["model"]
        if reasoning_effort is None:
            reasoning_effort = preference["reasoning_effort"]
        if tool_policy is not None and tool_policy not in {policy.value for policy in ToolPolicy}:
            raise ValueError("Unknown tool_policy")
        session_id, now = str(uuid.uuid4()), time()
        conn.execute("INSERT INTO sessions (id,agent_id,model,reasoning_effort,tool_policy,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (session_id, agent_id, model, reasoning_effort, tool_policy, "open", now, now))
        conn.commit()
        return Session(self, session_id)

    async def ensure_session(self, session_id: str, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None,
                             tool_policy: str | None = None) -> Session:
        """Bind an existing transport context ID to the library session."""
        session_id = str(uuid.UUID(session_id))
        conn = self._db()
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            now = time()
            conn.execute("INSERT INTO sessions (id,agent_id,model,reasoning_effort,tool_policy,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         (session_id, agent_id, model, reasoning_effort, tool_policy,
                          "open", now, now))
            conn.commit()
        elif (row["agent_id"], row["model"], row["reasoning_effort"], row["tool_policy"]) != (agent_id, model, reasoning_effort, tool_policy):
            raise ValueError("Session agent, model, effort and tool policy are pinned")
        return Session(self, session_id)

    async def dispatch_session(self, session_id: str, prompt: str, *, request_id: str | None = None) -> Task:
        row = self._session_row(session_id)
        return await self.dispatch(row["agent_id"], prompt, model=row["model"],
                                   reasoning_effort=row["reasoning_effort"],
                                   tool_policy=row["tool_policy"], session_id=session_id,
                                   request_id=request_id)

    async def list_sessions(self) -> list[SessionInfo]:
        rows = self._db().execute("SELECT * FROM sessions ORDER BY created_at, id").fetchall()
        return [self._session_info(row) for row in rows]

    async def reap_idle_sessions(self, idle_seconds: float = 1800) -> list[str]:
        if isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float)) or not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive finite")
        cutoff = time() - idle_seconds
        rows = self._db().execute(
            "SELECT id FROM sessions WHERE state IN ('open','suspended') AND updated_at<? ORDER BY updated_at",
            (cutoff,),
        ).fetchall()
        expired = []
        for row in rows:
            session_id = row["id"]
            if any(not running.done() and self._row(task_id)["session_id"] == session_id
                   for task_id, running in list(self._active.items())):
                continue
            await self.end_session(session_id)
            expired.append(session_id)
        return expired

    async def session(self, session_id: str) -> Session:
        self._session_row(session_id)
        return Session(self, session_id)

    async def end_session(self, session_id: str) -> bool:
        conn = self._db()
        row = self._session_row(session_id)
        if row["state"] == "ended":
            return False
        active = [task_id for task_id in self._active
                  if self._row(task_id)["session_id"] == session_id]
        for task_id in active:
            await self.cancel(task_id)
        native = self._native_sessions.pop(session_id, None)
        if native is not None:
            async with native.lock:
                await native.session.close()
        conn.execute("UPDATE sessions SET state='ended', updated_at=? WHERE id=?", (time(), session_id))
        conn.commit()
        return True
