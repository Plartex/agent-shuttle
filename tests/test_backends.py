import asyncio
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from agent_bridge.backends import (
    AntigravityAuthenticationError, AntigravityCliBackend, AntigravityPermissionDenied,
    AntigravitySdkBackend, CodexBackend,
    _AntigravityCliSession, _decode_agy_result, _decode_agy_usage,
    _run_codex_thread,
)
from agent_bridge.agy_policy import ScopedAgyPolicy


class AntigravityCliBackendTests(unittest.TestCase):
    @staticmethod
    def decode(payload: dict[str, object], stderr: str = "") -> str:
        return _decode_agy_result(json.dumps(payload).encode(), stderr.encode())

    def test_returns_non_empty_success_response(self) -> None:
        self.assertEqual(self.decode({"status": "SUCCESS", "response": "ok"}), "ok")

    def test_rejects_empty_success_response(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "soft-denied.*ViewFile"):
            self.decode(
                {"status": "SUCCESS", "response": ""},
                'Tool "ViewFile" requires confirmation',
            )

    def test_rejects_partial_success_when_command_was_denied(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "RunCommand"):
            self.decode({
                "status": "SUCCESS", "response": "Partial work completed",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            })

    def test_rejects_empty_success_with_structured_denial(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ViewFile"):
            self.decode({
                "status": "SUCCESS", "response": "",
                "denied_actions": [{"action": "read_file", "display_name": "ViewFile"}],
            })

    def test_full_permissions_setting_requires_boolean(self) -> None:
        with self.assertRaisesRegex(TypeError, "boolean"):
            AntigravityCliBackend(Path.cwd(), dangerously_skip_permissions="false")

    def test_rejects_invalid_json_with_stderr_context(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid JSON.*backend unavailable"):
            _decode_agy_result(b"not-json", b"backend unavailable")

    def test_extracts_only_nonnegative_token_counters(self) -> None:
        payload = {"usage": {
            "input_tokens": 123, "output_tokens": 7, "total_tokens": 130,
            "thinking_tokens": -1, "cache_read_tokens": True, "other": 99,
        }}
        self.assertEqual(
            _decode_agy_usage(json.dumps(payload).encode()),
            {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130},
        )


class _FakeWriter:
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, data):
        self.lines.append(data)

    async def drain(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, events):
        self.stdin = _FakeWriter()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class AntigravitySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_exit_after_prompt_reports_auth_failure(self):
        process = _FakeProcess([])
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(b"You are not logged into Antigravity. Access is denied.")
        process.stderr.feed_eof()
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaises(AntigravityAuthenticationError):
                await session.ask("hello")
        finally:
            await session.close()

    async def test_stream_exit_after_prompt_preserves_other_error(self):
        process = _FakeProcess([])
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(b"model unavailable")
        process.stderr.feed_eof()
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                await session.ask("hello")
        finally:
            await session.close()

    async def test_stream_rejects_invalid_json_and_failed_result(self):
        bad_json = _FakeProcess([])
        bad_json.stdout = asyncio.StreamReader()
        bad_json.stdout.feed_data(b"not-json\n")
        bad_json.stdout.feed_eof()
        session = _AntigravityCliSession(bad_json)
        try:
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                await session.ask("hello")
        finally:
            await session.close()

        failed = _FakeProcess([{"event": "result", "result": {"status": "ERROR", "error": "model unavailable"}}])
        session = _AntigravityCliSession(failed)
        try:
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                await session.ask("hello")
        finally:
            await session.close()

    async def test_stream_rejects_empty_success_response(self):
        process = _FakeProcess([{"event": "result", "result": {"status": "SUCCESS", "response": ""}}])
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaisesRegex(RuntimeError, "empty response"):
                await session.ask("hello")
        finally:
            await session.close()

    async def test_stream_auth_failure_reports_process_context(self):
        process = _FakeProcess([])
        process.returncode = 1
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(b"You are not logged into Antigravity. Access is denied.")
        process.stderr.feed_eof()
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaisesRegex(AntigravityAuthenticationError, "outside the caller's sandbox"):
                await session.ask("hello")
        finally:
            await session.close()

    async def test_stream_turn_timeout_terminates_stalled_session(self):
        class StalledProcess(_FakeProcess):
            def __init__(self):
                super().__init__([])
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.terminated = asyncio.Event()

            async def wait(self):
                await self.terminated.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.terminated.set()

        process = StalledProcess()
        session = _AntigravityCliSession(process, turn_timeout_seconds=0.01)
        with self.assertRaisesRegex(TimeoutError, "agy session"):
            await session.ask("hello")
        self.assertEqual(process.returncode, -9)

    async def test_scoped_task_is_not_dispatched_without_a_verified_policy_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder))
            process = _FakeProcess([{"event": "result", "result": {
                "status": "SUCCESS", "response": "READY",
            }}])
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
                with self.assertRaisesRegex(RuntimeError, "policy probe"):
                    await backend.run("inspect", read_only=True)
            self.assertEqual(len(process.stdin.lines), 1)
            self.assertNotIn("inspect", json.loads(process.stdin.lines[0])["message"]["content"])
            self.assertFalse(Path(spawn.call_args.kwargs["cwd"]).exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows process tree cleanup")
    async def test_scoped_timeout_stops_tree_before_root_exit_and_removes_control_directory(self):
        class StalledProcess(_FakeProcess):
            pid = 12345

            def __init__(self):
                super().__init__([])
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stopped = asyncio.Event()

            async def wait(self):
                await self.stopped.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.stopped.set()

        with tempfile.TemporaryDirectory() as folder, ScopedAgyPolicy("read_only", Path(folder)) as policy:
            process = StalledProcess()
            session = _AntigravityCliSession(process, policy=policy, turn_timeout_seconds=0.01)
            loop = asyncio.get_running_loop()

            def stop_tree(argv, **kwargs):
                self.assertIsNone(process.returncode)
                self.assertEqual(argv, ["taskkill", "/PID", "12345", "/T", "/F"])
                loop.call_soon_threadsafe(process.kill)

            with patch("agent_bridge.backends.subprocess.run", side_effect=stop_tree) as stop:
                with self.assertRaisesRegex(TimeoutError, "agy session"):
                    await session.ask("hello")
            stop.assert_called_once()
            self.assertFalse(policy.control.exists())

    async def test_scoped_denial_is_reported_from_hook_audit_even_when_cli_reports_success(self):
        with tempfile.TemporaryDirectory() as folder:
            for native_denials in ([], [{"display_name": "WriteToFile"}]):
                with self.subTest(native_denials=native_denials), ScopedAgyPolicy("read_only", Path(folder)) as policy:
                    process = _FakeProcess([{"event": "result", "result": {
                        "status": "SUCCESS", "response": "done", "denied_actions": native_denials,
                    }}])

                    async def drain():
                        policy.audit.write_text(json.dumps({
                            "tool": "write_to_file", "decision": "deny",
                        }) + "\n", encoding="utf-8")

                    process.stdin.drain = drain
                    session = _AntigravityCliSession(process, policy=policy)
                    try:
                        with self.assertRaisesRegex(AntigravityPermissionDenied, "task policy read_only denied tools: write_to_file"):
                            await session.ask("try to write")
                    finally:
                        await session.close()

    async def test_unknown_and_conflicting_tool_policies_are_rejected_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder))
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec") as spawn:
                with self.assertRaisesRegex(ValueError, "unknown"):
                    await backend.run("inspect", tool_policy="unknown")
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    await backend.open_session(read_only=True, tool_policy="workspace_write")
            spawn.assert_not_called()

    async def test_scoped_policies_replace_cli_prompts_with_a_verified_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder), dangerously_skip_permissions=True)
            for policy in ("no_tools", "read_only", "workspace_write"):
                async def spawn(*argv, **kwargs):
                    control = Path(kwargs["cwd"])
                    config = json.loads((control / ".agents" / "hooks.json").read_text())
                    self.assertTrue(config)
                    (control / "decisions.jsonl").write_text(json.dumps({
                        "tool": "write_to_file", "decision": "deny",
                        "target": str(control / "policy-canary.txt"),
                    }) + "\n")
                    self.assertIn("--dangerously-skip-permissions", argv)
                    self.assertNotIn("--add-dir", argv)
                    if policy == "workspace_write":
                        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
                    return _FakeProcess([
                        {"event": "result", "result": {"status": "SUCCESS", "response": "READY"}},
                        {"event": "result", "result": {"status": "SUCCESS", "response": "done"}},
                    ])
                with self.subTest(policy=policy), patch(
                    "agent_bridge.backends.asyncio.create_subprocess_exec", side_effect=spawn,
                ):
                    result = await backend.run("inspect", tool_policy=policy)
                self.assertEqual(result.text, "done")
                self.assertEqual(result.details["tool_policy"], policy)

    async def test_stream_all_permissions_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            process = _FakeProcess([])
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
                session = await AntigravityCliBackend(
                    Path(folder), dangerously_skip_permissions=True,
                ).open_session()
                self.assertIn("--dangerously-skip-permissions", spawn.call_args.args)
                await session.close()

    async def test_stream_turns_and_cumulative_usage_deltas(self):
        process = _FakeProcess([
            {"event": "init", "conversation_id": "test"},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "first", "usage": {
                    "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
                    "total_tokens": 110,
                },
            }},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "second", "usage": {
                    "input_tokens": 130, "output_tokens": 15, "cache_read_tokens": 80,
                    "total_tokens": 145,
                },
            }},
        ])
        session = _AntigravityCliSession(process)
        first = await session.ask("one")
        second = await session.ask("two")
        self.assertEqual(first.text, "first")
        self.assertEqual(second.text, "second")
        self.assertEqual(first.usage["input_tokens"], 100)
        self.assertEqual(second.usage["input_tokens"], 30)
        self.assertEqual(second.usage["cache_read_tokens"], 80)
        self.assertEqual(len(process.stdin.lines), 2)
        self.assertEqual(json.loads(process.stdin.lines[1])["message"]["content"], "two")
        await session.close()
        self.assertTrue(process.stdin.closed)

    async def test_stream_activity_is_reported_before_result(self):
        process = _FakeProcess([
            {"event": "message", "message": {"content": "checking code"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "done"}},
        ])
        seen = []

        async def report(event):
            seen.append(event)

        session = _AntigravityCliSession(process)
        try:
            result = await session.ask("review", on_event=report)
        finally:
            await session.close()
        self.assertEqual(result.text, "done")
        self.assertEqual(seen[0]["kind"], "message")

    async def test_codex_stream_preserves_final_answer_with_typed_enum_phase(self):
        from openai_codex.generated.v2_all import MessagePhase, TurnStatus
        events = [
            ("item/completed", SimpleNamespace(item=SimpleNamespace(type="agentMessage", text="final", phase=MessagePhase.final_answer))),
            ("turn/completed", SimpleNamespace(turn=SimpleNamespace(status=TurnStatus.completed, error=None))),
        ]
        for _, payload in events:
            payload.model_dump = lambda **kwargs: {}

        async def stream():
            for method, payload in events:
                yield SimpleNamespace(method=method, payload=payload)

        thread = SimpleNamespace(turn=AsyncMock(return_value=SimpleNamespace(stream=stream, interrupt=AsyncMock())))
        result = await _run_codex_thread(thread, "prompt", AsyncMock())
        self.assertEqual(result.text, "final")

    async def test_stream_rejects_partial_success_when_command_was_denied(self):
        process = _FakeProcess([{"event": "result", "result": {
            "status": "SUCCESS", "response": "Partial work completed",
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
        }}])
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaisesRegex(RuntimeError, "RunCommand"):
                await session.ask("do the full task")
        finally:
            await session.close()


class CodexSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_access_session_uses_sdk_full_access_sandbox(self):
        from openai_codex import Sandbox
        started = []

        class Codex:
            def __init__(self, config):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                started.append(kwargs)
                return object()

        with patch("openai_codex.AsyncCodex", Codex):
            session = await CodexBackend(Path.cwd()).open_session(tool_policy="full_access")
            await session.close()
        self.assertEqual(started[0]["sandbox"], Sandbox.full_access)

    async def test_reuses_one_thread_and_closes_runtime(self):
        runtimes = []

        class FakeThread:
            def __init__(self):
                self.calls = []

            async def run(self, prompt):
                self.calls.append(prompt)
                last = SimpleNamespace(
                    input_tokens=100 + len(self.calls), output_tokens=5,
                    total_tokens=105 + len(self.calls), reasoning_output_tokens=2,
                    cached_input_tokens=50, cache_write_input_tokens=0,
                )
                return SimpleNamespace(
                    final_response=f"turn {len(self.calls)}",
                    usage=SimpleNamespace(last=last),
                )

        class FakeCodex:
            def __init__(self, config):
                self.thread = FakeThread()
                self.started = []
                self.closed = False
                runtimes.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            async def thread_start(self, **kwargs):
                self.started.append(kwargs)
                return self.thread

        with patch("openai_codex.AsyncCodex", FakeCodex):
            session = await CodexBackend(Path.cwd()).open_session(
                "test-model", reasoning_effort="low", read_only=True,
            )
            first = await session.ask("one")
            second = await session.ask("two")
            self.assertEqual(first.text, "turn 1")
            self.assertEqual(second.text, "turn 2")
            self.assertEqual(second.usage["input_tokens"], 102)
            self.assertEqual(second.usage["cache_read_tokens"], 50)
            await session.close()
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(len(runtimes[0].started), 1)
        self.assertEqual(runtimes[0].thread.calls, ["one", "two"])
        self.assertTrue(runtimes[0].closed)


class OneShotBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_full_access_maps_to_sdk_sandbox(self):
        from openai_codex import Sandbox
        instances = []

        class Thread:
            async def run(self, prompt):
                return SimpleNamespace(final_response="ok", usage=None)

        class Codex:
            def __init__(self, config):
                instances.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                self.settings = kwargs
                return Thread()

        with patch("openai_codex.AsyncCodex", Codex):
            await CodexBackend(Path.cwd()).run("inspect", tool_policy="full_access")
        self.assertEqual(instances[0].settings["sandbox"], Sandbox.full_access)

    async def test_agy_one_shot_timeout_kills_process(self):
        class StalledProcess:
            returncode = None
            killed = False

            async def communicate(self):
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = StalledProcess()
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
            with self.assertRaisesRegex(TimeoutError, "agy"):
                await AntigravityCliBackend(
                    Path.cwd(), turn_timeout_seconds=0.01,
                ).run("hello")
        self.assertTrue(process.killed)
        self.assertIn("--print-timeout", spawn.call_args.args)
        self.assertEqual(spawn.call_count, 1)

    async def test_codex_one_shot_returns_last_turn_usage(self):
        class Thread:
            async def run(self, prompt):
                last = SimpleNamespace(input_tokens=8, output_tokens=3, total_tokens=11,
                                       reasoning_output_tokens=1, cached_input_tokens=2,
                                       cache_write_input_tokens=None)
                return SimpleNamespace(final_response="ok", usage=SimpleNamespace(last=last))

        class Codex:
            def __init__(self, config):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                self.settings = kwargs
                return Thread()

        with patch("openai_codex.AsyncCodex", Codex):
            result = await CodexBackend(Path.cwd()).run("hello", "test", reasoning_effort="high")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage["cache_read_tokens"], 2)
        self.assertNotIn("cache_write_tokens", result.usage)

    async def test_antigravity_cli_one_shot_parses_response_and_usage(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return json.dumps({"status": "SUCCESS", "response": "ok", "usage": {
                    "input_tokens": 10, "output_tokens": 2,
                }}).encode(), b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()) as spawn:
            response = await AntigravityCliBackend(Path.cwd()).run(
                "hello", "chosen", reasoning_effort="high",
            )
        self.assertEqual(response.text, "ok")
        self.assertEqual(response.usage["input_tokens"], 10)
        self.assertIn("--model", spawn.call_args.args)
        self.assertIn("--effort", spawn.call_args.args)
        self.assertNotIn("--dangerously-skip-permissions", spawn.call_args.args)

    async def test_antigravity_keeps_stdin_open_until_headless_turn_ends(self):
        class Reader:
            def __init__(self, data):
                self.data = data

            async def read(self):
                return self.data

        class Stdin:
            closed = False

            def close(self):
                self.closed = True

        class Process:
            returncode = 0

            def __init__(self):
                self.stdin = Stdin()
                self.stdout = Reader(b'{"status":"SUCCESS","response":"OK"}')
                self.stderr = Reader(b"")

            async def wait(self):
                self.stdin_open_at_exit = not self.stdin.closed
                return 0

            async def communicate(self):
                raise AssertionError("communicate() would close stdin before the turn")

        process = Process()
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   return_value=process) as spawn:
            result = await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(result.text, "OK")
        self.assertTrue(process.stdin_open_at_exit)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.PIPE)

    async def test_antigravity_cli_all_permissions_requires_explicit_opt_in(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return b'{"status":"SUCCESS","response":"ok"}', b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()) as spawn:
            await AntigravityCliBackend(
                Path.cwd(), dangerously_skip_permissions=True,
            ).run("hello")
        self.assertIn("--dangerously-skip-permissions", spawn.call_args.args)

    async def test_antigravity_full_access_request_requires_server_opt_in(self):
        with self.assertRaisesRegex(ValueError, "full_access"):
            await AntigravityCliBackend(Path.cwd()).run("inspect", tool_policy="full_access")

        class Process:
            returncode = 0

            async def communicate(self):
                return b'{"status":"SUCCESS","response":"ok"}', b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()):
            answer = await AntigravityCliBackend(
                Path.cwd(), dangerously_skip_permissions=True,
            ).run("inspect", tool_policy="full_access")
        self.assertEqual(answer.text, "ok")

    async def test_antigravity_cli_nonzero_reports_stderr(self):
        class Process:
            returncode = 2

            async def communicate(self):
                return b"", b"bad model"

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()):
            with self.assertRaisesRegex(RuntimeError, "bad model"):
                await AntigravityCliBackend(Path.cwd()).run("hello")

    async def test_antigravity_cli_auth_failure_is_actionable_and_not_retried(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", (
                    b"error getting token source: You are not logged into Antigravity.\n"
                    b"Failed to write server states: open "
                    b"C:/Users/test/.gemini/antigravity-cli/mcp/agent-bridge/ask_agent.json.tmp: "
                    b"Access is denied.\n"
                    b"Print mode: auth timed out\n"
                    b"Error: authentication timed out."
                )

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()) as spawn:
            with self.assertRaisesRegex(
                AntigravityAuthenticationError,
                "Antigravity CLI authentication",
            ):
                await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(spawn.call_count, 1)

    async def test_antigravity_cli_signed_out_is_distinct_from_sandbox_denial(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", b"error getting token source: You are not logged into Antigravity."

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()):
            with self.assertRaises(AntigravityAuthenticationError) as captured:
                await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertIn("Verify that the CLI is signed in", str(captured.exception))
        self.assertNotIn("outside the caller's sandbox", str(captured.exception))

    async def test_antigravity_retries_only_preflight_tls_failure(self):
        class Process:
            def __init__(self, code, stdout=b"", stderr=b""):
                self.returncode = code
                self.stdout = stdout
                self.stderr = stderr

            async def communicate(self):
                return self.stdout, self.stderr

        failure = Process(1, stderr=(
            b"error: Eligibility check failed: failed to get profile picture: "
            b"net/http: TLS handshake timeout"
        ))
        success = Process(0, stdout=b'{"status":"SUCCESS","response":"OK"}')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   side_effect=[failure, success]) as spawn, \
             patch("agent_bridge.backends.asyncio.sleep"):
            answer = await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(answer.text, "OK")
        self.assertEqual(answer.details, {"preflight_retries": 1})
        self.assertEqual(spawn.call_count, 2)

    async def test_antigravity_retries_transient_eligibility_503_without_switching_model(self):
        class Process:
            def __init__(self, code, stdout=b"", stderr=b""):
                self.returncode = code
                self.stdout = stdout
                self.stderr = stderr

            async def communicate(self):
                return self.stdout, self.stderr

        unavailable = Process(1, stderr=(
            b"error: Eligibility check failed: UNAVAILABLE (code 503): "
            b"The service is currently unavailable.\n"
        ))
        success = Process(0, stdout=b'{"status":"SUCCESS","response":"OK"}')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   side_effect=[unavailable, success]) as spawn, \
             patch("agent_bridge.backends.asyncio.sleep"):
            answer = await AntigravityCliBackend(Path.cwd()).run(
                "hello", model="gemini-3.8-flash-low",
            )
        self.assertEqual(answer.text, "OK")
        self.assertEqual(answer.details, {"preflight_retries": 1})
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(
            [call.args[call.args.index("--model") + 1] for call in spawn.call_args_list],
            ["gemini-3.8-flash-low", "gemini-3.8-flash-low"],
        )

    async def test_antigravity_eligibility_503_retry_is_bounded_and_actionable(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", (b"error: Eligibility check failed: UNAVAILABLE (code 503): "
                             b"The service is currently unavailable.\n")

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   return_value=Process()) as spawn, \
             patch("agent_bridge.backends.asyncio.sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempt") as captured:
                await AntigravityCliBackend(Path.cwd()).run(
                    "hello", model="gemini-3.8-flash-low",
                )
        self.assertIn("gemini-3.8-flash-low", str(captured.exception))
        self.assertIn("upstream", str(captured.exception).lower())
        self.assertEqual(spawn.call_count, 3)

    async def test_antigravity_does_not_retry_503_after_turn_start(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", b"error: model request failed: UNAVAILABLE (code 503)"

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   return_value=Process()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "model request failed"):
                await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(spawn.call_count, 1)

    async def test_antigravity_preflight_retry_is_bounded(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", (b"Eligibility check failed: failed to get profile picture: "
                             b"net/http: TLS handshake timeout")

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   return_value=Process()) as spawn, \
             patch("agent_bridge.backends.asyncio.sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempt"):
                await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(spawn.call_count, 3)

    async def test_antigravity_does_not_retry_other_cli_failure(self):
        class Process:
            returncode = 1

            async def communicate(self):
                return b"", b"error: model request failed: TLS handshake timeout"

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec",
                   return_value=Process()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "model request failed"):
                await AntigravityCliBackend(Path.cwd()).run("hello")
        self.assertEqual(spawn.call_count, 1)

    async def test_sdk_requires_worker_and_rejects_effort(self):
        backend = AntigravitySdkBackend(Path.cwd(), Path.cwd() / "nonexistent-python")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            await backend.run("hello")
        with self.assertRaises(NotImplementedError):
            await backend.open_session()

    async def test_sdk_worker_success_and_soft_failure(self):
        class Process:
            def __init__(self, output):
                self.output = output
                self.returncode = 0

            async def communicate(self, payload):
                self.payload = json.loads(payload)
                return self.output, b""

        backend = AntigravitySdkBackend(Path.cwd(), Path(sys.executable))
        success = Process(b'noise\nAGENT_BRIDGE_RESULT={"ok": true, "text": "reply"}\n')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=success):
            self.assertEqual(await backend.run("hello", "model"), "reply")
        self.assertEqual(success.payload["model"], "model")

        failure = Process(b'AGENT_BRIDGE_RESULT={"ok": false, "error": "denied"}\n')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                await backend.run("hello")

    async def test_sdk_effort_rejected_without_starting_worker(self):
        backend = AntigravitySdkBackend(Path.cwd(), Path(sys.executable))
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(RuntimeError, "does not expose reasoning"):
                await backend.run("hello", reasoning_effort="high")
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
