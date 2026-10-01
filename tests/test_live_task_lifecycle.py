"""Opt-in real harness lifecycle checks through a fresh MCP client."""

import asyncio
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@unittest.skipUnless(os.environ.get("BRIDGE_LIVE_TASK_LIFECYCLE") == "1", "requires live signed-in harnesses")
class LiveTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_paging_cancel_and_reopen_after_gateway_restart(self):
        repository = Path(__file__).resolve().parents[1]

        async def exercise(agent):
            root = repository / ".runtime" / ("live-task-" + agent + "-" + uuid.uuid4().hex[:8])
            root.mkdir(parents=True)
            marker = "TASK_REVIEW_" + uuid.uuid4().hex
            source = root / "calculation.py"
            original = f'MARKER = "{marker}"\n\ndef clamp(value, low, high):\n    return min(low, max(value, high))\n'
            source.write_text(original, encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if key not in {"BRIDGE_CODEX_URL", "BRIDGE_ANTIGRAVITY_URL", "BRIDGE_AGENTS_JSON"}}
            env.update(BRIDGE_WORKSPACE=str(root), BRIDGE_TASK_REGISTRY=str(root / "tickets.json"),
                       BRIDGE_AGY_COMMAND=str(repository / "bin" / "agy.exe"))
            params = StdioServerParameters(command=sys.executable, args=["-m", "agent_bridge.mcp_server"], env=env)
            recorded = []
            ticket = None
            async with asyncio.timeout(360):
                for restart in (False, True):
                    async with stdio_client(params) as (reader, writer):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()

                            async def call(name, **arguments):
                                result = await client.call_tool(name, arguments)
                                self.assertFalse(result.isError, result.content)
                                data = result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))
                                recorded.append({"operation": name, "result": data})
                                return data

                            if restart:
                                reopened = await call("check_task", task_id=ticket["task_id"])
                                self.assertEqual(reopened["state"], "TASK_STATE_COMPLETED", reopened)
                                self.assertIn(marker, reopened["text"])
                                continue
                            arguments = {"agent_id": agent, "workspace": str(root), "tool_policy": "read_only"}
                            if agent == "antigravity":
                                arguments.update(model="gemini-3.8-flash-high", reasoning_effort="high")
                            ticket = await call("submit_task", **arguments,
                                                prompt="Review calculation.py using native file reading. Include the exact MARKER, explain the clamp bug and give corrected code. Reply briefly.",
                                                request_id=str(uuid.uuid4()))
                            waiting = await call("wait_task", task_id=ticket["task_id"], timeout_seconds=0.05)
                            self.assertIn(waiting["state"], {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"})
                            result = await call("wait_task", task_id=ticket["task_id"], timeout_seconds=180)
                            self.assertEqual(result["state"], "TASK_STATE_COMPLETED", result)
                            chunks, cursor = [], 0
                            while cursor is not None:
                                page = await call("get_result", task_id=ticket["task_id"], cursor=cursor, limit=200)
                                chunks.append(page["text"])
                                cursor = page["next_cursor"]
                            self.assertEqual("".join(chunks), result["text"])
                            self.assertIn(marker, result["text"])
                            transcript = await call("get_transcript", task_id=ticket["task_id"])
                            self.assertTrue(transcript["items"])
                            cancelled = await call("submit_task", **arguments,
                                                   prompt="Perform a very detailed review of calculation.py and all edge cases. Read the source first.")
                            await call("wait_task", task_id=cancelled["task_id"], timeout_seconds=0.1)
                            outcome = await call("cancel_task", task_id=cancelled["task_id"])
                            self.assertEqual(outcome["state"], "TASK_STATE_CANCELED", outcome)
                            self.assertEqual((await call("cancel_task", task_id=cancelled["task_id"]))["state"], "TASK_STATE_CANCELED")
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            (root / "trace.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=2), encoding="utf-8")
            print("LIVE_TASK_RESULT=" + json.dumps({"agent": agent, "task_id": ticket["task_id"], "state": "passed", "trace": str(root / "trace.json")}), flush=True)

        await asyncio.gather(exercise("antigravity"), exercise("codex"))
