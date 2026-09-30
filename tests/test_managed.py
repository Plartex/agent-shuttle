"""Lifecycle contracts for the reusable Bridge server manager."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agent_bridge import HarnessLaunch, connect_harness


class ManagedHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        taskkill = patch("agent_bridge.managed.subprocess.run",
                         return_value=SimpleNamespace(returncode=0))
        self.mock_taskkill = taskkill.start()
        self.addCleanup(taskkill.stop)

    async def test_full_access_policy_alone_configures_antigravity_harness(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                command="C:/tools/agy.exe", tool_policy="full_access",
            )
            client = SimpleNamespace(identity=AsyncMock(side_effect=[
                OSError("offline"), {
                    "backend": "agy_cli", "workspace": str(root.resolve()),
                    "agy_permission_mode": "all", "agy_turn_timeout_seconds": 300,
                },
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    self.assertIn("--agy-dangerously-skip-permissions", popen.call_args.args[0])
                    self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    async def test_temp_ollama_profile_can_opt_into_full_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "opencode", "http://127.0.0.1:8767", root, model="qwen3.5:9b",
                command="C:/tools/opencode.exe", tool_policy="full_access",
                log_path=root / "bridge.log",
            )
            client = SimpleNamespace(identity=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "opencode", "workspace": str(root.resolve()),
                                   "read_only_tools": True},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    argv = popen.call_args.args[0]
                    profile = json.loads(Path(argv[argv.index("--profile") + 1]).read_text())
                    self.assertEqual(profile["max_tool_policy"], "full_access")

    async def test_antigravity_full_access_project_launch_sets_cli_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                command="C:/tools/agy.exe", tool_policy="full_access",
                agy_dangerously_skip_permissions=True,
            )
            client = SimpleNamespace(identity=AsyncMock(side_effect=[
                OSError("offline"), {
                    "backend": "agy_cli", "workspace": str(root.resolve()),
                    "agy_permission_mode": "all", "agy_turn_timeout_seconds": 300,
                },
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    self.assertIn("--agy-dangerously-skip-permissions", popen.call_args.args[0])

    async def test_reuse_checks_static_identity_without_fetching_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch("antigravity", "http://127.0.0.1:8766", root)
            client = SimpleNamespace(
                identity=AsyncMock(return_value={
                    "backend": "agy_cli", "workspace": str(root.resolve()),
                    "read_only_tools": False, "agy_permission_mode": "settings",
                    "agy_turn_timeout_seconds": 300,
                }),
                capabilities=AsyncMock(side_effect=AssertionError("model lookup is not readiness")),
            )
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                async with connect_harness(launch, client=client) as connection:
                    self.assertFalse(connection.started)
            client.identity.assert_awaited_once_with(launch.url)
            client.capabilities.assert_not_called()
            popen.assert_not_called()

    async def test_reuses_matching_server_without_starting_process(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder))
            client = SimpleNamespace(capabilities=AsyncMock(return_value={
                "backend": "codex_app_server", "workspace": str(Path(folder).resolve()),
                "read_only_tools": True,
            }))
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                async with connect_harness(launch, client=client) as connection:
                    self.assertFalse(connection.started)
                    self.assertEqual(connection.url, launch.url)
            popen.assert_not_called()

    async def test_starts_profile_server_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "opencode", "http://127.0.0.1:8767", root, model="qwen3.5:9b",
                command="C:/tools/opencode.exe", log_path=root / "bridge.log",
            )
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "opencode", "workspace": str(root.resolve()),
                                    "read_only_tools": False},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client) as connection:
                    self.assertTrue(connection.started)
                    self.assertEqual(connection.log_path, launch.log_path)
                    argv = popen.call_args.args[0]
                    profile = json.loads(Path(argv[argv.index("--profile") + 1]).read_text())
                    self.assertEqual(profile["runtime_command"], launch.command)
                    self.assertEqual(profile["default_model"], launch.model)
            process.terminate.assert_called_once()
            process.wait.assert_called_once()
            if os.name == "nt":
                self.mock_taskkill.assert_called_with(
                    ["taskkill", "/PID", "123", "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                )

    async def test_explicit_server_must_be_available(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder),
                                   start_if_missing=False)
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=OSError("offline")))
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    async with connect_harness(launch, client=client):
                        pass
            popen.assert_not_called()

    async def test_existing_profile_is_used_without_generating_ollama_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "cloud-profile.json"
            profile.write_text('{"id":"custom"}', encoding="utf-8")
            launch = HarnessLaunch("claude_code", "http://127.0.0.1:8768", root,
                                   profile_path=profile, log_path=root / "bridge.log")
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "claude_code", "workspace": str(root.resolve()),
                                    "read_only_tools": False},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    argv = popen.call_args.args[0]
                    self.assertEqual(Path(argv[argv.index("--profile") + 1]), profile)
            process.terminate.assert_called_once()

    async def test_started_server_stops_after_caller_error(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder),
                                   command="agent-bridge")
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "codex_app_server", "workspace": str(Path(folder).resolve()),
                                    "read_only_tools": True},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaisesRegex(RuntimeError, "caller failed"):
                    async with connect_harness(launch, client=client):
                        raise RuntimeError("caller failed")
            popen.assert_called_once()
            process.terminate.assert_called_once()

    async def test_wrong_backend_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder))
            client = SimpleNamespace(capabilities=AsyncMock(return_value={"backend": "opencode"}))
            with self.assertRaisesRegex(ValueError, "opencode"):
                async with connect_harness(launch, client=client):
                    pass

    async def test_wrong_workspace_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder))
            client = SimpleNamespace(capabilities=AsyncMock(return_value={
                "backend": "codex_app_server", "workspace": str(Path(other).resolve()),
                "read_only_tools": True,
            }))
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                with self.assertRaisesRegex(ValueError, "workspace"):
                    async with connect_harness(launch, client=client):
                        pass
            popen.assert_not_called()

    async def test_missing_workspace_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder))
            client = SimpleNamespace(capabilities=AsyncMock(return_value={"backend": "codex_app_server"}))
            with self.assertRaisesRegex(ValueError, "workspace"):
                async with connect_harness(launch, client=client):
                    pass

    async def test_temp_profile_can_enable_read_only_without_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "opencode", "http://127.0.0.1:8767", root, model="qwen3.5:9b",
                command="C:/tools/opencode.exe", tool_policy="read_only", log_path=root / "bridge.log",
            )
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "opencode", "workspace": str(root.resolve()),
                                    "read_only_tools": True},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    argv = popen.call_args.args[0]
                    profile = json.loads(Path(argv[argv.index("--profile") + 1]).read_text())
                    self.assertEqual(profile["max_tool_policy"], "read_only")
                    self.assertNotEqual(profile["max_tool_policy"], "workspace_write")

    async def test_existing_no_tools_profile_cannot_be_elevated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "profile.json"
            profile.write_text(json.dumps({
                "id": "local", "runtime": "opencode", "provider": "ollama",
                "workspace": str(root), "default_model": "test", "allowed_models": ["test"],
                "max_tool_policy": "no_tools",
            }), encoding="utf-8")
            launch = HarnessLaunch(
                "opencode", "http://127.0.0.1:8767", root,
                profile_path=profile, tool_policy="read_only",
            )
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=OSError("offline")))
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                with self.assertRaisesRegex(ValueError, "exceeds profile maximum"):
                    async with connect_harness(launch, client=client):
                        pass
            popen.assert_not_called()

    async def test_nonlocal_launch_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = HarnessLaunch("codex", "https://example.org:8765", Path(folder))
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=OSError("offline")))
            with self.assertRaisesRegex(ValueError, "loopback"):
                async with connect_harness(launch, client=client):
                    pass

    async def test_antigravity_full_permissions_passed_only_when_requested(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                command="C:/tools/agy.exe", log_path=root / "bridge.log",
                agy_dangerously_skip_permissions=True,
            )
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "agy_cli", "workspace": str(root.resolve()),
                                    "read_only_tools": False, "agy_permission_mode": "all",
                                    "agy_turn_timeout_seconds": 300},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    self.assertIn("--agy-dangerously-skip-permissions", popen.call_args.args[0])

    async def test_antigravity_default_starts_with_settings_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                command="C:/tools/agy.exe", log_path=root / "bridge.log",
            )
            client = SimpleNamespace(capabilities=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "agy_cli", "workspace": str(root.resolve()),
                                    "read_only_tools": False, "agy_permission_mode": "settings",
                                    "agy_turn_timeout_seconds": 300},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    self.assertNotIn("--agy-dangerously-skip-permissions", popen.call_args.args[0])

    async def test_antigravity_turn_timeout_passes_to_temporary_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                command="C:/tools/agy.exe", log_path=root / "bridge.log",
                agy_turn_timeout_seconds=42,
            )
            client = SimpleNamespace(identity=AsyncMock(side_effect=[
                OSError("offline"), {"backend": "agy_cli", "workspace": str(root.resolve()),
                                    "agy_permission_mode": "settings",
                                    "agy_turn_timeout_seconds": 42},
            ]))
            process = MagicMock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("agent_bridge.managed.subprocess.Popen", return_value=process) as popen, \
                 patch("agent_bridge.managed.asyncio.sleep", new_callable=AsyncMock):
                async with connect_harness(launch, client=client):
                    argv = popen.call_args.args[0]
                    self.assertEqual(argv[argv.index("--agy-turn-timeout-seconds") + 1], "42")

    async def test_antigravity_permission_mode_mismatch_prevents_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch = HarnessLaunch(
                "antigravity", "http://127.0.0.1:8766", root,
                agy_dangerously_skip_permissions=False,
            )
            client = SimpleNamespace(capabilities=AsyncMock(return_value={
                "backend": "agy_cli", "workspace": str(root.resolve()),
                "agy_permission_mode": "all",
            }))
            with patch("agent_bridge.managed.subprocess.Popen") as popen:
                with self.assertRaisesRegex(ValueError, "permission mode"):
                    async with connect_harness(launch, client=client):
                        pass
            popen.assert_not_called()

    async def test_antigravity_all_permissions_rejects_nonboolean_and_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = SimpleNamespace(capabilities=AsyncMock())
            for launch, message in (
                (HarnessLaunch("antigravity", "http://127.0.0.1:8766", root,
                               agy_dangerously_skip_permissions="false"), "boolean"),
                (HarnessLaunch("antigravity", "http://127.0.0.1:8766", root,
                               tool_policy="read_only",
                               agy_dangerously_skip_permissions=True), "read_only"),
                (HarnessLaunch("antigravity", "http://127.0.0.1:8766", root,
                               tool_policy="no_tools"), "no_tools"),
            ):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    async with connect_harness(launch, client=client):
                        pass
            client.capabilities.assert_not_called()
