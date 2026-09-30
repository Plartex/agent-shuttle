$ErrorActionPreference = 'Stop'
$bridgeRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $PSScriptRoot '.runtime'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Bridge environment missing: $python"
}
if ($env:BRIDGE_AGY_MODE -eq 'sdk') {
    if (-not (Test-Path -LiteralPath (Join-Path $bridgeRoot '.venv-agy\Scripts\python.exe'))) {
        Write-Warning 'Optional Antigravity SDK environment is missing.'
    }
} else {
    $agyCommand = $env:BRIDGE_AGY_COMMAND
    if (-not $agyCommand) {
        $candidate = Join-Path $bridgeRoot 'bin\agy.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $agyCommand = $candidate }
    }
    if (-not $agyCommand) {
        $found = Get-Command agy -ErrorAction SilentlyContinue
        if ($found) { $agyCommand = $found.Source }
    }
    if (-not $agyCommand) {
        $candidate = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $agyCommand = $candidate }
    }
    if (-not $agyCommand) {
        $agyCommand = 'agy'
        Write-Warning 'agy CLI is not installed; Antigravity tasks need it in the default harness mode.'
    }
    $env:BRIDGE_AGY_COMMAND = $agyCommand
}
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
function Get-BridgeIdentity([int]$port) {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$port/bridge/identity" -TimeoutSec 2 -ErrorAction Stop
    } catch {
        return $null
    }
}
function Assert-BridgeProcess($identity, [string]$agentName, [int]$port) {
    $expectedBackend = if ($agentName -eq 'codex') { 'codex_app_server' } else { 'agy_cli' }
    if ($identity.agent -ne $agentName -or $identity.backend -ne $expectedBackend -or
        $identity.workspace -ne $bridgeRoot -or [int]$identity.pid -le 0) {
        throw "Port $port has a Bridge with unexpected identity or workspace"
    }
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -eq [int]$identity.pid } | Select-Object -First 1
    if (-not $listener) { throw "Port $port is not owned by Bridge PID $($identity.pid)" }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($identity.pid)" -ErrorAction Stop
    $pattern = "(?i)-m\s+agent_bridge\.cli\s+serve\s+$agentName\s+--port\s+$port(?:\s|$)"
    if (-not $owner -or $owner.CommandLine -notmatch $pattern) {
        throw "PID $($identity.pid) does not run the expected Agent Bridge command"
    }
}
foreach ($agent in @(@{ Name = 'codex'; Port = 8765 }, @{ Name = 'antigravity'; Port = 8766 })) {
    $pidFile = Join-Path $runtime "$($agent.Name).pid"
    $identity = Get-BridgeIdentity $agent.Port
    if ($identity) {
        Assert-BridgeProcess $identity $agent.Name $agent.Port
        Set-Content -LiteralPath $pidFile -Value ([int]$identity.pid)
        Write-Output "$($agent.Name) already running (PID $($identity.pid))"
        continue
    }
    $occupied = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $agent.Port -State Listen -ErrorAction SilentlyContinue
    if ($occupied) { throw "Port $($agent.Port) is occupied by a non-Bridge process" }
    $launchArgs = @('-m', 'agent_bridge.cli', 'serve', $agent.Name, '--port', [string]$agent.Port)
    if ($agent.Name -eq 'antigravity') {
        if ($env:BRIDGE_AGY_MODE -eq 'sdk') {
            $launchArgs += @('--agy-mode', 'sdk')
        }
    }
    Start-Process -FilePath $python `
        -ArgumentList $launchArgs `
        -WorkingDirectory $bridgeRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "$($agent.Name).out.log") `
        -RedirectStandardError (Join-Path $runtime "$($agent.Name).err.log") | Out-Null
    $identity = $null
    for ($attempt = 0; $attempt -lt 80 -and -not $identity; $attempt++) {
        Start-Sleep -Milliseconds 250
        $identity = Get-BridgeIdentity $agent.Port
    }
    if (-not $identity) {
        $tail = Get-Content -LiteralPath (Join-Path $runtime "$($agent.Name).err.log") -Tail 20 -ErrorAction SilentlyContinue
        throw "$($agent.Name) Bridge did not become ready on port $($agent.Port): $tail"
    }
    Assert-BridgeProcess $identity $agent.Name $agent.Port
    Set-Content -LiteralPath $pidFile -Value ([int]$identity.pid)
    Write-Output "$($agent.Name): http://127.0.0.1:$($agent.Port) (PID $($identity.pid))"
}
