import asyncio
import socket
import tempfile
import unittest
import uuid
from pathlib import Path

import uvicorn

from agent_bridge.a2a_server import make_app
from agent_bridge.client import BridgeClient
from agent_bridge.task_store import SQLiteTaskStore
from tests.test_task_lifecycle import SlowBackend


class TaskRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_and_request_binding_survive_restart_without_second_backend_run(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            path = Path(folder) / "tasks.sqlite3"
            request_id = str(uuid.uuid4())
            first = SlowBackend()
            first.release.set()
            saved_id = None
            for index, backend in enumerate((first, SlowBackend())):
                server = uvicorn.Server(uvicorn.Config(make_app("slow", backend, url, task_store=SQLiteTaskStore(path)),
                                                       host="127.0.0.1", port=port, log_level="error"))
                running = asyncio.create_task(server.serve())
                while not server.started:
                    if running.done():
                        await running
                    await asyncio.sleep(0.01)
                try:
                    client = BridgeClient(timeout_seconds=2)
                    handle = await client.submit(url, "once", request_id=request_id)
                    if index == 0:
                        saved_id = handle.task_id
                        self.assertEqual((await handle.wait(2)).state, "TASK_STATE_COMPLETED")
                    else:
                        self.assertEqual(handle.task_id, saved_id)
                        self.assertEqual((await handle.status()).text, "done: once")
                        self.assertEqual(backend.calls, 0)
                finally:
                    backend.release.set()
                    server.should_exit = True
                    await running
