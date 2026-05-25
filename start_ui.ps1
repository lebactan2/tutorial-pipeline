<#
.SYNOPSIS
    Starts the web UI for the Tutorial Pipeline.
#>

$ErrorActionPreference = "Stop"
$PipelineRoot = "C:\tutorial-pipeline"

# Activate venv
$venvActivate = Join-Path $PipelineRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
    Write-Host "Virtual environment activated." -ForegroundColor Green
} else {
    Write-Host "Virtual environment not found. Ensure setup.ps1 has been run." -ForegroundColor Red
    exit 1
}

# Run app.py
$appScript = Join-Path $PipelineRoot "scripts\app.py"
Write-Host "Starting Web UI at http://127.0.0.1:5000..." -ForegroundColor Cyan
python $appScript
