"""Call Agent Shuttle over stdio MCP with no prestarted A2A server."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key not in {"BRIDGE_CODEX_URL", "BRIDGE_ANTIGRAVITY_URL", "BRIDGE_AGENTS_JSON"}}
    env["BRIDGE_WORKSPACE"] = str(root)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_shuttle.mcp_server"], env=env,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            arguments = {
                "prompt": "Read pyproject.toml and reply with only the project name and version. Do not edit files.",
            }
            if len(sys.argv) > 1:
                arguments["model"] = sys.argv[1]
            response = await session.call_tool("ask_codex", arguments)
            print("MCP error:", response.isError, flush=True)
            print("MCP result:", response.content, flush=True)
            if response.isError:
                raise RuntimeError("MCP task failed")
            result = json.loads(response.content[0].text)
            if result["state"] != "TASK_STATE_COMPLETED":
                raise RuntimeError(f"MCP task ended in {result['state']}")


if __name__ == "__main__":
    asyncio.run(main())
