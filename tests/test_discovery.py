import unittest
import os
from pathlib import Path
from unittest.mock import patch

from agent_bridge import discover_harnesses
from agent_bridge import discovery


class DiscoveryTests(unittest.TestCase):
    def test_path_and_manual_commands(self):
        def which(command):
            return {"agy": "/bin/agy", "custom-open": "/bin/custom-open"}.get(command)

        with patch("agent_bridge.discovery.shutil.which", side_effect=which), \
             patch("agent_bridge.discovery.importlib.util.find_spec", return_value=object()):
            found = discover_harnesses({"opencode": "custom-open"})
        self.assertEqual(found["codex"], "agent-shuttle")
        self.assertEqual(found["antigravity"], "/bin/agy")
        self.assertEqual(found["opencode"], "/bin/custom-open")

    def test_unknown_harness_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown harnesses"):
            discover_harnesses({"unknown": "anything"})

    def test_opencode_can_be_found_outside_path(self):
        with patch("agent_bridge.discovery.shutil.which", return_value=None), \
             patch("agent_bridge.discovery.Path.is_file", autospec=True,
                   side_effect=lambda path: str(path).endswith("opencode.exe")), \
             patch("agent_bridge.discovery.importlib.util.find_spec", return_value=None):
            found = discover_harnesses()
        self.assertTrue(found["opencode"].endswith("opencode.exe"))

    def test_inaccessible_install_directory_does_not_abort_discovery(self):
        with patch("agent_bridge.discovery.shutil.which", return_value=None), \
             patch("agent_bridge.discovery.Path.is_file", side_effect=PermissionError("denied")), \
             patch("agent_bridge.discovery.importlib.util.find_spec", return_value=object()):
            self.assertEqual(discover_harnesses(), {"codex": "agent-shuttle"})

    def test_antigravity_is_found_in_source_checkout_bin(self):
        bundled = Path(discovery.__file__).resolve().parent.parent / "bin" / "agy.exe"
        with patch.dict(os.environ, {"BRIDGE_AGY_COMMAND": ""}), \
             patch("agent_bridge.discovery.shutil.which", return_value=None), \
             patch("agent_bridge.discovery._is_file", side_effect=lambda path: path == bundled), \
             patch("agent_bridge.discovery.importlib.util.find_spec", return_value=None):
            found = discover_harnesses()
        self.assertEqual(found["antigravity"], str(bundled))

    def test_antigravity_respects_command_environment_override(self):
        command = "C:/manual/agy.exe"
        with patch.dict(os.environ, {"BRIDGE_AGY_COMMAND": command}), \
             patch("agent_bridge.discovery.shutil.which", return_value=None), \
             patch("agent_bridge.discovery._is_file", side_effect=lambda path: path == Path(command)), \
             patch("agent_bridge.discovery.importlib.util.find_spec", return_value=None):
            found = discover_harnesses()
        self.assertEqual(Path(found["antigravity"]), Path(command))
