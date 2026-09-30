"""Discover locally installed agent harness executables without starting them."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def discover_harnesses(commands: dict[str, str] | None = None) -> dict[str, str]:
    """Return available harnesses and launch commands.

    Explicit commands override PATH discovery. Codex is provided by the
    installed openai-codex SDK, so its value is the ``agent-shuttle`` backend.
    This function never contacts a model or changes local state.
    """
    overrides = commands or {}
    unknown = set(overrides) - {"codex", "antigravity", "opencode", "claude_code"}
    if unknown:
        raise ValueError(f"Unknown harnesses: {', '.join(sorted(unknown))}")
    result: dict[str, str] = {}
    if importlib.util.find_spec("openai_codex") is not None:
        result["codex"] = "agent-shuttle"
    for name, default in (("antigravity", "agy"), ("opencode", "opencode"),
                          ("claude_code", "claude")):
        environment_command = os.environ.get("BRIDGE_AGY_COMMAND") if name == "antigravity" else None
        command = overrides.get(name) or environment_command or default
        found = shutil.which(command)
        if found is None and _is_file(Path(command)):
            found = str(Path(command).resolve())
        if found is None and name not in overrides and not environment_command:
            home = Path.home()
            candidates = {
                "antigravity": [home / ".local" / "bin" / "agy.exe",
                                Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe",
                                Path(__file__).resolve().parent.parent / "bin" / "agy.exe"],
                "opencode": [Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" /
                             "opencode-ai" / "bin" / "opencode.exe",
                             home / ".local" / "bin" / "opencode.exe"],
                "claude_code": [home / ".local" / "bin" / "claude.exe"],
            }[name]
            found = next((str(path) for path in candidates if _is_file(path)), None)
        if found is not None:
            result[name] = found
    if "codex" in overrides:
        result["codex"] = overrides["codex"]
    return result
