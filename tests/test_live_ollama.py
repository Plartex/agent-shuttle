"""Opt-in live local tests; never run in CI without BRIDGE_LIVE_OLLAMA_MODEL."""

import os
import unittest
from pathlib import Path

from agent_bridge.profiles import AgentProfile
from agent_bridge.registry import build_profile


@unittest.skipUnless(os.environ.get("BRIDGE_LIVE_OLLAMA_MODEL"), "local Ollama model not configured")
class LiveOllamaTests(unittest.IsolatedAsyncioTestCase):
    async def _ask(self, runtime: str) -> None:
        model = os.environ["BRIDGE_LIVE_OLLAMA_MODEL"]
        profile = AgentProfile.from_mapping({
            "id": f"{runtime}-live", "runtime": runtime, "provider": "ollama",
            "workspace": str(Path(__file__).resolve().parents[1]),
            "default_model": model, "allowed_models": [model],
            "reasoning_efforts": ["none"] if runtime == "opencode" else [],
            "max_tool_policy": "no_tools",
            "turn_timeout_seconds": float(os.environ.get("BRIDGE_LIVE_TURN_TIMEOUT_SECONDS", "120")),
            "runtime_command": os.environ.get(
                "BRIDGE_LIVE_OPENCODE_COMMAND" if runtime == "opencode" else "BRIDGE_LIVE_CLAUDE_COMMAND",
                "opencode" if runtime == "opencode" else "claude",
            ),
        })
        backend, _ = build_profile(profile)
        try:
            reply = await backend.run(
                "Reply with only the word OK.", tool_policy="no_tools",
                reasoning_effort="none" if runtime == "opencode" else None,
            )
            self.assertTrue(reply.text.strip())
            self.assertIsInstance(reply.usage, dict)
        finally:
            await backend.close()

    async def test_opencode_local_model(self):
        await self._ask("opencode")

    async def test_claude_code_local_model(self):
        await self._ask("claude_code")


if __name__ == "__main__":
    unittest.main()
