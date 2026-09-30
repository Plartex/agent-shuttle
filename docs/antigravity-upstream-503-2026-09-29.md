# Antigravity eligibility 503: trace and mitigation (2026-09-29)

## Observed trace

After the Bridge authentication/process-context fix, a short MCP call with `model=gemini-3.8-flash-low` failed:

```text
TASK_STATE_FAILED
RuntimeError: agy failed (1) after 1 attempt(s):
error: Eligibility check failed: UNAVAILABLE (code 503):
The service is currently unavailable.
```

The same Bridge server could query `agy models` and `/usage`. A short call with `gemini-3.7-flash-low` completed, and later short calls with `gemini-3.8-flash-low` completed both directly through the updated backend and through the restarted MCP server. Thus this was not the previous credential-store denial or a permanently unsupported model. The CLI reported a transient upstream eligibility-service failure; listing a model did not guarantee that its next execution would be available.

## Local defect and TDD reproduction

`AntigravityCliBackend.run()` already retried one known pre-turn TLS failure, but its `_retryable_agy_preflight_failure` predicate rejected the exact eligibility 503 stderr above. It therefore failed after the first attempt. `test_antigravity_retries_transient_eligibility_503_without_switching_model` reproduced that failure before the implementation change. Companion tests cover exhausted retries and a 503 reported after the agent turn starts, which must **not** be replayed automatically.

The backend now retries this exact preflight 503 at most twice, subject to the original total turn deadline, always using the originally requested model. If all three attempts fail, it reports the upstream 503, model and attempt count with explicit advice to retry later or choose another model. This mitigates a transient outage; it cannot repair a persistent upstream outage or guarantee model availability.

While deploying the change, `Stop-Bridge.ps1` failed to parse because PowerShell interpreted `$pidFile:` as an invalid variable reference. A PowerShell parser regression test caught the issue; the string was fixed, and the local Codex/Antigravity Bridge processes were restarted so MCP uses the new backend code.

## Verification

- New 503 regression test initially failed with `agy failed (1) after 1 attempt(s)` and passed after the fix.
- `python -m coverage run --branch --source=agent_bridge -m unittest discover -s tests -q`: 158 tests run, 5 opt-in live tests skipped, no failures.
- `python -m coverage report --fail-under=90`: 90% overall coverage.
- Direct backend call to `gemini-3.8-flash-low` returned `OK`.
- MCP call through the restarted Bridge to `gemini-3.8-flash-low` returned `TASK_STATE_COMPLETED` and `РАБОТАЕТ`.

The live successes demonstrate current service availability and deployment of the new code, not a live replay of an actual 503 followed by a successful retry. That retry sequence is verified by the deterministic regression tests.
