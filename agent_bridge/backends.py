"""Execution adapters for one-shot calls and reusable agent conversations."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AntigravityPermissionDenied(RuntimeError):
    """The CLI completed a turn without permission for a requested tool."""


class AntigravityAuthenticationError(RuntimeError):
    """The CLI cannot use its account from the current process context."""


def _agy_authentication_error(stderr: bytes) -> AntigravityAuthenticationError | None:
    message = stderr.decode(errors="replace").lower()
    if not any(marker in message for marker in (
        "you are not logged into antigravity",
        "authentication failed or timed out",
        "print mode: auth timed out",
    )):
        return None
    if "access is denied" in message or "permission denied" in message:
        return AntigravityAuthenticationError(
            "Antigravity CLI authentication is unavailable in this process: "
            "access to its credential store or configuration was denied. "
            "Start Agent Shuttle as a normal user outside the caller's sandbox; "
            "a successful Antigravity desktop sign-in does not grant a sandboxed CLI access."
        )
    return AntigravityAuthenticationError(
        "Antigravity CLI authentication is unavailable. Verify that the CLI is signed in "
        "from the same user account and process context used to start Agent Shuttle."
    )


def _retryable_agy_preflight_failure(stderr: bytes) -> bool:
    """Retry only a known failure before Antigravity starts the agent turn."""
    message = stderr.decode(errors="replace").lower()
    return (
        "eligibility check failed" in message
        and (
            ("failed to get profile picture" in message and "tls handshake timeout" in message)
            or ("unavailable (code 503)" in message and "service is currently unavailable" in message)
        )
    )


async def _read_agy_headless_output(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Drain both outputs while keeping stdin open for the CLI's entire turn."""
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return await process.communicate()
    try:
        stdout, stderr, _ = await asyncio.gather(
            process.stdout.read(), process.stderr.read(), process.wait(),
        )
        return stdout, stderr
    finally:
        try:
            stdin.close()
        except OSError:
            pass


def _check_agy_denials(result: dict) -> None:
    denied = result.get("denied_actions")
    if not denied:
        return
    actions = denied if isinstance(denied, list) else [denied]
    labels = []
    for action in actions:
        if isinstance(action, dict):
            label = action.get("display_name") or action.get("action")
        else:
            label = None
        if isinstance(label, str) and label.strip():
            labels.append(label.strip()[:100])
    names = ", ".join(labels) if labels else "an unnamed tool"
    raise AntigravityPermissionDenied(
        f"agy denied a required tool ({names}); the result may be incomplete. "
        "Review scoped permissions.allow rules in Antigravity CLI settings."
    )


def _decode_agy_result(stdout: bytes, stderr: bytes) -> str:
    """Validate headless CLI output, including its exit-zero soft-denial case."""
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = stderr.decode(errors="replace")[-4000:].strip()
        raise RuntimeError(f"agy returned invalid JSON: {detail or exc}") from exc
    if result.get("status") != "SUCCESS":
        raise RuntimeError(str(result.get("error") or result.get("status") or "agy failed"))
    _check_agy_denials(result)
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        detail = stderr.decode(errors="replace")[-4000:].strip()
        message = (
            "agy reported SUCCESS but returned an empty response; in headless mode this "
            "usually means a requested tool was soft-denied by the permission policy"
        )
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message)
    return response


def _decode_agy_usage(stdout: bytes) -> dict[str, int]:
    raw_usage = json.loads(stdout).get("usage") or {}
    if not isinstance(raw_usage, dict):
        return {}
    return {
        key: value
        for key, value in raw_usage.items()
        if key in {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


class Backend(Protocol):
    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str | "BackendResponse": ...

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> "BackendSession": ...


class BackendSession(Protocol):
    async def ask(self, prompt: str) -> str | "BackendResponse": ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class BackendResponse:
    text: str
    usage: dict[str, int]
    details: dict | None = None


def _codex_response(result) -> BackendResponse:
    """Codex exposes both cumulative and last-turn counters; report the latter."""
    last = result.usage.last if result.usage is not None else None
    usage = {} if last is None else {
        "input_tokens": last.input_tokens,
        "output_tokens": last.output_tokens,
        "total_tokens": last.total_tokens,
        "thinking_tokens": last.reasoning_output_tokens,
        "cache_read_tokens": last.cached_input_tokens,
    }
    if last is not None and last.cache_write_input_tokens is not None:
        usage["cache_write_tokens"] = last.cache_write_input_tokens
    return BackendResponse(result.final_response or "", usage)


def _codex_sandbox(sandbox_type, read_only: bool, tool_policy: str | None):
    if read_only and tool_policy not in (None, "read_only"):
        raise ValueError("read_only conflicts with tool_policy")
    policy = tool_policy or ("read_only" if read_only else "workspace_write")
    if policy == "no_tools":
        raise ValueError("Codex cannot enforce no_tools")
    choices = {
        "read_only": sandbox_type.read_only,
        "workspace_write": sandbox_type.workspace_write,
        "full_access": sandbox_type.full_access,
    }
    try:
        return choices[policy]
    except KeyError as exc:
        raise ValueError(f"Unknown Codex tool_policy {policy!r}") from exc


class CodexBackend:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendResponse:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        sandbox = _codex_sandbox(Sandbox, read_only, tool_policy)

        # The Windows CLI needs an explicit home in some non-interactive shells.
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        config = CodexConfig(env={**os.environ, "CODEX_HOME": codex_home})
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None
                    else None
                ),
                sandbox=sandbox,
            )
            result = await thread.run(prompt)
            return _codex_response(result)

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendSession:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        sandbox = _codex_sandbox(Sandbox, read_only, tool_policy)

        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        codex = AsyncCodex(CodexConfig(env={**os.environ, "CODEX_HOME": codex_home}))
        await codex.__aenter__()
        try:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None else None
                ),
                sandbox=sandbox,
            )
        except BaseException:
            await codex.__aexit__(None, None, None)
            raise
        return _CodexSession(codex, thread)


class _CodexSession:
    def __init__(self, codex, thread):
        self.codex = codex
        self.thread = thread

    async def ask(self, prompt: str) -> BackendResponse:
        result = await self.thread.run(prompt)
        return _codex_response(result)

    async def close(self) -> None:
        await self.codex.__aexit__(None, None, None)


class AntigravityCliBackend:
    """Run the official `agy` headless CLI with its configured sign-in."""

    def __init__(
        self, workspace: Path, command: str = "agy", *,
        dangerously_skip_permissions: bool = False,
        turn_timeout_seconds: float = 300,
    ):
        if not isinstance(dangerously_skip_permissions, bool):
            raise TypeError("dangerously_skip_permissions must be a boolean")
        if (not isinstance(turn_timeout_seconds, (int, float))
                or isinstance(turn_timeout_seconds, bool)
                or not math.isfinite(turn_timeout_seconds)
                or turn_timeout_seconds <= 0):
            raise ValueError("turn_timeout_seconds must be a positive finite number")
        self.workspace = workspace.resolve(strict=True)
        self.command = command
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.turn_timeout_seconds = turn_timeout_seconds

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendResponse:
        if read_only:
            raise ValueError("Antigravity CLI cannot enforce read-only tools in headless mode")
        if tool_policy is not None and not (
            tool_policy == "full_access" and self.dangerously_skip_permissions
        ):
            raise ValueError(f"Antigravity CLI cannot enforce {tool_policy}")
        command = [self.command, "-p", prompt, "--output-format", "json",
                   "--print-timeout", f"{self.turn_timeout_seconds:g}s"]
        if self.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        deadline = asyncio.get_running_loop().time() + self.turn_timeout_seconds
        for attempt in range(3):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"agy did not return a result within {self.turn_timeout_seconds:g}s"
                )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    _read_agy_headless_output(process), timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise TimeoutError(
                    f"agy did not return a result within {self.turn_timeout_seconds:g}s"
                ) from exc
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            if process.returncode:
                authentication_error = _agy_authentication_error(stderr)
                if authentication_error is not None:
                    raise authentication_error
                retryable_preflight = _retryable_agy_preflight_failure(stderr)
                if attempt < 2 and retryable_preflight:
                    delay = (attempt + 1) * (
                        1.0 if b"unavailable (code 503)" in stderr.lower() else 0.5
                    )
                    await asyncio.sleep(min(delay, max(0, deadline - asyncio.get_running_loop().time())))
                    continue
                if retryable_preflight and b"unavailable (code 503)" in stderr.lower():
                    selected_model = model or "the CLI's default model"
                    raise RuntimeError(
                        f"Antigravity upstream service is unavailable for {selected_model} "
                        f"after {attempt + 1} attempt(s) (eligibility check HTTP 503). "
                        "The requested model was not switched; retry later or explicitly "
                        "select another model."
                    )
                raise RuntimeError(
                    f"agy failed ({process.returncode}) after {attempt + 1} attempt(s): "
                    f"{stderr.decode(errors='replace')[-4000:]}"
                )
            response = _decode_agy_result(stdout, stderr)
            details = {"preflight_retries": attempt} if attempt else None
            return BackendResponse(response, _decode_agy_usage(stdout), details)
        raise AssertionError("Antigravity retry loop exhausted unexpectedly")

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendSession:
        if read_only:
            raise ValueError("Antigravity CLI cannot enforce read-only tools in headless mode")
        if tool_policy is not None and not (
            tool_policy == "full_access" and self.dangerously_skip_permissions
        ):
            raise ValueError(f"Antigravity CLI cannot enforce {tool_policy}")
        command = [self.command, "--input-format", "stream-json", "--output-format", "stream-json", "--print-timeout", "30m"]
        if self.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2_000_000,
        )
        return _AntigravityCliSession(process)


class _AntigravityCliSession:
    def __init__(self, process: asyncio.subprocess.Process, *, turn_timeout_seconds: float = 1800):
        self.process = process
        self.turn_timeout_seconds = turn_timeout_seconds
        self.previous_usage: dict[str, int] = {}
        self.stderr_tail = b""
        self.stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + chunk)[-4000:]

    async def ask(self, prompt: str) -> BackendResponse:
        try:
            async with asyncio.timeout(self.turn_timeout_seconds):
                return await self._ask_within_deadline(prompt)
        except TimeoutError as exc:
            if self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
            await self.stderr_task
            raise TimeoutError(
                f"agy session did not return a result within {self.turn_timeout_seconds:g}s"
            ) from exc

    async def _ask_within_deadline(self, prompt: str) -> BackendResponse:
        if self.process.returncode is not None:
            await self.stderr_task
            authentication_error = _agy_authentication_error(self.stderr_tail)
            if authentication_error is not None:
                raise authentication_error
            raise RuntimeError(f"agy session exited ({self.process.returncode}): {self.stderr_tail.decode(errors='replace')}")
        assert self.process.stdin is not None and self.process.stdout is not None
        event = {"event": "user", "message": {"content": prompt}}
        self.process.stdin.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        while line := await self.process.stdout.readline():
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"agy session returned invalid JSON: {exc}") from exc
            if payload.get("event") != "result":
                continue
            result = payload.get("result") or {}
            if result.get("status") != "SUCCESS":
                raise RuntimeError(str(result.get("error") or result.get("status") or "agy session failed"))
            _check_agy_denials(result)
            response = result.get("response")
            if not isinstance(response, str) or not response.strip():
                raise RuntimeError(
                    "agy session returned an empty response; a tool may have been soft-denied: "
                    + self.stderr_tail.decode(errors="replace")
                )
            cumulative = _decode_agy_usage(json.dumps(result).encode("utf-8"))
            delta = {
                key: max(value - self.previous_usage.get(key, 0), 0)
                for key, value in cumulative.items()
            }
            self.previous_usage = cumulative
            return BackendResponse(response, delta)
        await self.stderr_task
        authentication_error = _agy_authentication_error(self.stderr_tail)
        if authentication_error is not None:
            raise authentication_error
        raise RuntimeError(f"agy session closed before result: {self.stderr_tail.decode(errors='replace')}")

    async def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        await self.stderr_task


def default_agy_python() -> Path:
    root = Path(__file__).resolve().parents[2]
    folder = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return root / ".venv-agy" / folder / name


class AntigravitySdkBackend:
    """Optional Antigravity SDK adapter in a separate Protobuf environment."""

    def __init__(self, workspace: Path, python: Path | None = None):
        self.workspace = workspace.resolve(strict=True)
        self.python = (python or default_agy_python()).resolve()

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str:
        if read_only:
            raise ValueError("Antigravity SDK cannot enforce read-only tools")
        if not self.python.is_file():
            raise RuntimeError(f"Antigravity Python environment missing: {self.python}")
        if reasoning_effort is not None:
            raise RuntimeError(
                "Antigravity SDK mode does not expose reasoning effort; use the default CLI mode"
            )
        process = await asyncio.create_subprocess_exec(
            str(self.python),
            "-m",
            "agent_bridge.agy_worker",
            cwd=str(self.workspace),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps({"prompt": prompt, "workspace": str(self.workspace), "model": model}).encode()
        try:
            stdout, stderr = await process.communicate(payload)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        marker = b"AGENT_BRIDGE_RESULT="
        lines = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
        if process.returncode or not lines:
            detail = stderr.decode(errors="replace")[-4000:]
            raise RuntimeError(f"Antigravity worker failed ({process.returncode}): {detail}")
        result = json.loads(lines[-1])
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Unknown Antigravity error"))
        return str(result.get("text", ""))

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> BackendSession:
        raise NotImplementedError("Persistent sessions require Antigravity CLI mode")
