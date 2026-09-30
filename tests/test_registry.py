import tempfile
import unittest

from agent_bridge.profiled import ProfiledBackend, ProfiledInfo
from agent_bridge.profiles import AgentProfile
from agent_bridge.registry import build_profile


class RegistryTests(unittest.TestCase):
    def test_builds_both_new_runtimes_without_starting_a_model(self):
        with tempfile.TemporaryDirectory() as workspace:
            for runtime in ("opencode", "claude_code"):
                with self.subTest(runtime=runtime):
                    profile = AgentProfile.from_mapping({
                        "id": runtime, "runtime": runtime, "provider": "ollama",
                        "workspace": workspace, "default_model": "test",
                        "allowed_models": ["test"],
                    })
                    backend, info = build_profile(profile)
                    self.assertIsInstance(backend, ProfiledBackend)
                    self.assertIsInstance(info, ProfiledInfo)
                    self.assertEqual(backend.profile.id, runtime)


if __name__ == "__main__":
    unittest.main()
