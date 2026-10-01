"""Real scoped delegation from a plain MCP client, without caller AGENTS.md."""

import asyncio
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@unittest.skipUnless(os.environ.get("BRIDGE_LIVE_AGY_SCOPED") == "1",
                     "requires BRIDGE_LIVE_AGY_SCOPED=1")
class LiveScopedMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_mcp_client_reviews_then_edits_with_scoped_permissions(self):
        repository = Path(__file__).resolve().parents[1]
        root = repository / ".runtime" / ("agy-scoped-mcp-" + uuid.uuid4().hex[:10])
        root.mkdir(parents=True)
        source = root / "calculation.py"
        marker = "REVIEW_" + uuid.uuid4().hex
        original = f'REVIEW_MARKER = "{marker}"\n\ndef clamp(value, low, high):\n    return min(low, max(value, high))\n'
        source.write_text(original, encoding="utf-8")
        (root / ".agents").mkdir()
        (root / ".agents" / "hooks.json").write_text(json.dumps({
            "untrusted-project-hook": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"command": "echo INVALID_PROJECT_HOOK", "timeout": 10},
            ]}]},
        }), encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key not in {"BRIDGE_CODEX_URL", "BRIDGE_ANTIGRAVITY_URL", "BRIDGE_AGENTS_JSON"}}
        env["BRIDGE_AGY_COMMAND"] = str(repository / "bin" / "agy.exe")
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_bridge.mcp_server"], env=env)
        results = []
        async with asyncio.timeout(240):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    for arguments in (
                        {"prompt": "Review calculation.py. Explain the clamp bug, recommend a fix, and include the exact REVIEW_MARKER value from the file. Use native file reading. Reply concisely.",
                         "workspace": str(root), "model": "gemini-3.8-flash-high"},
                        {"prompt": "Fix the clamp bug in calculation.py using native file editing. Preserve REVIEW_MARKER. Do not run shell commands or create other files. Reply briefly.",
                         "workspace": str(root), "model": "gemini-3.8-flash-high", "tool_policy": "workspace_write"},
                    ):
                        result = await client.call_tool("ask_antigravity", arguments)
                        self.assertFalse(result.isError, result.content)
                        data = result.structuredContent
                        if data is None:
                            data = json.loads(next(item.text for item in result.content if item.type == "text"))
                        results.append(data)
                        print("SCOPED_MCP_RESULT=" + json.dumps(data, ensure_ascii=False), flush=True)
                        self.assertEqual(data["state"], "TASK_STATE_COMPLETED", data["text"])
                        if len(results) == 1:
                            self.assertIn(marker, data["text"])
                            self.assertEqual(source.read_text(encoding="utf-8"), original)
                            self.assertEqual(data["details"]["tool_policy"], "read_only")
                        else:
                            self.assertEqual(data["details"]["tool_policy"], "workspace_write")
        namespace = {"__builtins__": {"min": min, "max": max}}
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
        self.assertEqual(namespace["REVIEW_MARKER"], marker)
        for value, expected in ((-5, 0), (5, 5), (15, 10)):
            self.assertEqual(namespace["clamp"](value, 0, 10), expected)
        (root / "trace.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print("SCOPED_MCP_TRACE=" + str(root / "trace.json"), flush=True)


if __name__ == "__main__":
    unittest.main()
