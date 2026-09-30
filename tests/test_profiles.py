"""Contract tests for configured agent profiles and security policy."""

import tempfile
import unittest
import json
from pathlib import Path

from agent_bridge.profiles import AgentProfile, ToolPolicy


class AgentProfileTests(unittest.TestCase):
    def test_full_access_requires_profile_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            base = {
                "id": "local", "runtime": "opencode", "provider": "ollama",
                "workspace": folder, "default_model": "test", "allowed_models": ["test"],
            }
            with self.assertRaisesRegex(ValueError, "exceeds profile maximum"):
                AgentProfile.from_mapping(base).resolve(None, None, "full_access")
            profile = AgentProfile.from_mapping({**base, "max_tool_policy": "full_access"})
            self.assertEqual(profile.resolve(None, None, "full_access").tool_policy, ToolPolicy.FULL_ACCESS)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def profile(self, **overrides):
        values = {
            "id": "opencode-local",
            "runtime": "opencode",
            "provider": "ollama",
            "workspace": str(self.workspace),
            "endpoint": "http://127.0.0.1:11434",
            "default_model": "qwen3.5",
            "allowed_models": ["qwen3.5", "other:latest"],
            "max_tool_policy": "no_tools",
        }
        values.update(overrides)
        return AgentProfile.from_mapping(values)

    def test_resolves_qualified_opencode_model_and_default_policy(self):
        profile = self.profile()
        selection = profile.resolve(None, None, None)
        self.assertEqual(selection.model, "ollama/qwen3.5")
        self.assertEqual(selection.tool_policy, ToolPolicy.NO_TOOLS)
        self.assertIsNone(selection.reasoning_effort)

    def test_rejects_unlisted_model_and_provider_switch(self):
        profile = self.profile()
        with self.assertRaisesRegex(ValueError, "not allowed"):
            profile.resolve("missing", None, None)
        with self.assertRaisesRegex(ValueError, "provider"):
            profile.resolve("openrouter/qwen3.5", None, None)

    def test_denies_policy_escalation(self):
        profile = self.profile()
        with self.assertRaisesRegex(ValueError, "exceeds"):
            profile.resolve(None, None, "workspace_write")

    def test_read_only_and_no_tools_are_distinct(self):
        profile = self.profile(max_tool_policy="read_only")
        self.assertEqual(profile.resolve(None, None, "no_tools").tool_policy, ToolPolicy.NO_TOOLS)
        self.assertEqual(profile.resolve(None, None, "read_only").tool_policy, ToolPolicy.READ_ONLY)
        with self.assertRaises(ValueError):
            profile.resolve(None, None, "workspace_write")

    def test_reasoning_requires_declared_support(self):
        profile = self.profile()
        with self.assertRaisesRegex(ValueError, "reasoning"):
            profile.resolve(None, "high", None)
        profile = self.profile(reasoning_efforts=["low", "high"])
        self.assertEqual(profile.resolve(None, "high", None).reasoning_effort, "high")

    def test_rejects_remote_ollama_endpoint_for_local_profile(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            self.profile(endpoint="https://example.com")

    def test_rejects_unknown_keys_and_missing_workspace(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.profile(secret_value="bad")
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.profile(workspace=str(self.workspace / "missing"))

    def test_claude_model_is_not_provider_qualified(self):
        profile = self.profile(runtime="claude_code")
        self.assertEqual(profile.resolve(None, None, None).model, "qwen3.5")

    def test_provider_model_id_may_contain_slash(self):
        profile = self.profile(
            provider="openrouter", endpoint="https://openrouter.ai/api/v1",
            allowed_models=["anthropic/claude-test"], default_model="anthropic/claude-test",
        )
        self.assertEqual(profile.resolve(None, None, None).model,
                         "openrouter/anthropic/claude-test")
        self.assertEqual(profile.resolve("openrouter/anthropic/claude-test", None, None).model,
                         "openrouter/anthropic/claude-test")

    def test_file_resolves_workspace_relative_to_profile(self):
        profile_path = self.workspace / "profile.json"
        profile_path.write_text(json.dumps({
            "id": "local", "runtime": "claude_code", "provider": "ollama",
            "workspace": ".", "default_model": "test", "allowed_models": ["test"],
        }), encoding="utf-8")
        self.assertEqual(AgentProfile.from_file(profile_path).workspace, self.workspace.resolve())

    def test_file_without_workspace_defaults_to_profile_directory(self):
        profile_path = self.workspace / "portable.json"
        profile_path.write_text(json.dumps({
            "id": "local", "runtime": "claude_code", "provider": "ollama",
            "default_model": "test", "allowed_models": ["test"],
        }), encoding="utf-8")
        self.assertEqual(AgentProfile.from_file(profile_path).workspace, self.workspace.resolve())

    def test_rejects_non_loopback_attached_runtime(self):
        with self.assertRaisesRegex(ValueError, "runtime_url"):
            self.profile(runtime_url="http://example.com:4096")

    def test_rejects_invalid_profile_structure(self):
        cases = [
            ({"runtime": "other"}, "runtime"),
            ({"provider": ""}, "provider"),
            ({"provider": "a/b"}, "provider"),
            ({"workspace": str(self.workspace / "missing")}, "workspace"),
            ({"endpoint": "http://example.com"}, "loopback"),
            ({"provider": "anthropic", "endpoint": "http://example.com"}, "HTTPS"),
            ({"allowed_models": []}, "allowed_models"),
            ({"allowed_models": [1]}, "allowed_models"),
            ({"default_model": "missing"}, "default_model"),
            ({"max_tool_policy": "invalid"}, "tool policy"),
            ({"default_tool_policy": "read_only"}, "exceeds"),
            ({"reasoning_efforts": "high"}, "reasoning_efforts"),
            ({"reasoning_efforts": [""]}, "reasoning_efforts"),
            ({"id": ""}, "id"),
            ({"runtime_command": ""}, "runtime_command"),
            ({"turn_timeout_seconds": 0}, "turn_timeout_seconds"),
            ({"turn_timeout_seconds": True}, "turn_timeout_seconds"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.profile(**overrides)

    def test_rejects_invalid_requested_policy_and_non_object_file(self):
        with self.assertRaisesRegex(ValueError, "Unknown tool policy"):
            self.profile().resolve(None, None, "invalid")
        path = self.workspace / "profile.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            AgentProfile.from_file(path)

    def test_shipped_ollama_examples_are_valid_profiles(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        for name in ("opencode-ollama.json", "claude-code-ollama.json"):
            with self.subTest(name=name):
                profile = AgentProfile.from_file(examples / name)
                self.assertEqual(profile.provider, "ollama")
                self.assertEqual(profile.workspace, examples.parent)
                self.assertEqual(profile.resolve(None, None, "read_only").tool_policy,
                                 ToolPolicy.READ_ONLY)


if __name__ == "__main__":
    unittest.main()
