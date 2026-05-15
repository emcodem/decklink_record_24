# Sets up the ffrecord virtual environment.
# Run once from the project root: .\install.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

# Create venv if missing
if (-not (Test-Path "$Root\venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv "$Root\venv"
}

$pip = "$Root\venv\Scripts\pip.exe"

# Install project deps; --find-links makes pip prefer vendor/ wheel for av
# over the lite PyPI build (which lacks NVENC support).
Write-Host "Installing dependencies..."
& $pip install -e $Root --find-links="$Root\vendor"

Write-Host "Done. Activate with: venv\Scripts\Activate.ps1"
