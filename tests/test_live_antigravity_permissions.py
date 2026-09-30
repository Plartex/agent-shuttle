"""Opt-in end-to-end test of Antigravity's explicit full-permission mode.

This invokes a real signed-in model with unrestricted tool approval. It is never
part of the default offline test run; enable it deliberately with
BRIDGE_LIVE_AGY_FULL_ACCESS=1.
"""

import asyncio
import json
import os
import socket
import unittest
import uuid
from pathlib import Path
from time import monotonic

from agent_bridge import BridgeClient, HarnessLaunch, connect_harness, discover_harnesses
from agent_bridge.backends import AntigravityCliBackend


@unittest.skipUnless(
    os.environ.get("BRIDGE_LIVE_AGY_FULL_ACCESS") == "1",
    "requires explicit BRIDGE_LIVE_AGY_FULL_ACCESS=1 opt-in",
)
class LiveAntigravityPermissionsTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        os.environ.get("BRIDGE_LIVE_AGY_STREAM_FULL_ACCESS") == "1",
        "requires separate BRIDGE_LIVE_AGY_STREAM_FULL_ACCESS=1 opt-in",
    )
    async def test_full_permissions_execute_command_in_stream_session(self):
        command = os.environ.get("BRIDGE_LIVE_AGY_COMMAND") or discover_harnesses().get("antigravity")
        if not command:
            self.fail("agy executable not found; set BRIDGE_LIVE_AGY_COMMAND")

        root = Path(__file__).resolve().parents[1] / ".runtime" / (
            "agy-full-access-stream-" + uuid.uuid4().hex[:12]
        )
        root.mkdir(parents=True)
        nonce = "AGY_FULL_ACCESS_" + uuid.uuid4().hex
        marker = root / "probe.txt"
        backend = AntigravityCliBackend(root, command, dangerously_skip_permissions=True)
        prompt = (
            "In this empty workspace, call RunCommand to execute exactly this "
            f"PowerShell command: Set-Content -LiteralPath probe.txt -Value {nonce} -NoNewline. "
            "Do not use a file-writing tool. Then reply OK."
        )
        async with asyncio.timeout(180):
            session = await backend.open_session()
            try:
                result = await session.ask(prompt)
            finally:
                await session.close()
        trace = {
            "workspace": str(root), "response": result.text,
            "usage": result.usage, "marker_exists": marker.is_file(),
        }
        print("ANTIGRAVITY_FULL_ACCESS_STREAM_TRACE=" + json.dumps(trace, ensure_ascii=False))
        self.assertEqual(marker.read_text(encoding="utf-8"), nonce)

    async def test_full_permissions_execute_command_direct_backend(self):
        command = os.environ.get("BRIDGE_LIVE_AGY_COMMAND") or discover_harnesses().get("antigravity")
        if not command:
            self.fail("agy executable not found; set BRIDGE_LIVE_AGY_COMMAND")

        root = Path(__file__).resolve().parents[1] / ".runtime" / (
            "agy-full-access-backend-" + uuid.uuid4().hex[:12]
        )
        root.mkdir(parents=True)
        nonce = "AGY_FULL_ACCESS_" + uuid.uuid4().hex
        marker = root / "probe.txt"
        backend = AntigravityCliBackend(
            root, command, dangerously_skip_permissions=True,
        )
        prompt = (
            "In this empty workspace, call RunCommand to execute exactly this "
            f"PowerShell command: Set-Content -LiteralPath probe.txt -Value {nonce} -NoNewline. "
            "Do not use a file-writing tool. Then reply OK."
        )
        result = await asyncio.wait_for(backend.run(prompt), timeout=120)
        trace = {
            "workspace": str(root), "response": result.text,
            "usage": result.usage, "marker_exists": marker.is_file(),
        }
        print("ANTIGRAVITY_FULL_ACCESS_BACKEND_TRACE=" + json.dumps(trace, ensure_ascii=False))
        self.assertEqual(marker.read_text(encoding="utf-8"), nonce)

    @unittest.skipUnless(
        os.environ.get("BRIDGE_LIVE_AGY_A2A_FULL_ACCESS") == "1",
        "requires separate BRIDGE_LIVE_AGY_A2A_FULL_ACCESS=1 opt-in",
    )
    async def test_full_permissions_execute_command_through_bridge(self):
        command = os.environ.get("BRIDGE_LIVE_AGY_COMMAND") or discover_harnesses().get("antigravity")
        if not command:
            self.fail("agy executable not found; set BRIDGE_LIVE_AGY_COMMAND")

        root = Path(__file__).resolve().parents[1] / ".runtime" / (
            "agy-full-access-" + uuid.uuid4().hex[:12]
        )
        root.mkdir(parents=True)
        nonce = "AGY_FULL_ACCESS_" + uuid.uuid4().hex
        marker = root / "probe.txt"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        launch = HarnessLaunch(
            "antigravity", url, root, command=command,
            log_path=root / "bridge.log", agy_dangerously_skip_permissions=True,
        )
        client = BridgeClient(timeout_seconds=90)
        prompt = (
            "In this empty workspace, call RunCommand to execute exactly this "
            f"PowerShell command: Set-Content -LiteralPath probe.txt -Value {nonce} -NoNewline. "
            "Do not use a file-writing tool. Then reply OK."
        )
        started = monotonic()
        async with asyncio.timeout(180):
            async with connect_harness(launch, client=client):
                startup_seconds = round(monotonic() - started, 3)
                identity = await client.identity(url)
                self.assertEqual(identity["agy_permission_mode"], "all")
                self.assertEqual(Path(identity["workspace"]).resolve(), root.resolve())
                result = await client.ask(url, prompt)

        trace = {
            "workspace": str(root), "bridge_log": str(root / "bridge.log"),
            "startup_seconds": startup_seconds,
            "mode": identity["agy_permission_mode"],
            "state": result.state, "response": result.text, "usage": result.usage,
            "marker_exists": marker.is_file(),
        }
        print("ANTIGRAVITY_FULL_ACCESS_TRACE=" + json.dumps(trace, ensure_ascii=False))
        self.assertEqual(result.state, "TASK_STATE_COMPLETED", result.text)
        self.assertEqual(marker.read_text(encoding="utf-8"), nonce)
