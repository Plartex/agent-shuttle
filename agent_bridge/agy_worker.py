"""Optional Antigravity SDK worker with an isolated Protobuf runtime."""

from __future__ import annotations

import asyncio
import json
import sys


async def _run(prompt: str, workspace: str, model: str | None = None) -> str:
    from google.antigravity import Agent, LocalAgentConfig

    config = LocalAgentConfig(workspaces=[workspace], model=model)
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        return await response.text()


def main() -> None:
    try:
        request = json.load(sys.stdin)
        text = asyncio.run(_run(request["prompt"], request["workspace"], request.get("model")))
        response = {"ok": True, "text": text}
    except Exception as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print("AGENT_BRIDGE_RESULT=" + json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
