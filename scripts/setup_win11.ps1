$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonExe = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $resolvedPython = & py -3.14 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonExe = ($resolvedPython | Select-Object -Last 1).Trim()
    }
}

if (-not $pythonExe) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $resolvedPython = & $pythonCommand.Source -c "import sys; print(sys.executable if sys.version_info >= (3, 14) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolvedPython) {
            $pythonExe = ($resolvedPython | Select-Object -Last 1).Trim()
        }
    }
}

if (-not $pythonExe) {
    throw "Python 3.14 or newer is required. Install it from https://www.python.org/downloads/windows/ and rerun this script."
}

$venvDir = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (Test-Path $venvPython) {
    $venvVersionText = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the existing .venv Python version."
    }
    if ([version]$venvVersionText -lt [version]"3.14") {
        throw "The existing .venv uses Python $venvVersionText. Rename or remove it yourself, then rerun this script to create a Python 3.14+ environment."
    }
} else {
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the .venv virtual environment."
    }
}

& $venvPython -m pip install -r (Join-Path $repoRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements.txt."
}

& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the Playwright Chromium browser."
}

Write-Host "Setup complete. Activate the environment with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "Then start the server with:"
Write-Host "  python deepseek_api_server.py"
