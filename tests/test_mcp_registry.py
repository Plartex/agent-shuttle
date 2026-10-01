"""MCP routing and automatic peer startup contracts."""

import json
import os
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_bridge.mcp_server import (
    _agent_launch, ask_agent, ask_antigravity, ask_codex,
    get_agent_info, get_antigravity_info, get_codex_info,
)


class McpRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_manage_their_peers(self):
        launches = []

        @asynccontextmanager
        async def connected(launch):
            launches.append(launch)
            yield SimpleNamespace(url=launch.url)

        result = SimpleNamespace(task_id="1", context_id="c", state="done", text="ok",
                                 usage={"input_tokens": 3}, details={"source": "test"})
        with patch.dict(os.environ, {"BRIDGE_WORKSPACE": str(Path.cwd())}, clear=True), \
             patch("agent_bridge.mcp_server.connect_harness", connected), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock,
                   return_value=result) as ask, \
             patch("agent_bridge.mcp_server.BridgeClient.info", new_callable=AsyncMock,
                   return_value={"ok": True}) as info:
            self.assertEqual((await ask_agent("codex", "task"))["text"], "ok")
            self.assertEqual((await ask_codex("task"))["details"], {"source": "test"})
            self.assertEqual((await ask_antigravity("task"))["usage"]["input_tokens"], 3)
            self.assertEqual(await get_agent_info("codex"), {"ok": True})
            self.assertEqual(await get_codex_info(), {"ok": True})
            self.assertEqual(await get_antigravity_info(), {"ok": True})

        self.assertEqual([launch.name for launch in launches],
                         ["codex", "codex", "antigravity", "codex", "codex", "antigravity"])
        self.assertEqual(ask.await_count, 3)
        self.assertEqual(info.await_count, 3)
        self.assertTrue(all(launch.start_if_missing for launch in launches))

    async def test_antigravity_timeout_and_policy_reach_launch(self):
        @asynccontextmanager
        async def connected(launch):
            self.assertEqual(launch.name, "antigravity")
            self.assertEqual(launch.workspace, Path.cwd())
            self.assertEqual(launch.tool_policy, "full_access")
            self.assertEqual(launch.agy_turn_timeout_seconds, 45)
            yield SimpleNamespace(url=launch.url)

        result = SimpleNamespace(task_id="1", context_id=None, state="done", text="ok",
                                 usage=None, details=None)
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent_bridge.mcp_server.connect_harness", connected), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock,
                   return_value=result):
            answer = await ask_antigravity("task", workspace=str(Path.cwd()),
                                           tool_policy="full_access", turn_timeout_seconds=45)
        self.assertEqual(answer["text"], "ok")

    async def test_custom_profile_configuration(self):
        mapping = {"local": {"harness": "opencode", "profile": "profiles/opencode.json",
                             "url": "http://127.0.0.1:8767"}}
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": json.dumps(mapping)}, clear=True):
            launch = _agent_launch("local")
        self.assertEqual(launch.name, "opencode")
        self.assertEqual(launch.profile_path, Path("profiles/opencode.json"))
        self.assertEqual(launch.url, "http://127.0.0.1:8767")

    async def test_registry_rejects_untrusted_urls_and_names(self):
        mapping = {"remote": {"harness": "codex", "url": "https://example.com"}}
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": json.dumps(mapping)}, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback"):
                _agent_launch("remote")
            with self.assertRaisesRegex(ValueError, "agent_id"):
                _agent_launch("../remote")
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "not-json"}, clear=True):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                _agent_launch("local")

    async def test_custom_id_needs_harness_for_startup(self):
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON":
                                  '{"local":"http://127.0.0.1:8767"}'}, clear=True):
            with self.assertRaisesRegex(ValueError, "Configure harness"):
                _agent_launch("local")

    async def test_codex_uses_fresh_peer_when_running_catalog_lacks_requested_model(self):
        launches = []

        @asynccontextmanager
        async def connected(launch):
            launches.append(launch)
            yield SimpleNamespace(url=launch.url, started=len(launches) > 1)

        result = SimpleNamespace(task_id="1", context_id=None, state="TASK_STATE_COMPLETED",
                                 text="OK", usage=None, details=None)
        with patch.dict(os.environ, {"BRIDGE_CODEX_URL": "http://127.0.0.1:8765"}, clear=True), \
             patch("agent_bridge.mcp_server.connect_harness", connected), \
             patch("agent_bridge.mcp_server._free_local_url",
                   return_value="http://127.0.0.1:49152"), \
             patch("agent_bridge.mcp_server.BridgeClient.capabilities", new_callable=AsyncMock,
                   return_value={"read_only_tools": True,
                                 "capabilities": {"models": [{"id": "gpt-6-astra"}]}}), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock,
                   return_value=result) as ask:
            answer = await ask_codex("hello", model="gpt-6-sol")

        self.assertEqual(answer["state"], "TASK_STATE_COMPLETED")
        self.assertEqual([launch.url for launch in launches],
                         ["http://127.0.0.1:8765", "http://127.0.0.1:49152"])
        self.assertEqual(launches[1].tool_policy, "read_only")
        self.assertEqual(ask.call_args.args[0], "http://127.0.0.1:49152")

    async def test_legacy_custom_url_still_routes_to_running_server(self):
        result = SimpleNamespace(task_id="1", context_id=None, state="done", text="ok",
                                 usage=None, details=None)
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON":
                                  '{"local":"http://127.0.0.1:8767"}'}, clear=True), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock,
                   return_value=result) as ask, \
             patch("agent_bridge.mcp_server.BridgeClient.info", new_callable=AsyncMock,
                   return_value={"agent": "local"}) as info:
            self.assertEqual((await ask_agent("local", "hello"))["text"], "ok")
            self.assertEqual(await get_agent_info("local"), {"agent": "local"})
        self.assertEqual(ask.call_args.args[:2], ("http://127.0.0.1:8767", "hello"))
        info.assert_awaited_once_with("http://127.0.0.1:8767")


if __name__ == "__main__":
    unittest.main()
