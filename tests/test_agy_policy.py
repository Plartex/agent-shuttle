import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_bridge.agy_policy import ScopedAgyPolicy, evaluate_tool_call


class ScopedAgyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.file = self.root / "example.py"
        self.file.write_text("answer = 42", encoding="utf-8")

    def decision(self, policy, tool, **args):
        return evaluate_tool_call(policy, self.root, {"name": tool, "args": args})["decision"]

    def test_read_only_allows_native_reads_inside_project(self):
        for tool, key, path in (
            ("view_file", "AbsolutePath", self.file),
            ("list_dir", "DirectoryPath", self.root),
            ("find_by_name", "SearchDirectory", self.root),
            ("grep_search", "SearchPath", self.root),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(self.decision("read_only", tool, **{key: str(path)}), "allow")

    def test_workspace_alias_resolves_to_same_project(self):
        alias = self.root.parent / "project-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable")
        self.assertEqual(evaluate_tool_call("read_only", alias, {
            "name": "view_file", "args": {"AbsolutePath": str(alias / "example.py")},
        })["decision"], "allow")
        self.assertEqual(evaluate_tool_call("workspace_write", alias, {
            "name": "write_to_file", "args": {"TargetFile": str(alias / "example.py")},
        })["decision"], "allow")

    def test_default_deny_covers_writes_commands_mcp_and_subagents(self):
        for policy in ("no_tools", "read_only"):
            for tool in ("write_to_file", "replace_file_content", "run_command",
                         "mcp_agent_bridge_ask_codex", "invoke_subagent", "ask_permission", "unknown"):
                with self.subTest(policy=policy, tool=tool):
                    self.assertEqual(self.decision(policy, tool, TargetFile=str(self.file)), "deny")
        self.assertEqual(self.decision("no_tools", "view_file", AbsolutePath=str(self.file)), "deny")

    def test_rejects_external_relative_and_malformed_read_paths(self):
        for path in (str(self.root.parent / "secret"), str(self.root / ".." / "secret"),
                     "example.py", "", None):
            with self.subTest(path=path):
                self.assertEqual(self.decision("read_only", "view_file", AbsolutePath=path), "deny")
        self.assertEqual(evaluate_tool_call("read_only", self.root, {})["decision"], "deny")

    def test_workspace_write_only_allows_native_project_file_changes(self):
        for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            self.assertEqual(self.decision("workspace_write", tool, TargetFile=str(self.file)), "allow")
        for path in (self.root.parent / "outside.py", self.root / ".git" / "config",
                     self.root / ".agents" / "hooks.json", self.root / ".codex" / "config.toml",
                     self.root / ".agent-shuttle" / "tasks-antigravity.sqlite3"):
            self.assertEqual(self.decision("workspace_write", "write_to_file", TargetFile=str(path)), "deny")
        self.assertEqual(self.decision("workspace_write", "run_command", Cwd=str(self.root)), "deny")

    def test_grants_are_limited_to_the_exact_allowed_file(self):
        result = evaluate_tool_call("workspace_write", self.root, {
            "name": "write_to_file", "args": {"TargetFile": str(self.file)},
        })
        self.assertEqual(result["permissionOverrides"], [f"write_file({self.file.resolve()})"])

    def test_symlink_and_hardlink_cannot_escape_write_boundary(self):
        outside = self.root.parent / "outside.py"
        outside.write_text("secret", encoding="utf-8")
        linked = self.root / "hardlink.py"
        os.link(outside, linked)
        self.assertEqual(self.decision("workspace_write", "replace_file_content", TargetFile=str(linked)), "deny")
        symlink = self.root / "linked.py"
        try:
            symlink.symlink_to(outside)
        except OSError:
            return
        self.assertEqual(self.decision("read_only", "view_file", AbsolutePath=str(symlink)), "deny")

    def test_hook_returns_deny_for_invalid_input_and_unknown_policy(self):
        with ScopedAgyPolicy("read_only", self.root) as scoped:
            for payload in ("not-json", json.dumps({"toolCall": {"name": "run_command", "args": {}}})):
                result = subprocess.run(scoped.hook_argv, input=payload, text=True,
                                        capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout)["decision"], "deny")
        self.assertFalse(scoped.control.exists())
        self.assertEqual(evaluate_tool_call("unknown", self.root, {})["decision"], "deny")

    def test_probe_must_prove_a_denied_write_before_task_dispatch(self):
        with ScopedAgyPolicy("read_only", self.root) as scoped:
            with self.assertRaisesRegex(RuntimeError, "policy probe"):
                scoped.verify_probe()
            payload = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": str(scoped.canary)}}}
            result = subprocess.run(scoped.hook_argv, input=json.dumps(payload), text=True,
                                    capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)["decision"], "deny")
            scoped.verify_probe()
            scoped.canary.write_text("escaped", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "policy probe"):
                scoped.verify_probe()


if __name__ == "__main__":
    unittest.main()
