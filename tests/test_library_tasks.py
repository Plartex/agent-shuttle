import asyncio
import contextlib
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from agent_bridge.backends import BackendResponse
from agent_bridge.task_library import TaskManager


class FakeSession:
    def __init__(self):
        self.turns = 0
        self.closed = False

    async def ask(self, prompt, *, on_event=None):
        self.turns += 1
        turn = self.turns
        await asyncio.sleep(0.01)
        if on_event:
            await on_event({"kind": "progress", "text": f"turn {turn}"})
        return f"{turn}:{prompt}"

    async def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.sessions = []
        self.release = asyncio.Event()

    async def run(self, prompt, model=None, *, reasoning_effort=None,
                  read_only=False, tool_policy=None, on_event=None):
        self.calls.append(prompt)
        if on_event:
            await on_event({"kind": "progress", "text": "started"})
        if prompt == "wait":
            await self.release.wait()
        return f"done:{prompt}"

    async def open_session(self, model=None, *, reasoning_effort=None,
                           read_only=False, tool_policy=None):
        session = FakeSession()
        self.sessions.append(session)
        return session


class LibraryTasksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "tasks.sqlite3"
        self.backend = FakeBackend()

    def manager(self):
        return TaskManager({"fake": self.backend}, workspace=self.root,
                           database=self.database, execution_timeout_seconds=5)

    async def test_python_api_dispatch_wait_result_and_persistence_without_transports(self):
        request_id = str(uuid.uuid4())
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "hello", tool_policy="read_only",
                                          request_id=request_id)
            result = await task.result()
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.text, "done:hello")
            self.assertEqual((await task.result_page(limit=5))["text"], "done:")
            self.assertTrue((await task.transcript())["items"])
            self.assertEqual((await manager.dispatch("fake", "hello", tool_policy="read_only",
                                                     request_id=request_id)).id, task.id)
            with self.assertRaises(ValueError):
                await manager.dispatch("fake", "different", request_id=request_id)
            task_id = task.id
        self.assertEqual(self.backend.calls, ["hello"])
        async with self.manager() as reopened:
            self.assertEqual((await (await reopened.get(task_id)).result()).text, "done:hello")
            self.assertEqual(len(await reopened.list_tasks()), 1)

    async def test_sessions_are_reusable_listable_and_endable(self):
        async with self.manager() as manager:
            session = await manager.create_session("fake", model="model-a", tool_policy="read_only")
            first = await session.dispatch("one")
            second = await session.dispatch("two")
            self.assertEqual((await first.result()).text, "1:one")
            self.assertEqual((await second.result()).text, "2:two")
            self.assertEqual(len(self.backend.sessions), 1)
            self.assertEqual((await manager.list_sessions())[0].id, session.id)
            self.assertTrue(await session.end())
            self.assertTrue(self.backend.sessions[0].closed)
            with self.assertRaises(RuntimeError):
                await session.dispatch("three")

    async def test_wait_budget_does_not_cancel_and_explicit_cancel_does(self):
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "wait")
            self.assertIn((await task.wait(0.01)).state, {"submitted", "working"})
            self.assertEqual((await task.cancel()).state, "canceled")
            self.assertEqual((await task.cancel()).state, "canceled")

    async def test_parallel_turns_share_one_native_session_and_cancel_invalidates_it(self):
        async with self.manager() as manager:
            session = await manager.create_session("fake")
            first, second = await asyncio.gather(session.dispatch("one"), session.dispatch("two"))
            self.assertEqual((await first.result()).text, "1:one")
            self.assertEqual((await second.result()).text, "2:two")
            self.assertEqual(len(self.backend.sessions), 1)
            waiting = await session.dispatch("wait")
            self.assertEqual((await waiting.cancel()).state, "canceled")
            with self.assertRaises(RuntimeError):
                await session.dispatch("after cancel")

    async def test_recovery_marks_only_interrupted_tasks_failed_without_replay(self):
        async with self.manager() as manager:
            completed = await manager.dispatch("fake", "done")
            await completed.result()
            completed_id = completed.id
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            conn.execute("UPDATE tasks SET state='working' WHERE id=?", (completed_id,))
            conn.commit()
        async with self.manager() as reopened:
            result = await (await reopened.get(completed_id)).result()
            self.assertEqual(result.state, "failed")
            self.assertEqual(result.error["code"], "worker_interrupted")
        self.assertEqual(self.backend.calls, ["done"])

    async def test_silent_worker_fails_with_structured_error(self):
        async with TaskManager({"fake": self.backend}, workspace=self.root,
                               database=self.database, stall_timeout_seconds=0.05) as manager:
            task = await manager.dispatch("fake", "wait")
            result = await task.result()
            self.assertEqual(result.state, "failed")
            self.assertEqual(result.error["code"], "worker_stalled")

    async def test_transport_event_failure_does_not_fail_library_task(self):
        async def broken_sink(event):
            raise ConnectionError("subscriber disconnected")

        async with self.manager() as manager:
            task = await manager.dispatch("fake", "hello", event_sink=broken_sink)
            self.assertEqual((await task.result()).text, "done:hello")

    async def test_shutdown_marks_unstarted_task_canceled(self):
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "wait")
            task_id = task.id
        async with self.manager() as manager:
            self.assertEqual((await manager.status(task_id)).state, "canceled")

    async def test_library_agent_catalog_and_persisted_model_preference(self):
        async with self.manager() as manager:
            agents = await manager.list_agents()
            self.assertEqual([agent.id for agent in agents], ["fake"])
            self.assertTrue(agents[0].supports_sessions)
            await manager.set_preference("fake", model="preferred-model", reasoning_effort="high")
            task = await manager.dispatch("fake", "hello")
            self.assertEqual((await task.result()).requested_model, "preferred-model")
        async with self.manager() as manager:
            self.assertEqual((await manager.get_preference("fake"))["reasoning_effort"], "high")

    async def test_idle_session_reaper_closes_native_session(self):
        async with self.manager() as manager:
            session = await manager.create_session("fake")
            await (await session.dispatch("hello")).result()
            manager._db().execute("UPDATE sessions SET updated_at=0 WHERE id=?", (session.id,))
            manager._db().commit()
            self.assertEqual(await manager.reap_idle_sessions(1), [session.id])
            self.assertTrue(self.backend.sessions[0].closed)

    async def test_resume_capable_backend_reopens_session_after_manager_restart(self):
        class ResumableSession(FakeSession):
            native_id = "native-thread-1"

        class ResumableBackend(FakeBackend):
            async def open_session(self, *args, **kwargs):
                session = ResumableSession()
                self.sessions.append(session)
                return session

            async def resume_session(self, native_id, *args, **kwargs):
                self.resumed_id = native_id
                return await self.open_session(*args, **kwargs)

        backend = ResumableBackend()
        async with TaskManager({"fake": backend}, workspace=self.root, database=self.database) as manager:
            session = await manager.create_session("fake")
            await (await session.dispatch("first")).result()
            session_id = session.id
        async with TaskManager({"fake": backend}, workspace=self.root, database=self.database) as manager:
            self.assertEqual((await manager.list_sessions())[0].state, "suspended")
            session = await manager.session(session_id)
            self.assertEqual((await (await session.dispatch("second")).result()).state, "completed")
            self.assertEqual(backend.resumed_id, "native-thread-1")

    async def test_session_with_interrupted_turn_is_not_resumed(self):
        class ResumableSession(FakeSession):
            native_id = "native-thread-1"

        class ResumableBackend(FakeBackend):
            async def open_session(self, *args, **kwargs):
                return ResumableSession()

            async def resume_session(self, native_id, *args, **kwargs):
                raise AssertionError("unsafe resume")

        backend = ResumableBackend()
        async with TaskManager({"fake": backend}, workspace=self.root, database=self.database) as manager:
            session = await manager.create_session("fake")
            task = await session.dispatch("first")
            await task.result()
            session_id, task_id = session.id, task.id
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            conn.execute("UPDATE sessions SET state='open' WHERE id=?", (session_id,))
            conn.execute("UPDATE tasks SET state='working' WHERE id=?", (task_id,))
            conn.commit()
        async with TaskManager({"fake": backend}, workspace=self.root, database=self.database) as manager:
            self.assertEqual((await manager.list_sessions())[0].state, "interrupted")

    async def test_clean_git_workspace_reports_new_files_per_task(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

        class EditingBackend(FakeBackend):
            async def run(self, prompt, *args, **kwargs):
                (self_root / "created.txt").write_text("created", encoding="utf-8")
                return "done"

        self_root = self.root
        async with TaskManager({"fake": EditingBackend()}, workspace=self.root,
                               database=self.root / ".git" / "library.sqlite3") as manager:
            task = await manager.dispatch("fake", "create")
            result = await task.result()
            self.assertEqual(result.files_changed, ("created.txt",))
            self.assertEqual(result.files_changed_state, "collected")

    async def test_structured_backend_metadata_is_exposed_in_result(self):
        class MetadataBackend(FakeBackend):
            async def run(self, prompt, *args, **kwargs):
                return BackendResponse("answer", {"input_tokens": 3},
                                       {"observed_model": "actual-model", "warnings": ["quota near limit"]})

        async with TaskManager({"fake": MetadataBackend()}, workspace=self.root,
                               database=self.database) as manager:
            result = await (await manager.dispatch("fake", "hello", model="requested-model")).result()
            self.assertEqual(result.requested_model, "requested-model")
            self.assertEqual(result.observed_model, "actual-model")
            self.assertEqual(result.warnings, ("quota near limit",))

    async def test_task_events_replay_journal_through_terminal_state(self):
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "hello")
            kinds = [event["kind"] async for event in task.events()]
            self.assertIn("submitted", kinds)
            self.assertIn("progress", kinds)
            self.assertEqual(kinds[-1], "completed")


if __name__ == "__main__":
    unittest.main()
