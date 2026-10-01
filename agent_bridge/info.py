"""Read live harness capabilities and account quotas without model turns."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
import json
import math
import os
from pathlib import Path
from typing import Protocol

from .backends import _agy_authentication_error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _enum_value(value):
    return getattr(value, "value", value)


def _json_ready(value):
    """Normalize SDK enums nested in typed App Server response dumps."""
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


class InfoProvider(Protocol):
    async def fetch(self, *, capabilities: bool = True, usage: bool = True) -> dict: ...


class AntigravityCliInfo:
    def __init__(self, workspace: Path, command: str = "agy", *, timeout_seconds: float = 45):
        if (not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive finite")
        self.workspace = workspace.resolve(strict=True)
        self.command = command
        self.timeout_seconds = timeout_seconds
        self._read_lock = asyncio.Lock()

    async def _read(self, *args: str) -> dict:
        # Multiple HTTP info requests must not start competing agy processes.
        async with self._read_lock:
            return await self._read_unlocked(*args)

    async def _read_unlocked(self, *args: str) -> dict:
        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            cwd=str(self.workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TimeoutError(
                f"agy info {' '.join(args)} timed out after {self.timeout_seconds:g}s"
            ) from exc
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            authentication_error = _agy_authentication_error(stderr)
            if authentication_error is not None:
                raise authentication_error
            raise RuntimeError(
                f"agy info failed ({process.returncode}): {stderr.decode(errors='replace')[-2000:]}"
            )
        # `agy models` may print a progress line before its JSON envelope.
        try:
            envelope = next(
                json.loads(line)
                for line in reversed(stdout.decode(errors="replace").splitlines())
                if line.startswith("{")
            )
        except (StopIteration, json.JSONDecodeError) as exc:
            raise RuntimeError("agy returned no JSON info envelope") from exc
        if envelope.get("status") != "SUCCESS":
            raise RuntimeError(str(envelope.get("error") or "agy info failed"))
        return envelope.get("command", {}).get("data", {})

    async def fetch(self, *, capabilities: bool = True, usage: bool = True) -> dict:
        calls: list[tuple[str, ...]] = []
        if capabilities:
            calls.extend(
                [
                    ("--output-format", "json", "models"),
                    ("-p", "/model", "--output-format", "json"),
                    ("-p", "/effort", "--output-format", "json"),
                ]
            )
        if usage:
            calls.append(("-p", "/usage", "--output-format", "json"))
        results = iter([await self._read(*args) for args in calls])
        data = {"agent": "antigravity", "backend": "agy_cli", "fetched_at": _utc_now()}
        if capabilities:
            model_list = next(results)
            selected = next(results)
            effort = next(results)
            data["capabilities"] = {
                "selected_model": selected.get("id"),
                "selected_effort": effort.get("current") or selected.get("effort"),
                "models": model_list.get("models", []),
                "effort_options": effort.get("available", []),
                "effort_adjustable": effort.get("adjustable"),
                "effort_options_scope": "Current CLI selection; model-specific support is not reported",
            }
        if usage:
            quota = next(results)
            groups = []
            for group in quota.get("groups", []):
                buckets = []
                for bucket in group.get("buckets", []):
                    fraction = bucket.get("remaining_fraction")
                    buckets.append(
                        {
                            "id": bucket.get("id"),
                            "name": bucket.get("name"),
                            "window": bucket.get("window"),
                            "remaining_percent": round(fraction * 100, 2) if fraction is not None else None,
                            "used_percent": round((1 - fraction) * 100, 2) if fraction is not None else None,
                            "resets_at": bucket.get("reset_time"),
                        }
                    )
                groups.append({"name": group.get("name"), "description": group.get("description"), "buckets": buckets})
            data["usage"] = {"available": True, "source": "agy /usage", "groups": groups}
        return data


class AntigravitySdkInfo:
    async def fetch(self, *, capabilities: bool = True, usage: bool = True) -> dict:
        data = {"agent": "antigravity", "backend": "antigravity_sdk", "fetched_at": _utc_now()}
        if capabilities:
            data["capabilities"] = {"available": False, "reason": "The SDK does not expose the signed-in CLI model catalog"}
        if usage:
            data["usage"] = {"available": False, "reason": "The SDK does not expose Antigravity CLI account quotas"}
        return data


class CodexInfo:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)

    async def fetch(self, *, capabilities: bool = True, usage: bool = True) -> dict:
        from openai_codex import AsyncCodex, CodexConfig
        from openai_codex.generated.v2_all import ConfigReadResponse, GetAccountRateLimitsResponse

        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        config = CodexConfig(env={**os.environ, "CODEX_HOME": codex_home})
        data = {"agent": "codex", "backend": "codex_app_server", "fetched_at": _utc_now()}
        async with AsyncCodex(config) as codex:
            if capabilities:
                catalog = await codex.models()
                # The Python SDK has public models(), but config/read currently uses its typed transport.
                effective = await codex._client.request(
                    "config/read", {"cwd": str(self.workspace)}, response_model=ConfigReadResponse
                )
                configured_model = effective.config.model
                selected = next(
                    (model for model in catalog.data if configured_model in (model.model, model.id)),
                    None,
                )
                if selected is None:
                    selected = next((model for model in catalog.data if model.is_default), None)
                data["capabilities"] = {
                    "selected_model": configured_model or (selected.model if selected else None),
                    "model_access_note": (
                        "selected_model is the local Codex configuration, not an account access check. "
                        "The App Server model catalog may be cached; a listed model can still be "
                        "rejected for the signed-in ChatGPT account. Omit the model override unless "
                        "the caller explicitly requests it."
                    ),
                    "selected_effort": _enum_value(
                        effective.config.model_reasoning_effort
                        or (selected.default_reasoning_effort if selected else None)
                    ),
                    "models": [
                        {
                            "id": model.model,
                            "label": model.display_name,
                            "default_effort": _enum_value(model.default_reasoning_effort),
                            "efforts": [_enum_value(option.reasoning_effort) for option in model.supported_reasoning_efforts],
                            "is_default": model.is_default,
                        }
                        for model in catalog.data
                    ],
                }
            if usage:
                # account/rateLimits/read is a read-only App Server method with generated SDK types.
                snapshot = await codex._client.request(
                    "account/rateLimits/read", {}, response_model=GetAccountRateLimitsResponse
                )
                limits = snapshot.rate_limits_by_limit_id or {
                    snapshot.rate_limits.limit_id or "codex": snapshot.rate_limits.model_dump(by_alias=True)
                }
                groups = []
                for limit_id, limit in limits.items():
                    buckets = []
                    for name in ("primary", "secondary"):
                        window = limit.get(name)
                        if not window:
                            continue
                        used = window.get("usedPercent")
                        reset = window.get("resetsAt")
                        buckets.append(
                            {
                                "id": f"{limit_id}:{name}",
                                "name": name,
                                "window_minutes": window.get("windowDurationMins"),
                                "used_percent": used,
                                "remaining_percent": max(0, 100 - used) if used is not None else None,
                                "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat().replace("+00:00", "Z") if reset else None,
                            }
                        )
                    groups.append(
                        {
                            "name": limit.get("limitName") or limit_id,
                            "limit_id": limit_id,
                            "normal_model_slug": limit.get("normalModelSlug"),
                            "plan_type": limit.get("planType"),
                            "credits": limit.get("credits"),
                            "individual_limit": limit.get("individualLimit"),
                            "rate_limit_reached_type": limit.get("rateLimitReachedType"),
                            "spend_control_reached": limit.get("spendControlReached"),
                            "buckets": buckets,
                        }
                    )
                data["usage"] = {
                    "available": True,
                    "source": "Codex App Server account/rateLimits/read",
                    "ordinary_usage_allowed": snapshot.ordinary_usage_allowed,
                    "rate_limit_reset_credits": (
                        snapshot.rate_limit_reset_credits.model_dump(by_alias=True, exclude_none=True)
                        if snapshot.rate_limit_reset_credits else None
                    ),
                    "groups": groups,
                }
        return _json_ready(data)
