"""Tests for SQLite durable A2A TaskStore."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from google.protobuf.timestamp_pb2 import Timestamp

from a2a.helpers import new_text_message, new_text_part
from a2a.types import ListTasksRequest, Task, TaskState
from a2a.utils.errors import InvalidParamsError

from agent_bridge.task_store import CURRENT_SCHEMA_VERSION, SQLiteTaskStore


def _create_task(
    task_id: str,
    *,
    context_id: str = "",
    state: TaskState = TaskState.TASK_STATE_SUBMITTED,
    status_text: str = "",
    history_texts: list[str] | None = None,
    artifact_texts: list[str] | None = None,
    timestamp_iso: str | None = None,
    with_timestamp: bool = True,
) -> Task:
    task = Task(id=task_id, context_id=context_id)
    task.status.state = state
    if with_timestamp:
        if timestamp_iso is not None:
            task.status.timestamp.FromJsonString(timestamp_iso)
        else:
            task.status.timestamp.GetCurrentTime()
    if status_text:
        task.status.message.CopyFrom(new_text_message(status_text))
    if history_texts:
        for text in history_texts:
            task.history.append(new_text_message(text))
    if artifact_texts:
        for art_text in artifact_texts:
            art = task.artifacts.add()
            art.name = "output"
            art.parts.append(new_text_part(art_text))
    return task


class TestSQLiteTaskStore(unittest.IsolatedAsyncioTestCase):
    def test_one_running_server_owns_database_and_releases_owner_lock(self):
        first = SQLiteTaskStore(self.db_path)
        second = SQLiteTaskStore(self.db_path)
        first.acquire_owner()
        try:
            with self.assertRaisesRegex(RuntimeError, "already owned"):
                second.acquire_owner()
        finally:
            first.close()
        second.acquire_owner()
        second.close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tasks.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_initialization_and_version_rejection(self) -> None:
        # Initializing new DB sets schema version to CURRENT_SCHEMA_VERSION.
        store = SQLiteTaskStore(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            version = cursor.fetchone()[0]
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)

        # Modifying user_version to an unsupported newer version must be rejected.
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
            conn.commit()

        with self.assertRaisesRegex(RuntimeError, "Unsupported.*schema version"):
            SQLiteTaskStore(self.db_path)

    def test_connections_closed_explicitly(self) -> None:
        # Verifies that initializing and reading from SQLiteTaskStore leaves no locked file handles on Windows.
        store = SQLiteTaskStore(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM tasks")
            self.assertEqual(cursor.fetchone()[0], 0)
        # Unlinking the database file succeeds because all connections are closed.
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

    async def test_restart_persistence(self) -> None:
        store1 = SQLiteTaskStore(self.db_path)
        task = _create_task(
            "task-restart",
            context_id="ctx-1",
            state=TaskState.TASK_STATE_WORKING,
            status_text="processing",
            history_texts=["step 1", "step 2"],
            artifact_texts=["result data"],
        )
        await store1.save(task)

        # Drop store instance and create a new instance on the same database file.
        del store1
        store2 = SQLiteTaskStore(self.db_path)
        loaded = await store2.get("task-restart")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.id, "task-restart")
        self.assertEqual(loaded.context_id, "ctx-1")
        self.assertEqual(loaded.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(len(loaded.history), 2)
        self.assertEqual(loaded.history[0].parts[0].text, "step 1")
        self.assertEqual(loaded.history[1].parts[0].text, "step 2")
        self.assertEqual(len(loaded.artifacts), 1)
        self.assertEqual(loaded.artifacts[0].parts[0].text, "result data")

    async def test_mutation_isolation_defensive_copies(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        original = _create_task(
            "task-mut",
            status_text="initial",
            history_texts=["entry 1"],
            artifact_texts=["artifact 1"],
        )
        await store.save(original)

        # Mutating the original object in memory after save() must not alter stored data.
        original.status.state = TaskState.TASK_STATE_COMPLETED
        del original.history[:]
        del original.artifacts[:]

        retrieved1 = await store.get("task-mut")
        self.assertIsNotNone(retrieved1)
        assert retrieved1 is not None
        self.assertEqual(retrieved1.status.state, TaskState.TASK_STATE_SUBMITTED)
        self.assertEqual(len(retrieved1.history), 1)
        self.assertEqual(len(retrieved1.artifacts), 1)

        # Mutating the retrieved object must not alter subsequent get() calls.
        retrieved1.status.state = TaskState.TASK_STATE_FAILED
        retrieved2 = await store.get("task-mut")
        self.assertIsNotNone(retrieved2)
        assert retrieved2 is not None
        self.assertEqual(retrieved2.status.state, TaskState.TASK_STATE_SUBMITTED)

        # Mutating an object in list() result must not alter store.
        list_resp = await store.list(ListTasksRequest())
        self.assertEqual(len(list_resp.tasks), 1)
        list_resp.tasks[0].status.state = TaskState.TASK_STATE_CANCELED

        retrieved3 = await store.get("task-mut")
        self.assertIsNotNone(retrieved3)
        assert retrieved3 is not None
        self.assertEqual(retrieved3.status.state, TaskState.TASK_STATE_SUBMITTED)

    async def test_recover_interrupted_states_and_details(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        # Create tasks in SUBMITTED and WORKING states
        t_sub = _create_task("t-sub", state=TaskState.TASK_STATE_SUBMITTED, status_text="queued")
        t_work = _create_task("t-work", state=TaskState.TASK_STATE_WORKING, status_text="active")
        await store.save(t_sub)
        await store.save(t_work)

        recovered_count = await store.recover_interrupted()
        self.assertEqual(recovered_count, 2)

        expected_msg = "Server restarted before task completion; execution was not resumed"

        res_sub = await store.get("t-sub")
        self.assertIsNotNone(res_sub)
        assert res_sub is not None
        self.assertEqual(res_sub.status.state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(res_sub.status.message.parts[0].text, expected_msg)
        self.assertEqual(res_sub.metadata["agent_bridge.error"]["code"], "server_restarted")

        res_work = await store.get("t-work")
        self.assertIsNotNone(res_work)
        assert res_work is not None
        self.assertEqual(res_work.status.state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(res_work.status.message.parts[0].text, expected_msg)
        self.assertEqual(res_work.metadata["agent_bridge.error"]["code"], "server_restarted")

        # Second recovery call finds nothing left to recover.
        self.assertEqual(await store.recover_interrupted(), 0)

    async def test_recover_interrupted_preserves_terminal_and_input_auth(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        preserved_states = [
            ("t-comp", TaskState.TASK_STATE_COMPLETED),
            ("t-fail", TaskState.TASK_STATE_FAILED),
            ("t-canc", TaskState.TASK_STATE_CANCELED),
            ("t-rej", TaskState.TASK_STATE_REJECTED),
            ("t-in-req", TaskState.TASK_STATE_INPUT_REQUIRED),
            ("t-auth-req", TaskState.TASK_STATE_AUTH_REQUIRED),
        ]
        for task_id, state in preserved_states:
            task = _create_task(task_id, state=state, status_text="status unchanged")
            await store.save(task)

        # Include one working task that should be recovered.
        working_task = _create_task("t-working", state=TaskState.TASK_STATE_WORKING)
        await store.save(working_task)

        recovered = await store.recover_interrupted()
        self.assertEqual(recovered, 1)

        for task_id, expected_state in preserved_states:
            loaded = await store.get(task_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.status.state, expected_state)
            self.assertEqual(loaded.status.message.parts[0].text, "status unchanged")
            self.assertNotIn("agent_bridge.error", loaded.metadata)

    async def test_bind_request_atomicity_and_retry_dedup(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        task = _create_task("t-1", status_text="initial")
        fp = b"fingerprint-1234"

        # First insertion returns (task_id, True).
        task_id, inserted = await store.bind_request("req-1", fp, task)
        self.assertEqual(task_id, "t-1")
        self.assertTrue(inserted)

        # Identical retry with same fingerprint returns (task_id, False).
        retry_task = _create_task("t-2-ignored", status_text="different object")
        task_id_retry, inserted_retry = await store.bind_request("req-1", fp, retry_task)
        self.assertEqual(task_id_retry, "t-1")
        self.assertFalse(inserted_retry)

        # The stored task remains the first one.
        stored = await store.get("t-1")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.id, "t-1")

    async def test_bind_request_conflict_raises_invalid_params(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        task = _create_task("t-conflict")
        fp1 = b"fingerprint-original"
        fp2 = b"fingerprint-conflicting"

        await store.bind_request("req-conflict", fp1, task)

        with self.assertRaises(InvalidParamsError):
            await store.bind_request("req-conflict", fp2, _create_task("t-other"))

    async def test_bind_request_concurrent_dispatch(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        fp = b"shared-fingerprint"

        async def worker(idx: int) -> tuple[str, bool]:
            task = _create_task(f"t-concurrent-{idx}")
            return await store.bind_request("req-concurrent", fp, task)

        results = await asyncio.gather(*(worker(i) for i in range(10)))
        true_count = sum(1 for _, inserted in results if inserted)
        false_count = sum(1 for _, inserted in results if not inserted)
        task_ids = {tid for tid, _ in results}

        self.assertEqual(true_count, 1)
        self.assertEqual(false_count, 9)
        self.assertEqual(len(task_ids), 1)

    async def test_request_task_conflict_and_snapshot(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        task = _create_task("t-snap", status_text="snapshot check")
        fp = b"fingerprint-valid"

        # Not found initially
        self.assertIsNone(await store.request_task("req-snap", fp))

        await store.bind_request("req-snap", fp, task)

        # Retrieval with matching fingerprint
        retrieved = await store.request_task("req-snap", fp)
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.id, "t-snap")
        self.assertEqual(retrieved.status.message.parts[0].text, "snapshot check")

        # Conflict raises InvalidParamsError
        with self.assertRaises(InvalidParamsError):
            await store.request_task("req-snap", b"fingerprint-wrong")

    async def test_delete_task_removes_request_binding(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        task = _create_task("t-del")
        fp = b"fingerprint-del"

        await store.bind_request("req-del", fp, task)
        self.assertIsNotNone(await store.request_task("req-del", fp))

        await store.delete("t-del")

        self.assertIsNone(await store.get("t-del"))
        # Binding must also be removed when task is deleted
        self.assertIsNone(await store.request_task("req-del", fp))

        # Re-binding with a new task after deletion succeeds
        new_task = _create_task("t-new")
        new_tid, new_inserted = await store.bind_request("req-del", fp, new_task)
        self.assertEqual(new_tid, "t-new")
        self.assertTrue(new_inserted)

    async def test_list_filters_context_state_timestamp(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        t1 = _create_task(
            "t1",
            context_id="ctx-A",
            state=TaskState.TASK_STATE_WORKING,
            timestamp_iso="2026-10-01T10:00:00Z",
        )
        t2 = _create_task(
            "t2",
            context_id="ctx-A",
            state=TaskState.TASK_STATE_COMPLETED,
            timestamp_iso="2026-10-01T11:00:00Z",
        )
        t3 = _create_task(
            "t3",
            context_id="ctx-B",
            state=TaskState.TASK_STATE_WORKING,
            timestamp_iso="2026-10-01T12:00:00Z",
        )
        for t in (t1, t2, t3):
            await store.save(t)

        # Filter by context_id
        res_ctx = await store.list(ListTasksRequest(context_id="ctx-A"))
        self.assertEqual([t.id for t in res_ctx.tasks], ["t2", "t1"])

        # Filter by status state
        res_state = await store.list(ListTasksRequest(status=TaskState.TASK_STATE_WORKING))
        self.assertEqual([t.id for t in res_state.tasks], ["t3", "t1"])

        # Filter by status_timestamp_after
        cutoff = Timestamp()
        cutoff.FromJsonString("2026-10-01T10:30:00Z")
        res_time = await store.list(ListTasksRequest(status_timestamp_after=cutoff))
        self.assertEqual([t.id for t in res_time.tasks], ["t3", "t2"])

    async def test_list_sorting_order(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        t_no_ts_1 = _create_task("t-no-ts-1", with_timestamp=False)
        t_no_ts_2 = _create_task("t-no-ts-2", with_timestamp=False)
        t_ts_early = _create_task("t-ts-early", timestamp_iso="2026-10-01T08:00:00Z")
        t_ts_late = _create_task("t-ts-late", timestamp_iso="2026-10-01T09:00:00Z")

        for t in (t_no_ts_1, t_ts_early, t_no_ts_2, t_ts_late):
            await store.save(t)

        resp = await store.list(ListTasksRequest())
        ids = [t.id for t in resp.tasks]
        # Timestamps descending first, then tasks without timestamp ordered by ID descending
        self.assertEqual(ids, ["t-ts-late", "t-ts-early", "t-no-ts-2", "t-no-ts-1"])

    async def test_list_pagination_and_invalid_token(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        for i in range(5):
            t = _create_task(f"task-{i:02d}", timestamp_iso=f"2026-10-01T10:0{i}:00Z")
            await store.save(t)

        # Page 1: page_size=2
        p1 = await store.list(ListTasksRequest(page_size=2))
        self.assertEqual([t.id for t in p1.tasks], ["task-04", "task-03"])
        self.assertEqual(p1.total_size, 5)
        self.assertEqual(p1.page_size, 2)
        self.assertIsNotNone(p1.next_page_token)

        # Page 2: with page_token
        p2 = await store.list(ListTasksRequest(page_size=2, page_token=p1.next_page_token))
        self.assertEqual([t.id for t in p2.tasks], ["task-02", "task-01"])
        self.assertIsNotNone(p2.next_page_token)

        # Page 3: last item
        p3 = await store.list(ListTasksRequest(page_size=2, page_token=p2.next_page_token))
        self.assertEqual([t.id for t in p3.tasks], ["task-00"])
        self.assertFalse(bool(p3.next_page_token))

        # Invalid page token raises InvalidParamsError
        with self.assertRaises(InvalidParamsError):
            await store.list(ListTasksRequest(page_token="invalid-token-value"))

    async def test_list_projections_history_length_and_artifacts(self) -> None:
        store = SQLiteTaskStore(self.db_path)
        task = _create_task(
            "t-proj",
            history_texts=["h1", "h2", "h3"],
            artifact_texts=["art1", "art2"],
        )
        await store.save(task)

        # Full retrieval by default
        default_resp = await store.list(ListTasksRequest())
        t_def = default_resp.tasks[0]
        self.assertEqual(len(t_def.history), 3)
        self.assertEqual(len(t_def.artifacts), 2)

        # history_length projection: keep last 2 history items
        req_hist2 = SimpleNamespace(
            context_id="",
            status=0,
            page_size=10,
            page_token="",
            history_length=2,
            artifacts=True,
        )
        resp_hist2 = await store.list(req_hist2)
        t_hist2 = resp_hist2.tasks[0]
        self.assertEqual(len(t_hist2.history), 2)
        self.assertEqual([h.parts[0].text for h in t_hist2.history], ["h2", "h3"])
        self.assertEqual(len(t_hist2.artifacts), 2)

        # history_length = 0 clears history
        req_hist0 = SimpleNamespace(
            context_id="",
            status=0,
            page_size=10,
            page_token="",
            history_length=0,
            artifacts=True,
        )
        resp_hist0 = await store.list(req_hist0)
        self.assertEqual(len(resp_hist0.tasks[0].history), 0)

        # artifacts = False strips artifacts
        req_no_art = SimpleNamespace(
            context_id="",
            status=0,
            page_size=10,
            page_token="",
            history_length=None,
            artifacts=False,
        )
        resp_no_art = await store.list(req_no_art)
        self.assertEqual(len(resp_no_art.tasks[0].artifacts), 0)
        self.assertEqual(len(resp_no_art.tasks[0].history), 3)

        # Verify underlying store task remains full
        persisted = await store.get("t-proj")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(len(persisted.history), 3)
        self.assertEqual(len(persisted.artifacts), 2)

    async def test_owner_scoping_and_loopback_none_context(self) -> None:
        def resolver(ctx):
            if ctx is None:
                return "default-loopback"
            return getattr(ctx, "user", "anonymous")

        store = SQLiteTaskStore(self.db_path, owner_resolver=resolver)

        # Context None on local loopback
        t_loopback = _create_task("t-loop")
        await store.save(t_loopback, context=None)
        self.assertIsNotNone(await store.get("t-loop", context=None))

        # Separate owner context
        ctx_alice = SimpleNamespace(user="alice")
        ctx_bob = SimpleNamespace(user="bob")

        t_alice = _create_task("t-shared-id", status_text="Alice's task")
        t_bob = _create_task("t-shared-id", status_text="Bob's task")

        await store.save(t_alice, context=ctx_alice)
        await store.save(t_bob, context=ctx_bob)

        alice_loaded = await store.get("t-shared-id", context=ctx_alice)
        bob_loaded = await store.get("t-shared-id", context=ctx_bob)
        self.assertIsNotNone(alice_loaded)
        self.assertIsNotNone(bob_loaded)
        assert alice_loaded is not None and bob_loaded is not None
        self.assertEqual(alice_loaded.status.message.parts[0].text, "Alice's task")
        self.assertEqual(bob_loaded.status.message.parts[0].text, "Bob's task")

        # Request bindings are also owner-scoped
        fp = b"owner-fp"
        await store.bind_request("req-scoped", fp, _create_task("t-a"), context=ctx_alice)
        await store.bind_request("req-scoped", fp, _create_task("t-b"), context=ctx_bob)

        req_alice = await store.request_task("req-scoped", fp, context=ctx_alice)
        req_bob = await store.request_task("req-scoped", fp, context=ctx_bob)
        self.assertIsNotNone(req_alice)
        self.assertIsNotNone(req_bob)
        assert req_alice is not None and req_bob is not None
        self.assertEqual(req_alice.id, "t-a")
        self.assertEqual(req_bob.id, "t-b")


if __name__ == "__main__":
    unittest.main()
