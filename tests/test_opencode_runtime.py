import tempfile
import unittest
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_bridge.opencode_runtime import OpenCodeRuntime, _child_env, _inline_config, _resolve_command, _usage
from agent_bridge.profiles import AgentProfile, ToolPolicy


class OpenCodeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        def respond(request):
            self.calls.append(request)
            if request.url.path == "/global/health":
                return httpx.Response(200, json={"healthy": True, "version": "1.2.3"})
            if request.url.path == "/session" and request.method == "POST":
                return httpx.Response(200, json={"id": "session-1"})
            if request.url.path == "/session/session-1/message":
                return httpx.Response(200, json={
                    "info": {"id": "message-1", "role": "assistant", "tokens": {
                        "input": 12, "output": 5, "reasoning": 2,
                        "cache": {"read": 3, "write": 0},
                    }},
                    "parts": [{"type": "text", "text": "first"}, {"type": "text", "text": " answer"}],
                })
            if request.url.path == "/session/session-1" and request.method == "DELETE":
                return httpx.Response(200, json=True)
            return httpx.Response(404)

        profile = AgentProfile.from_mapping({
            "id": "open-local", "runtime": "opencode", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "max_tool_policy": "no_tools",
            "runtime_url": "http://127.0.0.1:4096",
        })
        self.profile = profile
        self.runtime = OpenCodeRuntime(profile, transport=httpx.MockTransport(respond))

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_session_message_usage_and_cleanup(self):
        session = await self.runtime.open_session(self.profile.resolve(None, None, None))
        result = await session.ask("inspect code")
        self.assertEqual(result.text, "first answer")
        self.assertEqual(result.usage["input_tokens"], 12)
        self.assertEqual(result.usage["cache_read_tokens"], 3)
        self.assertEqual(result.details["usage_source"], "opencode_message")
        request = next(x for x in self.calls if x.url.path.endswith("/message"))
        self.assertEqual(request.method, "POST")
        payload = __import__("json").loads(request.content)
        self.assertEqual(payload["model"], {"providerID": "ollama", "modelID": "test"})
        self.assertEqual(payload["agent"], "bridge")
        self.assertEqual(payload["parts"], [{"type": "text", "text": "inspect code"}])
        self.assertTrue(all(enabled is False for enabled in payload["tools"].values()))
        await session.close()
        self.assertEqual(self.calls[-1].method, "DELETE")

    async def test_discovery_uses_health_without_model_turn(self):
        self.assertEqual((await self.runtime.discover())["version"], "1.2.3")
        self.assertEqual(len(self.calls), 1)

    async def test_external_server_rejected_for_strict_policy_without_test_transport(self):
        runtime = OpenCodeRuntime(self.profile)
        try:
            with self.assertRaisesRegex(RuntimeError, "attached"):
                await runtime.open_session(self.profile.resolve(None, None, ToolPolicy.NO_TOOLS))
        finally:
            await runtime.close()

    async def test_invalid_reply_fails_and_closing_is_idempotent(self):
        async def invalid(request):
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "session-1"})
            if request.url.path.endswith("/message"):
                return httpx.Response(200, json={"info": {}, "parts": []})
            return httpx.Response(200, json=True)

        runtime = OpenCodeRuntime(self.profile, transport=httpx.MockTransport(invalid))
        try:
            session = await runtime.open_session(self.profile.resolve(None, None, None))
            with self.assertRaisesRegex(RuntimeError, "no text"):
                await session.ask("hello")
            await session.close()
            await session.close()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await session.ask("hello")
        finally:
            await runtime.close()

    async def test_absolute_turn_deadline_aborts_long_running_generation(self):
        calls = []

        async def respond(request):
            calls.append(request.url.path)
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "session-1"})
            if request.url.path.endswith("/message"):
                await asyncio.sleep(1)
            return httpx.Response(200, json=True)

        profile = AgentProfile.from_mapping({
            "id": "deadline", "runtime": "opencode", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "runtime_url": "http://127.0.0.1:4096",
            "turn_timeout_seconds": 0.01,
        })
        runtime = OpenCodeRuntime(profile, transport=httpx.MockTransport(respond))
        try:
            session = await runtime.open_session(profile.resolve(None, None, None))
            with self.assertRaises(TimeoutError):
                await session.ask("hello")
            self.assertIn("/session/session-1/abort", calls)
            await session.close()
        finally:
            await runtime.close()

    async def test_configured_ollama_reasoning_variant_is_sent_per_turn(self):
        profile = AgentProfile.from_mapping({
            "id": "effort", "runtime": "opencode", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "reasoning_efforts": ["none"],
            "runtime_url": "http://127.0.0.1:4096",
        })
        runtime = OpenCodeRuntime(profile, transport=self.runtime.transport)
        try:
            session = await runtime.open_session(profile.resolve(None, "none", None))
            await session.ask("hello")
            request = next(x for x in self.calls if x.url.path.endswith("/message"))
            self.assertEqual(json.loads(request.content)["variant"], "none")
            await session.close()
        finally:
            await runtime.close()
        config = json.loads(_inline_config(profile, ToolPolicy.NO_TOOLS))
        self.assertEqual(
            config["provider"]["ollama"]["models"]["test"]["variants"]["none"],
            {"reasoningEffort": "none"},
        )

    async def test_managed_server_has_password_and_inline_policy(self):
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return self.returncode

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False

            async def get(self, path, **kwargs):
                return httpx.Response(200, json={"healthy": True, "version": "test"},
                                      request=httpx.Request("GET", "http://127.0.0.1" + path))

            async def post(self, path, **kwargs):
                return httpx.Response(200, json={"id": "s1"},
                                      request=httpx.Request("POST", "http://127.0.0.1" + path))

            async def delete(self, path):
                return httpx.Response(200, json=True,
                                      request=httpx.Request("DELETE", "http://127.0.0.1" + path))

            async def aclose(self):
                self.closed = True

        profile = AgentProfile.from_mapping({
            "id": "managed", "runtime": "opencode", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "max_tool_policy": "no_tools",
        })
        captured = []

        async def spawn(*args, **kwargs):
            captured.append((args, kwargs))
            return FakeProcess()

        with patch("agent_bridge.opencode_runtime.asyncio.create_subprocess_exec", side_effect=spawn), \
             patch("agent_bridge.opencode_runtime.httpx.AsyncClient", FakeClient):
            runtime = OpenCodeRuntime(profile)
            session = await runtime.open_session(profile.resolve(None, None, None))
            await session.close()
            await runtime.close()
        args, kwargs = captured[0]
        self.assertEqual(Path(args[0]).stem, "opencode")
        self.assertEqual(args[1:3], ("--pure", "serve"))
        self.assertTrue(kwargs["env"]["OPENCODE_SERVER_PASSWORD"])
        config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertNotIn("steps", config["agent"]["bridge"])
        self.assertEqual(config["provider"]["ollama"]["options"]["baseURL"],
                         "http://127.0.0.1:11434/v1")


class OpenCodeConfigTests(unittest.TestCase):
    def test_full_access_allows_all_tools_and_external_directories(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = AgentProfile.from_mapping({
                "id": "full", "runtime": "opencode", "provider": "ollama",
                "workspace": workspace, "default_model": "test",
                "allowed_models": ["test"], "max_tool_policy": "full_access",
            })
            config = json.loads(_inline_config(profile, ToolPolicy.FULL_ACCESS))
            self.assertEqual(config["permission"], {"*": "allow"})
            self.assertNotIn("read-only", config["agent"]["bridge"]["prompt"])

    @unittest.skipUnless(os.name == "nt", "Windows npm shim")
    def test_windows_npm_shim_resolves_real_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = __import__("pathlib").Path(folder)
            shim = root / "opencode.cmd"
            shim.touch()
            executable = root / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            self.assertEqual(_resolve_command(str(shim)), str(executable))

    def test_inline_read_policy_and_usage_semantics(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = AgentProfile.from_mapping({
                "id": "read", "runtime": "opencode", "provider": "ollama",
                "workspace": workspace, "default_model": "test",
                "allowed_models": ["test"], "max_tool_policy": "read_only",
            })
            config = json.loads(_inline_config(profile, ToolPolicy.READ_ONLY))
            self.assertEqual(config["permission"].get("edit", config["permission"]["*"]), "deny")
            self.assertEqual(config["permission"]["read"], "allow")
            self.assertEqual(config["permission"]["*"], "deny")
            usage, details = _usage({"tokens": {"input": 10, "output": 2}})
            self.assertEqual(usage["total_tokens"], 12)
            self.assertEqual(details["cache_status"], "unknown")

    def test_child_env_does_not_inherit_unrelated_cloud_key(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = AgentProfile.from_mapping({
                "id": "local", "runtime": "opencode", "provider": "ollama",
                "workspace": workspace, "default_model": "test",
                "allowed_models": ["test"],
            })
            with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-leak"}):
                env = _child_env(profile, ToolPolicy.NO_TOOLS, "password")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], "password")


if __name__ == "__main__":
    unittest.main()
