"""Public Agent Shuttle API; legacy ``agent_bridge`` imports remain supported."""

import agent_bridge as _legacy
from agent_bridge import *  # noqa: F401,F403 - intentional compatibility facade
from agent_bridge import BridgeClient as ShuttleClient

__all__ = [*_legacy.__all__, "ShuttleClient"]
