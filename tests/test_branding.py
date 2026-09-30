"""Public Agent Shuttle names keep the former Agent Bridge API working."""

import tomllib
import runpy
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import agent_bridge


class AgentShuttleBrandingTests(unittest.TestCase):
    def test_module_entry_points_delegate_to_existing_implementation(self):
        import agent_shuttle.cli
        import agent_shuttle.mcp_server

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*found in sys.modules.*", category=RuntimeWarning)
            with patch("agent_bridge.cli.main") as cli_main:
                runpy.run_module("agent_shuttle.cli", run_name="__main__")
            with patch("agent_bridge.mcp_server.main") as mcp_main:
                runpy.run_module("agent_shuttle.mcp_server", run_name="__main__")
        cli_main.assert_called_once_with()
        mcp_main.assert_called_once_with()

        with patch("agent_shuttle.cli.main") as cli_main:
            runpy.run_module("agent_shuttle", run_name="__main__")
        cli_main.assert_called_once_with()

    def test_new_python_namespace_reexports_existing_client(self):
        import agent_shuttle

        self.assertIs(agent_shuttle.ShuttleClient, agent_bridge.BridgeClient)
        self.assertIs(agent_shuttle.BridgeClient, agent_bridge.BridgeClient)
        self.assertIs(agent_shuttle.HarnessLaunch, agent_bridge.HarnessLaunch)
        self.assertIs(agent_shuttle.connect_harness, agent_bridge.connect_harness)

    def test_mcp_server_advertises_new_name(self):
        from agent_bridge.mcp_server import mcp

        self.assertEqual(mcp.name, "Agent Shuttle")

    def test_distribution_and_console_scripts_have_new_and_legacy_names(self):
        config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["name"], "agent-shuttle")
        scripts = config["project"]["scripts"]
        self.assertEqual(scripts["agent-shuttle"], "agent_shuttle.cli:main")
        self.assertEqual(scripts["agent-shuttle-mcp"], "agent_shuttle.mcp_server:main")
        self.assertIn("agent-bridge", scripts)
        self.assertIn("agent-bridge-mcp", scripts)


if __name__ == "__main__":
    unittest.main()
