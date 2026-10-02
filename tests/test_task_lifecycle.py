"""Public task lifecycle contracts, exercised through a real A2A server."""

import asyncio
import socket
import unittest
import uuid

import httpx
import uvicorn

from agent_bridge.a2a_server import make_app
from agent_bridge.client import BridgeClient


class SlowBackend:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.sessions = []
        self.calls = 0

    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return f"done: {prompt}"

    async def open_session(self, model=None, *, reasoning_effort=None, read_only=False):
        session = SlowSession(self)
        self.sessions.append(session)
        return session


class SlowSession:
    def __init__(self, backend):
        self.backend = backend
        self.closed = False
        self.close_calls = 0

    async def ask(self, prompt):
        return await self.backend.run(prompt)

    async def close(self):
        self.close_calls += 1
        self.closed = True


class TaskLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.backend = SlowBackend()
        self.app = make_app("slow", self.backend, self.url)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app,
            host="127.0.0.1", port=port, log_level="error",
        ))
        self.running = asyncio.create_task(self.server.serve())
        async with httpx.AsyncClient() as http:
            for _ in range(100):
                try:
                    if (await http.get(self.url + "/.well-known/agent-card.json")).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.03)
            else:
                self.fail("A2A server did not start")

    async def asyncTearDown(self):
        self.backend.release.set()
        self.server.should_exit = True
        await self.running

    async def test_submit_returns_before_completion_and_wait_timeout_does_not_cancel(self):
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "slow request")
        self.assertTrue(handle.task_id)
        await asyncio.wait_for(self.backend.started.wait(), 2)
        waiting = await handle.wait(timeout=1)
        self.assertIn(waiting.state, {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"})
        self.assertFalse(self.backend.cancelled.is_set())
        self.backend.release.set()
        done = await handle.wait(timeout=5)
        self.assertEqual(done.state, "TASK_STATE_COMPLETED")
        self.assertEqual(done.text, "done: slow request")
        self.assertEqual((await handle.result()).task_id, handle.task_id)

    async def test_a2a_projects_the_library_owned_task_with_same_id(self):
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "shared core")
        self.backend.release.set()
        self.assertEqual((await handle.result()).state, "TASK_STATE_COMPLETED")
        core_task = await self.app.state.task_manager.get(handle.task_id)
        self.assertEqual((await core_task.result()).text, "done: shared core")

    async def test_cancel_stops_running_backend_and_is_observable(self):
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "cancel me")
        await asyncio.wait_for(self.backend.started.wait(), 2)
        cancelled = await handle.cancel()
        self.assertEqual(cancelled.state, "TASK_STATE_CANCELED")
        self.assertTrue(self.backend.cancelled.is_set())
        self.assertEqual((await handle.status()).state, "TASK_STATE_CANCELED")
        self.assertEqual((await handle.cancel()).state, "TASK_STATE_CANCELED")

    async def test_cancelled_session_is_closed_and_cannot_be_reused_silently(self):
        client = BridgeClient(timeout_seconds=3)
        session_id = str(uuid.uuid4())
        handle = await client.submit(self.url, "first turn", session_id=session_id)
        await asyncio.wait_for(self.backend.started.wait(), 2)
        self.assertEqual((await handle.cancel()).state, "TASK_STATE_CANCELED")
        self.assertTrue(self.backend.sessions[0].closed)
        next_turn = await client.ask(self.url, "second turn", session_id=session_id)
        self.assertEqual(next_turn.state, "TASK_STATE_FAILED")
        self.assertIn("closed after cancellation", next_turn.text)
        self.assertEqual(len(self.backend.sessions), 1)
        self.assertEqual(self.backend.sessions[0].close_calls, 1)

    async def test_cancelling_blocking_ask_cancels_remote_task(self):
        call = asyncio.create_task(BridgeClient(timeout_seconds=3).ask(self.url, "blocking"))
        await asyncio.wait_for(self.backend.started.wait(), 2)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(self.backend.cancelled.wait(), 2)

    async def test_task_can_be_reopened_from_id_and_streams_status_updates(self):
        first_client = BridgeClient(timeout_seconds=3)
        handle = await first_client.submit(self.url, "reconnect")
        await asyncio.wait_for(self.backend.started.wait(), 2)
        reopened = BridgeClient(timeout_seconds=3).task(self.url, handle.task_id)
        self.assertEqual((await reopened.status()).state, "TASK_STATE_WORKING")

        seen = asyncio.Queue()

        async def observe():
            events = []
            async for event in reopened.events():
                events.append(event)
                await seen.put(event)
            return events

        observing = asyncio.create_task(observe())
        first = await asyncio.wait_for(seen.get(), 2)
        self.assertEqual(first.state, "TASK_STATE_WORKING")
        self.backend.release.set()
        events = await asyncio.wait_for(observing, 2)
        self.assertEqual(events[-1].state, "TASK_STATE_COMPLETED")
        self.assertEqual((await reopened.result()).text, "done: reconnect")

    async def test_retried_submit_with_same_request_id_does_not_run_twice(self):
        client = BridgeClient(timeout_seconds=3)
        request_id = str(uuid.uuid4())
        first = await client.submit(self.url, "once", request_id=request_id)
        second = await client.submit(self.url, "once", request_id=request_id)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(self.backend.calls, 1)
        with self.assertRaises(Exception):
            await client.submit(self.url, "different", request_id=request_id)
        self.backend.release.set()
        self.assertEqual((await second.result()).state, "TASK_STATE_COMPLETED")

    async def test_wait_budget_is_respected_even_when_status_transport_hangs(self):
        client = BridgeClient(timeout_seconds=3)

        async def hung_status(*args):
            await asyncio.Event().wait()

        client.task_status = hung_status
        waiting = client.task(self.url, "unknown").wait(timeout=0.05)
        snapshot = await asyncio.wait_for(waiting, 0.5)
        self.assertEqual(snapshot.state, "TASK_STATE_SUBMITTED")

    async def test_worker_execution_timeout_fails_task_and_stops_backend(self):
        await self._reconfigure(execution_timeout_seconds=0.1)
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "stalled")
        result = await handle.wait(2)
        self.assertEqual(result.state, "TASK_STATE_FAILED")
        self.assertEqual(result.error["code"], "worker_timeout")
        self.assertTrue(self.backend.cancelled.is_set())

    async def _reconfigure(self, **options):
        self.server.should_exit = True
        await self.running
        port = int(self.url.rsplit(":", 1)[1])
        self.server = uvicorn.Server(uvicorn.Config(
            make_app("slow", self.backend, self.url, **options),
            host="127.0.0.1", port=port, log_level="error",
        ))
        self.running = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)

    async def test_silent_worker_has_distinct_stalled_error_and_is_cancelled(self):
        await self._reconfigure(execution_timeout_seconds=2, stall_timeout_seconds=0.1)
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "stalled")
        result = await handle.wait(2)
        self.assertEqual(result.state, "TASK_STATE_FAILED")
        self.assertEqual(result.error["code"], "worker_stalled")
        self.assertTrue(self.backend.cancelled.is_set())

    async def test_native_activity_extends_stall_budget_and_is_present_in_transcript(self):
        async def active(prompt, model=None, *, reasoning_effort=None, read_only=False, on_event=None):
            for _ in range(8):
                await on_event({"kind": "tool", "text": "reading source"})
                await asyncio.sleep(0.03)
            return "done"
        self.backend.run = active
        await self._reconfigure(execution_timeout_seconds=2, stall_timeout_seconds=0.12)
        handle = await BridgeClient(timeout_seconds=3).submit(self.url, "active")
        self.assertEqual((await handle.wait(2)).state, "TASK_STATE_COMPLETED")
        transcript = await handle.transcript()
        self.assertIn("agent_bridge.event", str(transcript))

    async def test_backend_failure_preserves_structured_error(self):
        async def fail(*args, **kwargs):
            raise ValueError("broken configuration")
        self.backend.run = fail
        result = await BridgeClient(timeout_seconds=3).ask(self.url, "failure")
        self.assertEqual(result.state, "TASK_STATE_FAILED")
        self.assertEqual(result.error["type"], "ValueError")
        self.assertFalse(result.error["retryable"])

    async def test_cancelled_bridge_session_refuses_followup_locally(self):
        client = BridgeClient(timeout_seconds=3)
        async with client.session(self.url) as session:
            call = asyncio.create_task(session.ask("first turn"))
            await asyncio.wait_for(self.backend.started.wait(), 2)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                await session.ask("second turn")
        self.assertEqual(len(self.backend.sessions), 1)
        self.assertEqual(self.backend.sessions[0].close_calls, 1)
