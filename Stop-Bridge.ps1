$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.runtime'
$bridgeRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
foreach ($agent in @(@{ Name = 'codex'; Port = 8765 }, @{ Name = 'antigravity'; Port = 8766 })) {
    $pidFile = Join-Path $runtime "$($agent.Name).pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    try {
        $identity = Invoke-RestMethod -Uri "http://127.0.0.1:$($agent.Port)/bridge/identity" -TimeoutSec 2 -ErrorAction Stop
    } catch {
        $identity = $null
    }
    if ($identity) {
        $expectedBackend = if ($agent.Name -eq 'codex') { 'codex_app_server' } else { 'agy_cli' }
        if ($identity.agent -ne $agent.Name -or $identity.backend -ne $expectedBackend -or
            $identity.workspace -ne $bridgeRoot -or [int]$identity.pid -ne $bridgePid) {
            throw "Refusing to stop port $($agent.Port): identity does not match $pidFile"
        }
        $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $agent.Port -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $bridgePid } | Select-Object -First 1
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $bridgePid" -ErrorAction Stop
        $pattern = "(?i)-m\s+agent_bridge\.cli\s+serve\s+$($agent.Name)\s+--port\s+$($agent.Port)(?:\s|$)"
        if (-not $listener -or -not $owner -or $owner.CommandLine -notmatch $pattern) {
            throw "Refusing to stop PID ${bridgePid}: process ownership could not be verified"
        }
        Stop-Process -Id $bridgePid -Force -ErrorAction Stop
        Write-Output "Stopped $($agent.Name) (PID $bridgePid)"
    } else {
        $occupied = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $agent.Port -State Listen -ErrorAction SilentlyContinue
        if ($occupied) {
            throw "Refusing to discard ${pidFile}: port $($agent.Port) is listening but identity is unavailable"
        }
        Write-Output "Removed stale $($agent.Name) PID file (server is not reachable)"
    }
    Remove-Item -LiteralPath $pidFile
}
