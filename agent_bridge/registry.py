"""Runtime registry: one configured profile becomes one A2A agent."""

from __future__ import annotations

from .claude_runtime import ClaudeCodeRuntime
from .opencode_runtime import OpenCodeRuntime
from .profiled import ProfiledBackend, ProfiledInfo
from .profiles import AgentProfile


_RUNTIMES = {
    "opencode": OpenCodeRuntime,
    "claude_code": ClaudeCodeRuntime,
}


def build_profile(profile: AgentProfile) -> tuple[ProfiledBackend, ProfiledInfo]:
    runtime = _RUNTIMES[profile.runtime](profile)
    return ProfiledBackend(profile, runtime), ProfiledInfo(profile, runtime)
