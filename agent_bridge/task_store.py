"""SQLite-backed durable A2A TaskStore implementation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import os
from pathlib import Path
from typing import Any

from a2a.helpers import new_text_message
from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task, TaskState
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


class SQLiteTaskStore(TaskStore):
    """Durable SQLite storage for A2A tasks and idempotent request bindings."""

    def __init__(
        self,
        path: Path | str,
        owner_resolver: OwnerResolver | None = None,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteTaskStore requires a file; use InMemoryTaskStore for ephemeral tasks")
        self.path = Path(path).resolve()
        self.owner_resolver = owner_resolver or resolve_user_scope
        self._write_lock = asyncio.Lock()
        self._owner_file = None
        self._init_db()

    def acquire_owner(self):
        """Prevent a second server from recovering tasks owned by a live server."""
        if self._owner_file is not None:
            return
        handle = self.path.with_suffix(self.path.suffix + ".owner").open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f"Task database is already owned by a running server: {self.path}") from exc
        self._owner_file = handle

    async def _write(self, operation):
        async with self._write_lock:
            pending = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Do not let a late SQLite commit overwrite a subsequent cancellation.
                await pending
                raise

    def _init_db(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            row = cursor.fetchone()
            version = row[0] if row else 0
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported SQLiteTaskStore schema version {version}; "
                    f"expected <= {CURRENT_SCHEMA_VERSION}"
                )
            if version == 0:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        owner TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_data BLOB NOT NULL,
                        context_id TEXT NOT NULL DEFAULT '',
                        state INTEGER NOT NULL DEFAULT 0,
                        status_timestamp_iso TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (owner, task_id)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner, state)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_owner_context ON tasks(owner, context_id)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS request_bindings (
                        owner TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        fingerprint BLOB NOT NULL,
                        task_id TEXT NOT NULL,
                        PRIMARY KEY (owner, request_id),
                        FOREIGN KEY (owner, task_id) REFERENCES tasks(owner, task_id) ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_bindings_owner_task ON request_bindings(owner, task_id)
                """)
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                conn.commit()

    def _resolve_owner(self, context: ServerCallContext | None = None) -> str:
        if self.owner_resolver is not None:
            if context is None and self.owner_resolver is resolve_user_scope:
                context = ServerCallContext()
            owner = self.owner_resolver(context)
            if owner is not None:
                return str(owner)
        return "default"

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        owner = self._resolve_owner(context)
        task_data = task.SerializeToString()
        context_id = getattr(task, "context_id", "") or ""
        state = int(task.status.state) if task.HasField("status") else 0
        status_timestamp_iso = ""
        if task.HasField("status") and task.status.HasField("timestamp"):
            status_timestamp_iso = task.status.timestamp.ToJsonString()

        def _do_save() -> None:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(
                    """
                    INSERT INTO tasks (owner, task_id, task_data, context_id, state, status_timestamp_iso)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner, task_id) DO UPDATE SET
                        task_data = excluded.task_data,
                        context_id = excluded.context_id,
                        state = excluded.state,
                        status_timestamp_iso = excluded.status_timestamp_iso
                    """,
                    (owner, task.id, task_data, context_id, state, status_timestamp_iso),
                )
                conn.commit()

        await self._write(_do_save)

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        owner = self._resolve_owner(context)

        def _do_get() -> bytes | None:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT task_data FROM tasks WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                )
                row = cursor.fetchone()
                return row[0] if row is not None else None

        raw = await asyncio.to_thread(_do_get)
        if raw is None:
            return None
        task = Task()
        task.ParseFromString(raw)
        return task

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        owner = self._resolve_owner(context)

        def _do_delete() -> None:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(
                    "DELETE FROM request_bindings WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                )
                conn.execute(
                    "DELETE FROM tasks WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                )
                conn.commit()

        await self._write(_do_delete)

    async def list(
        self,
        params: a2a_pb2.ListTasksRequest | Any,
        context: ServerCallContext | None = None,
    ) -> a2a_pb2.ListTasksResponse:
        owner = self._resolve_owner(context)

        def _fetch_all() -> list[bytes]:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT task_data FROM tasks WHERE owner = ?",
                    (owner,),
                )
                return [row[0] for row in cursor.fetchall()]

        raw_list = await asyncio.to_thread(_fetch_all)
        tasks: list[Task] = []
        for raw in raw_list:
            t = Task()
            t.ParseFromString(raw)
            tasks.append(t)

        # Filter: context_id
        if getattr(params, "context_id", None):
            tasks = [t for t in tasks if t.context_id == params.context_id]

        # Filter: status state
        status_filter = getattr(params, "status", None)
        if status_filter:
            tasks = [t for t in tasks if t.status.state == status_filter]

        # Filter: status_timestamp_after
        if hasattr(params, "HasField") and params.HasField("status_timestamp_after"):
            cutoff_iso = params.status_timestamp_after.ToJsonString()
            tasks = [
                t
                for t in tasks
                if (
                    t.HasField("status")
                    and t.status.HasField("timestamp")
                    and t.status.timestamp.ToJsonString() >= cutoff_iso
                )
            ]
        elif not hasattr(params, "HasField") and getattr(params, "status_timestamp_after", None) is not None:
            val = params.status_timestamp_after
            cutoff_iso = val.ToJsonString() if hasattr(val, "ToJsonString") else str(val)
            tasks = [
                t
                for t in tasks
                if (
                    t.HasField("status")
                    and t.status.HasField("timestamp")
                    and t.status.timestamp.ToJsonString() >= cutoff_iso
                )
            ]

        # Order tasks by last update time. To ensure stable sorting, in cases where
        # timestamps are null or not unique, do a second order comparison of IDs.
        tasks.sort(
            key=lambda t: (
                t.status.HasField("timestamp") if t.HasField("status") else False,
                t.status.timestamp.ToJsonString()
                if t.HasField("status") and t.status.HasField("timestamp")
                else "",
                t.id,
            ),
            reverse=True,
        )

        total_size = len(tasks)
        start_idx = 0
        page_token = getattr(params, "page_token", "") or ""
        if page_token:
            try:
                start_task_id = decode_page_token(page_token)
            except InvalidParamsError:
                raise
            except Exception as exc:
                raise InvalidParamsError(f"Invalid page token: {page_token}") from exc
            valid_token = False
            for i, t in enumerate(tasks):
                if t.id == start_task_id:
                    start_idx = i
                    valid_token = True
                    break
            if not valid_token:
                raise InvalidParamsError(f"Invalid page token: {page_token}")

        page_size = getattr(params, "page_size", None) or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_page_token = (
            encode_page_token(tasks[end_idx].id)
            if end_idx < total_size
            else None
        )
        tasks = tasks[start_idx:end_idx]

        # Apply projections (history_length, artifacts)
        history_length = getattr(params, "history_length", None)
        artifacts = getattr(params, "artifacts", None)
        if artifacts is None:
            artifacts = getattr(params, "include_artifacts", None)
        if isinstance(params, a2a_pb2.ListTasksRequest):
            history_length = params.history_length if params.HasField("history_length") else None
            artifacts = params.include_artifacts if params.HasField("include_artifacts") else None

        for t in tasks:
            if history_length is not None:
                if history_length <= 0:
                    del t.history[:]
                elif len(t.history) > history_length:
                    del t.history[:-history_length]
            if artifacts is False:
                del t.artifacts[:]

        resp = a2a_pb2.ListTasksResponse(
            tasks=tasks,
            total_size=total_size,
            page_size=page_size,
        )
        if next_page_token:
            resp.next_page_token = next_page_token
        return resp

    async def recover_interrupted(self, context: ServerCallContext | None = None) -> int:
        owner = self._resolve_owner(context) if context is not None else None

        def _do_recover() -> int:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()
                if owner is not None:
                    cursor.execute(
                        "SELECT owner, task_id, task_data FROM tasks WHERE owner = ? AND state IN (1, 2)",
                        (owner,),
                    )
                else:
                    cursor.execute(
                        "SELECT owner, task_id, task_data FROM tasks WHERE state IN (1, 2)"
                    )
                rows = cursor.fetchall()
                if not rows:
                    return 0

                message_text = "Server restarted before task completion; execution was not resumed"
                recovered_count = 0
                for row_owner, row_task_id, raw_data in rows:
                    task = Task()
                    task.ParseFromString(raw_data)
                    if task.status.state not in (
                        TaskState.TASK_STATE_SUBMITTED,
                        TaskState.TASK_STATE_WORKING,
                    ):
                        continue

                    task.status.state = TaskState.TASK_STATE_FAILED
                    task.status.message.CopyFrom(new_text_message(message_text))
                    task.status.timestamp.GetCurrentTime()
                    task.metadata["agent_bridge.error"] = {
                        "code": "server_restarted",
                        "message": message_text,
                    }

                    updated_data = task.SerializeToString()
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET task_data = ?, state = ?, status_timestamp_iso = ?
                        WHERE owner = ? AND task_id = ?
                        """,
                        (
                            updated_data,
                            int(TaskState.TASK_STATE_FAILED),
                            task.status.timestamp.ToJsonString(),
                            row_owner,
                            row_task_id,
                        ),
                    )
                    recovered_count += 1

                conn.commit()
                return recovered_count

        return await self._write(_do_recover)

    async def bind_request(
        self,
        request_id: str,
        fingerprint: bytes,
        task: Task,
        context: ServerCallContext | None = None,
    ) -> tuple[str, bool]:
        owner = self._resolve_owner(context)
        fp_bytes = bytes(fingerprint)
        task_data = task.SerializeToString()
        context_id = getattr(task, "context_id", "") or ""
        state = int(task.status.state) if task.HasField("status") else 0
        status_timestamp_iso = ""
        if task.HasField("status") and task.status.HasField("timestamp"):
            status_timestamp_iso = task.status.timestamp.ToJsonString()

        def _do_bind() -> tuple[str, bool]:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT fingerprint, task_id FROM request_bindings WHERE owner = ? AND request_id = ?",
                    (owner, request_id),
                )
                row = cursor.fetchone()
                if row is not None:
                    existing_fp, existing_task_id = row
                    if existing_fp != fp_bytes:
                        raise InvalidParamsError(
                            f"request_id {request_id!r} is already bound to another request"
                        )
                    return existing_task_id, False

                cursor.execute(
                    """
                    INSERT INTO tasks (owner, task_id, task_data, context_id, state, status_timestamp_iso)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner, task_id) DO UPDATE SET
                        task_data = excluded.task_data,
                        context_id = excluded.context_id,
                        state = excluded.state,
                        status_timestamp_iso = excluded.status_timestamp_iso
                    """,
                    (owner, task.id, task_data, context_id, state, status_timestamp_iso),
                )
                cursor.execute(
                    """
                    INSERT INTO request_bindings (owner, request_id, fingerprint, task_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (owner, request_id, fp_bytes, task.id),
                )
                conn.commit()
                return task.id, True

        return await self._write(_do_bind)

    async def request_task(
        self,
        request_id: str,
        fingerprint: bytes,
        context: ServerCallContext | None = None,
    ) -> Task | None:
        owner = self._resolve_owner(context)
        fp_bytes = bytes(fingerprint)

        def _do_request_task() -> bytes | None:
            with contextlib.closing(sqlite3.connect(self.path, timeout=15.0)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT fingerprint, task_id FROM request_bindings WHERE owner = ? AND request_id = ?",
                    (owner, request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                existing_fp, task_id = row
                if existing_fp != fp_bytes:
                    raise InvalidParamsError(
                        f"request_id {request_id!r} is already bound to another request"
                    )
                cursor.execute(
                    "SELECT task_data FROM tasks WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                )
                task_row = cursor.fetchone()
                return task_row[0] if task_row is not None else None

        raw = await asyncio.to_thread(_do_request_task)
        if raw is None:
            return None
        task = Task()
        task.ParseFromString(raw)
        return task

    def close(self) -> None:
        if self._owner_file is not None:
            self._owner_file.close()
            self._owner_file = None
