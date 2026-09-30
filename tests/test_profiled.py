import tempfile
import unittest
from pathlib import Path

from agent_bridge.profiled import ProfiledBackend, ProfiledInfo
from agent_bridge.profiles import AgentProfile, ToolPolicy


class FakeSession:
    def __init__(self):
        self.prompts = []
        self.closed = False

    async def ask(self, prompt):
        self.prompts.append(prompt)
        return f"answer {len(self.prompts)}"

    async def close(self):
        self.closed = True


class FakeRuntime:
    def __init__(self):
        self.selections = []
        self.sessions = []
        self.closed = False

    async def open_session(self, selection):
        self.selections.append(selection)
        session = FakeSession()
        self.sessions.append(session)
        return session

    async def discover(self):
        return {"healthy": True, "version": "test"}

    async def close(self):
        self.closed = True


class ProfiledBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = AgentProfile.from_mapping({
            "id": "local", "runtime": "opencode", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "max_tool_policy": "read_only",
            "reasoning_efforts": ["high"],
        })
        self.runtime = FakeRuntime()
        self.backend = ProfiledBackend(self.profile, self.runtime)

    async def test_one_shot_closes_session(self):
        answer = await self.backend.run("hello", "test", tool_policy="no_tools")
        self.assertEqual(answer, "answer 1")
        self.assertTrue(self.runtime.sessions[0].closed)
        self.assertEqual(self.runtime.selections[0].tool_policy, ToolPolicy.NO_TOOLS)

    async def test_persistent_session_pins_selection(self):
        session = await self.backend.open_session(
            "test", reasoning_effort="high", tool_policy="read_only"
        )
        self.assertEqual(await session.ask("first"), "answer 1")
        self.assertEqual(await session.ask("second"), "answer 2")
        self.assertEqual(self.runtime.selections[0].model, "ollama/test")
        await session.close()
        self.assertTrue(self.runtime.sessions[0].closed)

    async def test_legacy_read_only_maps_to_read_only(self):
        await self.backend.run("hello", read_only=True)
        self.assertEqual(self.runtime.selections[0].tool_policy, ToolPolicy.READ_ONLY)

    async def test_rejects_conflicting_policy_and_unsupported_model_before_runtime(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await self.backend.run("hello", read_only=True, tool_policy="workspace_write")
        with self.assertRaises(ValueError):
            await self.backend.run("hello", model="other")
        self.assertEqual(self.runtime.selections, [])

    async def test_cleanup_error_does_not_hide_generation_error(self):
        class BrokenSession:
            async def ask(self, prompt):
                raise TimeoutError("generation timed out")

            async def close(self):
                raise RuntimeError("cleanup failed")

        async def open_session(selection):
            return BrokenSession()

        self.runtime.open_session = open_session
        with self.assertRaisesRegex(TimeoutError, "generation timed out"):
            await self.backend.run("hello")

    async def test_info_reports_profile_and_runtime_without_model_turn(self):
        info = ProfiledInfo(self.profile, self.runtime)
        result = await info.fetch()
        self.assertEqual(result["capabilities"]["models"], ["ollama/test"])
        self.assertEqual(result["capabilities"]["max_tool_policy"], "read_only")
        self.assertEqual(result["runtime"]["version"], "test")
        self.assertFalse(result["usage"]["available"])
        self.assertEqual(self.runtime.sessions, [])
        self.assertNotIn("usage", await info.fetch(usage=False))


if __name__ == "__main__":
    unittest.main()
