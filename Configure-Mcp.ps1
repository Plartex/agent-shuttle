$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Run .\Install-Shuttle.ps1 first.'
}

$codexDir = Join-Path $root '.codex'
$agentsDir = Join-Path $root '.agents'
New-Item -ItemType Directory -Path $codexDir, $agentsDir -Force | Out-Null

$tomlPath = $python.Replace("'", "''")
$tomlRoot = $root.Replace("'", "''")
$toml = @"
[mcp_servers.agent_shuttle]
command = '$tomlPath'
args = ["-m", "agent_shuttle.mcp_server"]
tool_timeout_sec = 1800

[mcp_servers.agent_shuttle.env]
BRIDGE_CODEX_URL = "http://127.0.0.1:8765"
BRIDGE_ANTIGRAVITY_URL = "http://127.0.0.1:8766"
BRIDGE_ANTIGRAVITY_WORKSPACE = '$tomlRoot'
"@
Set-Content -LiteralPath (Join-Path $codexDir 'config.toml') -Value $toml -Encoding utf8

$json = @{
    mcpServers = @{
        'agent-shuttle' = @{
            command = $python
            args = @('-m', 'agent_shuttle.mcp_server')
            env = @{
                BRIDGE_CODEX_URL = 'http://127.0.0.1:8765'
                BRIDGE_ANTIGRAVITY_URL = 'http://127.0.0.1:8766'
                BRIDGE_ANTIGRAVITY_WORKSPACE = $root
            }
        }
    }
} | ConvertTo-Json -Depth 10
Set-Content -LiteralPath (Join-Path $agentsDir 'mcp_config.json') -Value $json -Encoding utf8

Write-Output 'Created .codex/config.toml and .agents/mcp_config.json with local absolute paths.'
Write-Output 'For Antigravity CLI, register the same server globally with `agy mcp add` if workspace discovery is unavailable.'
