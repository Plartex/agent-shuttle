"""Local A2A bridge for configured agent runtimes."""

from .backends import AntigravityAuthenticationError, AntigravityPermissionDenied
from .client import BridgeClient, BridgeEvent, BridgeResult, BridgeSession, TaskHandle
from .discovery import discover_harnesses
from .managed import BridgeConnection, HarnessLaunch, connect_harness
from .profiles import AgentProfile, ToolPolicy
from .registry import build_profile

__all__ = [
    "BridgeClient",
    "AntigravityPermissionDenied",
    "AntigravityAuthenticationError",
    "BridgeResult",
    "BridgeEvent",
    "BridgeSession",
    "TaskHandle",
    "discover_harnesses",
    "BridgeConnection",
    "HarnessLaunch",
    "connect_harness",
    "AgentProfile",
    "ToolPolicy",
    "build_profile",
]
