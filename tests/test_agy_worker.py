import asyncio
import io
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from agent_bridge import agy_worker


class WorkerTests(unittest.TestCase):
    def test_main_success_and_error_envelopes(self):
        request = io.StringIO(json.dumps({"prompt": "hello", "workspace": "C:/repo"}))
        output = io.StringIO()
        with patch.object(sys, "stdin", request), patch.object(sys, "stdout", output), \
             patch("agent_bridge.agy_worker._run", new_callable=AsyncMock, return_value="ok"):
            agy_worker.main()
        self.assertEqual(json.loads(output.getvalue().split("=", 1)[1]), {"ok": True, "text": "ok"})

        request = io.StringIO("invalid")
        output = io.StringIO()
        with patch.object(sys, "stdin", request), patch.object(sys, "stdout", output):
            agy_worker.main()
        self.assertFalse(json.loads(output.getvalue().split("=", 1)[1])["ok"])

    def test_sdk_agent_is_used_by_worker(self):
        class Agent:
            def __init__(self, config):
                self.config = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def chat(self, prompt):
                self.prompt = prompt
                return types.SimpleNamespace(text=AsyncMock(return_value="reply"))

        fake = types.ModuleType("google.antigravity")
        fake.Agent = Agent
        fake.LocalAgentConfig = lambda **kwargs: kwargs
        with patch.dict(sys.modules, {"google.antigravity": fake}):
            result = asyncio.run(agy_worker._run("hello", "C:/repo", "model"))
        self.assertEqual(result, "reply")


if __name__ == "__main__":
    unittest.main()
