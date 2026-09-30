$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$bootstrap = $env:BRIDGE_BOOTSTRAP_PYTHON
if (-not $bootstrap) {
    foreach ($candidate in @('py', 'python3', 'python')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { $bootstrap = $found.Source; break }
    }
}
if (-not $bootstrap) {
    throw 'Python 3.11+ was not found. Put it in PATH or set BRIDGE_BOOTSTRAP_PYTHON.'
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & $bootstrap -m venv $venv
}
& $python -m pip install --upgrade pip
& $python -m pip install -e $root

if ($env:BRIDGE_INSTALL_AGY_SDK -eq '1') {
    $agyVenv = Join-Path $root '.venv-agy'
    $agyPython = Join-Path $agyVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $agyPython -PathType Leaf)) {
        & $bootstrap -m venv $agyVenv
    }
    & $agyPython -m pip install --upgrade pip
    & $agyPython -m pip install -r (Join-Path $root 'requirements-antigravity.txt')
}

Write-Output "Installed agent-shuttle in $venv"
Write-Output 'Run .\Configure-Shuttle-Mcp.ps1, then .\Start-Shuttle.ps1'
