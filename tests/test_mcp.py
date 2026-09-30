import os
import sys
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class McpGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_agent_tools_are_listed(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_bridge.mcp_server"],
            env={
                **os.environ,
                "BRIDGE_CODEX_URL": "http://127.0.0.1:8765",
                "BRIDGE_ANTIGRAVITY_URL": "http://127.0.0.1:8766",
            },
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                for tool in tools:
                    if tool.name.startswith("ask_"):
                        self.assertIn("model", tool.inputSchema["properties"])
                        self.assertIn("reasoning_effort", tool.inputSchema["properties"])
                    if tool.name == "ask_antigravity":
                        self.assertIn("workspace", tool.inputSchema["properties"])
                        self.assertIn("tool_policy", tool.inputSchema["properties"])
                        self.assertIn("turn_timeout_seconds", tool.inputSchema["properties"])
        self.assertEqual(
            names,
            {
                "ask_codex", "ask_antigravity", "get_codex_info", "get_antigravity_info",
                "ask_agent", "get_agent_info",
            },
        )


if __name__ == "__main__":
    unittest.main()
