"""Server-owned agent profiles and request policy validation.

An inference provider is configuration of an agent runtime, not another agent.
No caller-supplied endpoint or credential crosses the A2A boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse


class ToolPolicy(str, Enum):
    NO_TOOLS = "no_tools"
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


_POLICY_RANK = {
    ToolPolicy.NO_TOOLS: 0,
    ToolPolicy.READ_ONLY: 1,
    ToolPolicy.WORKSPACE_WRITE: 2,
    ToolPolicy.FULL_ACCESS: 3,
}


@dataclass(frozen=True)
class ProfileSelection:
    model: str
    reasoning_effort: str | None
    tool_policy: ToolPolicy


@dataclass(frozen=True)
class AgentProfile:
    id: str
    runtime: str
    provider: str
    workspace: Path
    endpoint: str | None
    default_model: str
    allowed_models: tuple[str, ...]
    max_tool_policy: ToolPolicy
    default_tool_policy: ToolPolicy = ToolPolicy.NO_TOOLS
    reasoning_efforts: tuple[str, ...] = ()
    runtime_url: str | None = None
    runtime_command: str | None = None
    credential_env: str | None = None
    turn_timeout_seconds: float = 1800

    @classmethod
    def from_file(
        cls, path: Path, *, workspace_override: Path | None = None,
    ) -> "AgentProfile":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Agent profile must be a JSON object")
        if workspace_override is not None:
            data["workspace"] = str(workspace_override)
        elif "workspace" not in data:
            data["workspace"] = str(path.resolve().parent)
        elif not Path(data["workspace"]).is_absolute():
            data["workspace"] = str(path.resolve().parent / data["workspace"])
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: dict) -> "AgentProfile":
        fields = set(cls.__dataclass_fields__)
        unknown = set(data) - fields
        if unknown:
            raise ValueError(f"Unknown agent profile fields: {', '.join(sorted(unknown))}")
        required = {"id", "runtime", "provider", "workspace", "default_model", "allowed_models"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"Missing agent profile fields: {', '.join(sorted(missing))}")
        runtime = data["runtime"]
        if runtime not in {"opencode", "claude_code"}:
            raise ValueError("runtime must be opencode or claude_code")
        provider = data["provider"]
        if not isinstance(provider, str) or not provider or "/" in provider:
            raise ValueError("provider must be a nonempty provider ID")
        try:
            workspace = Path(data["workspace"]).resolve(strict=True)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise ValueError("workspace must be an existing directory") from exc
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        endpoint = data.get("endpoint")
        if provider == "ollama":
            endpoint = endpoint or "http://127.0.0.1:11434"
            parsed = urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Local Ollama endpoint must use HTTP loopback")
        elif endpoint is not None and urlparse(endpoint).scheme != "https":
            raise ValueError("Cloud provider endpoint must use HTTPS")
        allowed = data["allowed_models"]
        if not isinstance(allowed, list) or not allowed or any(
            not isinstance(item, str) or not item.strip() for item in allowed
        ):
            raise ValueError("allowed_models must be a nonempty list of model IDs")
        allowed = tuple(dict.fromkeys(allowed))
        default_model = data["default_model"]
        if default_model not in allowed:
            raise ValueError("default_model must occur in allowed_models")
        try:
            maximum = ToolPolicy(data.get("max_tool_policy", "no_tools"))
            default = ToolPolicy(data.get("default_tool_policy", "no_tools"))
        except ValueError as exc:
            raise ValueError("Unknown tool policy") from exc
        if _POLICY_RANK[default] > _POLICY_RANK[maximum]:
            raise ValueError("default_tool_policy exceeds max_tool_policy")
        efforts = data.get("reasoning_efforts", [])
        if not isinstance(efforts, list) or any(not isinstance(x, str) or not x for x in efforts):
            raise ValueError("reasoning_efforts must be a list of nonempty strings")
        for name in ("id", "runtime_url", "runtime_command", "credential_env"):
            value = data.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty string")
        if data.get("runtime_url"):
            runtime_url = urlparse(data["runtime_url"])
            if runtime_url.scheme != "http" or runtime_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("runtime_url must use local HTTP loopback")
        timeout = data.get("turn_timeout_seconds", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or timeout <= 0:
            raise ValueError("turn_timeout_seconds must be positive")
        return cls(
            id=data["id"], runtime=runtime, provider=provider, workspace=workspace,
            endpoint=endpoint, default_model=default_model, allowed_models=allowed,
            max_tool_policy=maximum, default_tool_policy=default,
            reasoning_efforts=tuple(dict.fromkeys(efforts)),
            runtime_url=data.get("runtime_url"), runtime_command=data.get("runtime_command"),
            credential_env=data.get("credential_env"), turn_timeout_seconds=float(timeout),
        )

    def resolve(
        self,
        model: str | None,
        reasoning_effort: str | None,
        tool_policy: str | ToolPolicy | None,
    ) -> ProfileSelection:
        selected = model or self.default_model
        if self.runtime == "opencode" and selected not in self.allowed_models and "/" in selected:
            provider, selected = selected.split("/", 1)
            if provider != self.provider:
                raise ValueError(f"Model provider {provider!r} does not match profile provider {self.provider!r}")
        if selected not in self.allowed_models:
            raise ValueError(f"Model {selected!r} is not allowed by profile {self.id!r}")
        if reasoning_effort is not None and reasoning_effort not in self.reasoning_efforts:
            raise ValueError(f"Unsupported reasoning effort {reasoning_effort!r} for profile {self.id!r}")
        try:
            policy = ToolPolicy(tool_policy) if tool_policy is not None else self.default_tool_policy
        except ValueError as exc:
            raise ValueError(f"Unknown tool policy {tool_policy!r}") from exc
        if _POLICY_RANK[policy] > _POLICY_RANK[self.max_tool_policy]:
            raise ValueError(f"Tool policy {policy.value!r} exceeds profile maximum")
        effective_model = f"{self.provider}/{selected}" if self.runtime == "opencode" else selected
        return ProfileSelection(effective_model, reasoning_effort, policy)
