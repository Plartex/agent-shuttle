import json
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge.claude_runtime import ClaudeCodeRuntime
from agent_bridge.claude_runtime import _environment, _usage
from agent_bridge.profiles import AgentProfile


class FakeProcess:
    def __init__(self, output, returncode=0, stderr=b""):
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.input = None
        self.killed = False

    async def communicate(self, payload=None):
        self.input = payload
        return self.output, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class ClaudeCodeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_access_passes_explicit_permission_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = AgentProfile.from_mapping({
                "id": "full", "runtime": "claude_code", "provider": "ollama",
                "workspace": folder, "default_model": "test", "allowed_models": ["test"],
                "max_tool_policy": "full_access",
            })
            runtime = ClaudeCodeRuntime(profile)
            session = await runtime.open_session(profile.resolve(None, None, "full_access"))
            self.assertIn("--dangerously-skip-permissions", session._command())
            await runtime.close()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = AgentProfile.from_mapping({
            "id": "claude-local", "runtime": "claude_code", "provider": "ollama",
            "workspace": self.temp.name, "default_model": "test",
            "allowed_models": ["test"], "max_tool_policy": "read_only",
            "runtime_command": "claude-test",
        })
        self.runtime = ClaudeCodeRuntime(self.profile)

    async def test_two_turns_resume_same_session_and_keep_prompt_on_stdin(self):
        calls = []

        async def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            session_id = self.session_id
            result = {"type": "result", "subtype": "success", "result": "answer", "session_id": session_id,
                      "usage": {"input_tokens": 11, "output_tokens": 4,
                                "cache_read_input_tokens": 0}}
            return FakeProcess(json.dumps(result).encode())

        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec", side_effect=spawn):
            session = await self.runtime.open_session(self.profile.resolve(None, None, "no_tools"))
            self.session_id = session.session_id
            first = await session.ask("a" * 40000)
            second = await session.ask("follow up")
            await session.close()
        self.assertEqual(first.text, "answer")
        self.assertEqual(second.usage["input_tokens"], 11)
        self.assertEqual(first.details["cache_status"], "not_supported")
        self.assertIn("--session-id", calls[0][0])
        self.assertIn("--resume", calls[1][0])
        self.assertIn("--tools", calls[0][0])
        self.assertEqual(calls[0][0][calls[0][0].index("--tools") + 1], "")
        self.assertNotIn("a" * 40000, calls[0][0])
        self.assertEqual(calls[0][1]["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:11434")
        self.assertEqual(calls[0][1]["env"]["ANTHROPIC_API_KEY"], "")
        self.assertFalse(os.path.exists(calls[0][1]["env"]["CLAUDE_CONFIG_DIR"]))

    async def test_read_only_exposes_only_read_tools(self):
        async def spawn(*args, **kwargs):
            result = {"type": "result", "result": "ok", "session_id": self.session_id}
            self.args = args
            return FakeProcess(json.dumps(result).encode())

        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec", side_effect=spawn):
            session = await self.runtime.open_session(self.profile.resolve(None, None, "read_only"))
            self.session_id = session.session_id
            await session.ask("read")
            await session.close()
        self.assertEqual(self.args[self.args.index("--tools") + 1], "Read,Glob,Grep")

    async def test_nonzero_and_invalid_json_fail_with_bounded_diagnostics(self):
        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec", return_value=FakeProcess(b"", 1, b"bad endpoint")):
            session = await self.runtime.open_session(self.profile.resolve(None, None, None))
            with self.assertRaisesRegex(RuntimeError, "bad endpoint"):
                await session.ask("hello")
            await session.close()
        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec", return_value=FakeProcess(b"not json")):
            session = await self.runtime.open_session(self.profile.resolve(None, None, None))
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                await session.ask("hello")
            await session.close()

    async def test_rejects_wrong_session_error_result_and_empty_answer(self):
        cases = [
            ({"type": "result", "result": "ok", "session_id": "other"}, "different session"),
            ({"type": "result", "result": "failed", "is_error": True}, "turn failed"),
            ({"type": "result", "result": "", "session_id": "SELF"}, "no text"),
            ({"type": "message", "result": "ok"}, "unexpected"),
        ]
        for result, message in cases:
            with self.subTest(message=message):
                session = await self.runtime.open_session(self.profile.resolve(None, None, None))
                result = {key: (session.session_id if value == "SELF" else value)
                          for key, value in result.items()}
                with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec",
                           return_value=FakeProcess(json.dumps(result).encode())):
                    with self.assertRaisesRegex(RuntimeError, message):
                        await session.ask("hello")
                await session.close()

    async def test_runtime_close_cleans_unclosed_sessions(self):
        session = await self.runtime.open_session(self.profile.resolve(None, None, None))
        path = session.config_dir.name
        self.assertTrue(os.path.isdir(path))
        await self.runtime.close()
        self.assertTrue(session.closed)
        self.assertFalse(os.path.exists(path))

    async def test_timeout_kills_process_and_closes_session(self):
        process = FakeProcess(b"")
        async def timeout(*_args):
            raise asyncio.TimeoutError
        process.communicate = timeout
        session = await self.runtime.open_session(self.profile.resolve(None, None, None))
        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(asyncio.TimeoutError):
                await session.ask("task")
        self.assertTrue(process.killed)
        await session.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await session.ask("task")

    async def test_discover_and_version_failure(self):
        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec",
                   return_value=FakeProcess(b"2.1.259\n")):
            self.assertEqual((await self.runtime.discover())["version"], "2.1.259")
        with patch("agent_bridge.claude_runtime.asyncio.create_subprocess_exec",
                   return_value=FakeProcess(b"", 1, b"bad binary")):
            with self.assertRaisesRegex(RuntimeError, "bad binary"):
                await self.runtime.discover()

    async def test_workspace_write_omits_safe_mode_and_explicit_effort(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = AgentProfile.from_mapping({
                "id": "cloud", "runtime": "claude_code", "provider": "anthropic",
                "workspace": folder, "default_model": "test", "allowed_models": ["test"],
                "max_tool_policy": "workspace_write", "reasoning_efforts": ["high"],
            })
            runtime = ClaudeCodeRuntime(profile)
            session = await runtime.open_session(profile.resolve(None, "high", "workspace_write"))
            command = session._command()
            self.assertNotIn("--safe-mode", command)
            self.assertNotIn("--tools", command)
            self.assertEqual(command[command.index("--effort") + 1], "high")
            await runtime.close()


class ClaudeEnvironmentTests(unittest.TestCase):
    def test_cloud_credentials_are_explicit_and_redacted_from_local(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = AgentProfile.from_mapping({
                "id": "cloud", "runtime": "claude_code", "provider": "anthropic",
                "workspace": workspace, "default_model": "test",
                "allowed_models": ["test"], "credential_env": "ANTHROPIC_API_KEY",
            })
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "top-secret", "OPENAI_API_KEY": "other"}):
                env = _environment(profile, workspace)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "top-secret")
            self.assertNotIn("OPENAI_API_KEY", env)
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
                with self.assertRaisesRegex(RuntimeError, "Missing credential"):
                    _environment(profile, workspace)

    def test_usage_does_not_invent_cache_for_ollama(self):
        usage, details = _usage({"usage": {"input_tokens": 10, "output_tokens": 2}}, "ollama")
        self.assertEqual(usage["total_tokens"], 12)
        self.assertNotIn("cache_read_tokens", usage)
        self.assertEqual(details["cache_status"], "not_supported")
        self.assertTrue(details["estimated"])

    def test_cloud_usage_reports_cache_and_endpoint(self):
        with tempfile.TemporaryDirectory() as workspace:
            profile = AgentProfile.from_mapping({
                "id": "cloud", "runtime": "claude_code", "provider": "anthropic",
                "workspace": workspace, "endpoint": "https://example.com",
                "default_model": "test", "allowed_models": ["test"],
            })
            self.assertEqual(_environment(profile, workspace)["ANTHROPIC_BASE_URL"],
                             "https://example.com")
        usage, details = _usage({"usage": {"input_tokens": 2, "output_tokens": 3,
                                           "cache_read_input_tokens": 4,
                                           "cache_creation_input_tokens": 5}}, "anthropic")
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(details["cache_status"], "reported")


if __name__ == "__main__":
    unittest.main()
