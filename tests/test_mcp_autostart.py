"""MCP must manage A2A peers without a manual serve step."""

import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_bridge import mcp_server


class McpAutostartTests(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_agent_uses_managed_server_without_url_configuration(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"BRIDGE_WORKSPACE": folder}, clear=True):
            launches = []

            @asynccontextmanager
            async def connection(launch):
                launches.append(launch)
                yield SimpleNamespace(url=launch.url, started=True)

            client = SimpleNamespace(ask=AsyncMock(return_value=SimpleNamespace(
                task_id="task-1", context_id="context-1", state="TASK_STATE_COMPLETED",
                text="done", usage=None, details=None,
            )))
            with patch.object(mcp_server, "connect_harness", connection), \
                 patch.object(mcp_server, "BridgeClient", return_value=client):
                result = await mcp_server.ask_agent("codex", "Summarize this project")

            self.assertEqual(result["text"], "done")
            self.assertEqual(launches[0].name, "codex")
            self.assertEqual(launches[0].workspace, Path(folder).resolve())
            self.assertTrue(launches[0].url.startswith("http://127.0.0.1:"))
            client.ask.assert_awaited_once()

    async def test_configured_profile_starts_when_url_is_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "opencode.json"
            profile.write_text("{}", encoding="utf-8")
            config = '{"local": {"harness": "opencode", "profile": "' + str(profile).replace("\\", "\\\\") + '"}}'
            with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": config,
                                      "BRIDGE_WORKSPACE": folder}, clear=True):
                launch = mcp_server._agent_launch("local")
            self.assertEqual(launch.name, "opencode")
            self.assertEqual(launch.profile_path, profile)
            self.assertTrue(launch.start_if_missing)

    async def test_explicit_url_is_local_and_reusable(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"BRIDGE_CODEX_URL": "http://127.0.0.1:8765",
                                      "BRIDGE_WORKSPACE": folder}, clear=True):
                launch = mcp_server._agent_launch("codex")
            self.assertEqual(launch.url, "http://127.0.0.1:8765")
            self.assertTrue(launch.start_if_missing)

    async def test_rejects_remote_url(self):
        with patch.dict(os.environ, {"BRIDGE_CODEX_URL": "http://example.com:8765"}, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback"):
                mcp_server._agent_launch("codex")


if __name__ == "__main__":
    unittest.main()
