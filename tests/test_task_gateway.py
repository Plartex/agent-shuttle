import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_bridge.client import BridgeClient, BridgeResult, TaskHandle
from agent_bridge.managed import HarnessLaunch
from agent_bridge.mcp_tasks import TaskGateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_keeps_peer_alive_until_shutdown_and_cancels_active_task(self):
        closed = []

        @asynccontextmanager
        async def connect(launch):
            try:
                yield SimpleNamespace(url=launch.url)
            finally:
                closed.append(launch.url)

        with tempfile.TemporaryDirectory() as folder:
            client = BridgeClient()
            client.submit = AsyncMock(return_value=TaskHandle(client, "http://127.0.0.1:1234", "job", "context"))
            client.task_status = AsyncMock(return_value=BridgeResult("url", "job", "context", "TASK_STATE_WORKING", ""))
            client.cancel_task = AsyncMock(return_value=BridgeResult("url", "job", "context", "TASK_STATE_CANCELED", ""))
            gateway = TaskGateway(Path(folder) / "registry.json", client=client)
            launch = HarnessLaunch("antigravity", "http://127.0.0.1:1234", Path(folder), tool_policy="read_only")
            with patch("agent_bridge.mcp_tasks.connect_harness", connect):
                ticket = await gateway.submit(launch, "review", "gemini-3.8-flash-high", "high", None)
                self.assertEqual(ticket["task_id"], "job")
                self.assertFalse(closed)
                self.assertEqual((await gateway.handle("job")).task_id, "job")
                await gateway.close()
            client.cancel_task.assert_awaited_once()
            self.assertEqual(closed, [launch.url])

    async def test_registry_reopens_task_after_gateway_restart_without_resubmitting(self):
        @asynccontextmanager
        async def connect(launch):
            yield SimpleNamespace(url=launch.url)

        with tempfile.TemporaryDirectory() as folder:
            client = BridgeClient()
            client.submit = AsyncMock(return_value=TaskHandle(client, "http://127.0.0.1:1234", "job", "context"))
            client.task_status = AsyncMock(return_value=BridgeResult("url", "job", "context", "TASK_STATE_COMPLETED", "done"))
            path = Path(folder) / "registry.json"
            launch = HarnessLaunch("antigravity", "http://127.0.0.1:1234", Path(folder), tool_policy="read_only")
            with patch("agent_bridge.mcp_tasks.connect_harness", connect):
                first = TaskGateway(path, client=client)
                await first.submit(launch, "review", None, None, None)
                await first.close()
                second = TaskGateway(path, client=client)
                reopened = await second.handle("job")
                self.assertEqual((await reopened.status()).text, "done")
                await second.close()
            client.submit.assert_awaited_once()
