"""Per-conversation Antigravity tool gate, independent of caller configuration.

The hook entry point uses only the standard library and runs with -I -S.
This gates agent tools; it is not an operating-system process sandbox.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import tempfile
import uuid
from pathlib import Path


SCOPED_POLICIES = {"no_tools", "read_only", "workspace_write"}
_READ_PATHS = {
    "view_file": "AbsolutePath", "list_dir": "DirectoryPath",
    "find_by_name": "SearchDirectory", "grep_search": "SearchPath",
}
_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
_CONTROL_DIRECTORIES = {".git", ".agents", ".codex", ".claude", ".gemini", ".agent-shuttle"}


def evaluate_tool_call(policy: str, workspace: Path, call: object) -> dict:
    denied = {"decision": "deny", "reason": "Tool is outside the Agent Shuttle task policy."}
    if policy not in SCOPED_POLICIES or policy == "no_tools" or not isinstance(call, dict):
        return denied
    tool = call.get("name")
    args = call.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return denied
    key = _READ_PATHS.get(tool)
    writing = tool in _WRITE_TOOLS and policy == "workspace_write"
    if writing:
        key = "TargetFile"
    if key is None:
        return denied
    raw = args.get(key)
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        return denied
    try:
        # Keep the caller's path spelling for lexical containment. Windows
        # runner temp directories may resolve through a drive junction, so
        # comparing an unresolved child against a resolved root denies valid
        # files. Resolve both paths separately for the actual containment.
        lexical_root = Path(os.path.abspath(workspace))
        lexical = Path(raw).relative_to(lexical_root)
        root = workspace.resolve(strict=True)
        target = Path(raw).resolve()
        relative = target.relative_to(root)
        if writing and (
            any(part.casefold() in _CONTROL_DIRECTORIES for part in (*lexical.parts, *relative.parts))
            or target.exists() and (not target.is_file() or target.stat().st_nlink > 1)
        ):
            return denied
    except (OSError, RuntimeError, ValueError):
        return denied
    resource = "write_file" if writing else "read_file"
    return {"decision": "allow", "reason": "Allowed by the Agent Shuttle task policy.",
            "permissionOverrides": [f"{resource}({target})"]}


def _hook_command(argv: list[str]) -> str:
    if os.name != "nt":
        return shlex.join(argv)
    # EncodedCommand avoids nested Windows-shell quoting and Unicode-path errors.
    quoted = " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    expression = "& " + quoted + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(expression.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded


class ScopedAgyPolicy:
    def __init__(self, policy: str, workspace: Path):
        if policy not in SCOPED_POLICIES:
            raise ValueError(f"Unknown scoped Antigravity policy {policy!r}")
        self.policy = policy
        self.workspace = workspace.resolve(strict=True)
        self.temporary: tempfile.TemporaryDirectory | None = None

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-shuttle-agy-")
        self.control = Path(self.temporary.name).resolve()
        self.canary = self.control / "policy-canary.txt"
        self.audit = self.control / "decisions.jsonl"
        config = self.control / "policy.json"
        self.config = config
        config.write_text(json.dumps({"policy": "no_tools", "workspace": str(self.workspace)}),
                          encoding="utf-8")
        self.hook_argv = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), str(config)]
        hooks = {"agent-shuttle-" + uuid.uuid4().hex: {"PreToolUse": [{
            "matcher": "*", "hooks": [{"type": "command",
                                         "command": _hook_command(self.hook_argv), "timeout": 10}],
        }]}}
        (self.control / ".agents").mkdir()
        (self.control / ".agents" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        return self

    def __exit__(self, *args):
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def decisions(self) -> list[dict]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def verify_probe(self) -> None:
        expected = os.path.normcase(str(self.canary))
        denied = any(
            item.get("tool") == "write_to_file" and item.get("decision") == "deny"
            and os.path.normcase(item.get("target", "")) == expected
            for item in self.decisions()
        )
        if self.canary.exists() or not denied:
            raise RuntimeError(
                "Antigravity policy probe failed; the user task was not dispatched. "
                "The CLI must load and execute Agent Shuttle's PreToolUse hook."
            )
        updated = self.control / "policy.next.json"
        updated.write_text(json.dumps({"policy": self.policy, "workspace": str(self.workspace)}),
                           encoding="utf-8")
        updated.replace(self.config)

    def probe_prompt(self) -> str:
        return (
            "Agent Shuttle is verifying tool permissions before sending the user's task. "
            "Call write_to_file exactly once to create this disposable canary file: "
            f"{self.canary}. Use Overwrite=true, CodeContent=POLICY_PROBE, "
            "Description=Negative permission check. The hook should deny this write. "
            "Attempt the native write tool even if you expect denial; this is the test. "
            "Do not use commands, MCP, or other tools. After the denial reply READY."
        )

    def task_prompt(self, prompt: str) -> str:
        return (
            f"The task project is {self.workspace}. Use absolute paths in that project. "
            f"The enforced tool policy is {self.policy}. "
            "Only native file reading/search is allowed for read_only; workspace_write "
            "also allows native file creation/replacement inside the project. "
            "Shell, MCP, subagents, permission changes, and external paths are blocked. "
            "For no_tools, respond using only the supplied context.\n\n" + prompt
        )


def main() -> None:
    result = {"decision": "deny", "reason": "Agent Shuttle could not validate the tool call."}
    try:
        config_path = Path(sys.argv[1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        payload = json.loads(sys.stdin.buffer.read())
        call = payload.get("toolCall", {})
        result = evaluate_tool_call(config["policy"], Path(config["workspace"]), call)
        args = call.get("args", {}) if isinstance(call, dict) else {}
        target = args.get("TargetFile", "") if isinstance(args, dict) else ""
        record = {"tool": call.get("name") if isinstance(call, dict) else None,
                  "decision": result["decision"], "target": target if isinstance(target, str) else ""}
        with (config_path.parent / "decisions.jsonl").open("a", encoding="utf-8") as audit:
            audit.write(json.dumps(record) + "\n")
    except Exception:
        result = {"decision": "deny", "reason": "Agent Shuttle could not validate the tool call."}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
