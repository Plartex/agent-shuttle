import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_bridge.a2a_server import make_app
from agent_bridge.backends import AntigravityCliBackend
from agent_bridge.task_store import SQLiteTaskStore
from tests.test_task_lifecycle import SlowBackend


class FakeAntigravity(SlowBackend, AntigravityCliBackend):
    def __init__(self, workspace):
        SlowBackend.__init__(self)
        AntigravityCliBackend.__init__(self, workspace)

    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False,
                  tool_policy=None, on_event=None):
        return await SlowBackend.run(self, prompt, model, reasoning_effort=reasoning_effort, read_only=read_only)


class McpTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_work_wait_cancel_pages_and_gateway_shutdown_over_real_transports(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            backend = FakeAntigravity(Path(folder))
            store = SQLiteTaskStore(Path(folder) / ".agent-shuttle" / "tasks-antigravity.sqlite3")
            server = uvicorn.Server(uvicorn.Config(make_app("antigravity", backend, url, task_store=store),
                                                   host="127.0.0.1", port=port, log_level="error"))
            running = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.01)
            env = {**os.environ, "BRIDGE_WORKSPACE": folder,
                   "BRIDGE_ANTIGRAVITY_URL": url, "BRIDGE_AGENTS_JSON": "{}",
                   "BRIDGE_TASK_REGISTRY": str(Path(folder) / "tickets.json")}
            params = StdioServerParameters(command=sys.executable, args=["-m", "agent_bridge.mcp_server"], env=env)
            try:
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()

                        async def call(name, **args):
                            result = await client.call_tool(name, args)
                            self.assertFalse(result.isError, result.content)
                            return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))

                        first = await call("submit_task", agent_id="antigravity", prompt="cancel me")
                        await asyncio.wait_for(backend.started.wait(), 2)
                        waiting = await call("wait_task", task_id=first["task_id"], timeout_seconds=0.05)
                        self.assertEqual(waiting["state"], "TASK_STATE_WORKING")
                        self.assertFalse(backend.cancelled.is_set())
                        self.assertEqual((await call("cancel_task", task_id=first["task_id"]))["state"], "TASK_STATE_CANCELED")
                        self.assertEqual((await call("cancel_task", task_id=first["task_id"]))["state"], "TASK_STATE_CANCELED")
                        second = await call("submit_task", agent_id="antigravity", prompt="finish")
                        backend.release.set()
                        self.assertEqual((await call("wait_task", task_id=second["task_id"], timeout_seconds=2))["state"], "TASK_STATE_COMPLETED")
                        chunks = []
                        cursor = 0
                        while cursor is not None:
                            page = await call("get_result", task_id=second["task_id"], cursor=cursor, limit=5)
                            chunks.append(page["text"])
                            cursor = page["next_cursor"]
                        self.assertEqual("".join(chunks), "done: finish")
                        transcript = await call("get_transcript", task_id=second["task_id"])
                        self.assertIn("finish", json.dumps(transcript))
            finally:
                backend.release.set()
                server.should_exit = True
                await running
