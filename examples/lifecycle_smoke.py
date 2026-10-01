"""Run one real Codex task through Agent Shuttle's A2A task lifecycle."""

import asyncio
import socket
from pathlib import Path

from agent_shuttle import HarnessLaunch, ShuttleClient, connect_harness


def free_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


async def main():
    root = Path(__file__).resolve().parents[1]
    launch = HarnessLaunch("codex", free_url(), root, tool_policy="read_only")
    async with connect_harness(launch) as peer:
        client = ShuttleClient()
        handle = await client.submit(
            peer.url,
            "Read pyproject.toml. Report only the project name, version, and "
            "minimum Python version. Do not change files.",
            tool_policy="read_only",
        )
        print("started:", peer.started, "task_id:", handle.task_id, flush=True)
        initial = await handle.status()
        print("initial state:", initial.state, flush=True)
        async def collect_events():
            seen = []
            async for event in handle.events():
                seen.append((event.kind, event.state))
                if event.state in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                                   "TASK_STATE_CANCELED"}:
                    return seen
            return seen

        events = await asyncio.wait_for(collect_events(), timeout=120)
        print("events:", events, flush=True)
        result = await handle.wait(timeout=120)
        print("final state:", result.state, flush=True)
        print("answer:", result.text, flush=True)
        print("reopened state:", (await client.task(peer.url, handle.task_id).status()).state,
              flush=True)
        if result.state != "TASK_STATE_COMPLETED":
            raise RuntimeError(f"Live task ended in {result.state}")

        cancellable = await client.submit(
            peer.url,
            "Inspect all source files and write a detailed architecture review. "
            "Do not change files.",
            tool_policy="read_only",
        )
        print("cancel task id:", cancellable.task_id, flush=True)
        canceled = await cancellable.cancel()
        print("cancel state:", canceled.state, flush=True)
        if canceled.state != "TASK_STATE_CANCELED":
            raise RuntimeError(f"Cancellation ended in {canceled.state}")


if __name__ == "__main__":
    asyncio.run(main())
