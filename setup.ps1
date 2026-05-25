#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Tutorial Pipeline - Automated Environment Setup
.DESCRIPTION
    Installs all dependencies for the bilingual tutorial video pipeline.
    Run this once on a fresh Windows 10/11 machine.
    Must be run as Administrator (for winget installs).
.NOTES
    Logs all output to logs\setup.log
#>

$ErrorActionPreference = "Stop"
$PipelineRoot = "C:\tutorial-pipeline"
$LogFile = Join-Path $PipelineRoot "logs\setup.log"

# Ensure log directory exists
if (-not (Test-Path (Join-Path $PipelineRoot "logs"))) {
    New-Item -ItemType Directory -Path (Join-Path $PipelineRoot "logs") -Force | Out-Null
}

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Test-Command {
    param([string]$Command)
    try {
        Get-Command $Command -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Log "PATH refreshed"
}

# -------------------------------------------------
# 1. Create folder structure
# -------------------------------------------------
Write-Log "=== Phase 1: Project Structure ==="
$folders = @("scripts", "config", "voice_refs", "inbox", "working", "output", "logs")
foreach ($f in $folders) {
    $path = Join-Path $PipelineRoot $f
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Write-Log "Created folder: $f"
    } else {
        Write-Log "Folder exists: $f"
    }
}

# Copy .env template if .env doesn't exist
$envFile = Join-Path $PipelineRoot "config\.env"
$envTemplate = Join-Path $PipelineRoot "config\.env.template"
if (-not (Test-Path $envFile) -and (Test-Path $envTemplate)) {
    Copy-Item $envTemplate $envFile
    Write-Log "Created config\.env from template -- add your API keys there"
}

# -------------------------------------------------
# 2. Install system dependencies
# -------------------------------------------------
Write-Log "=== Phase 2: System Dependencies ==="

# Python 3.11
if (Test-Command "python") {
    $pyVer = & python --version 2>&1
    if ($pyVer -match "3\.11") {
        Write-Log "Python 3.11 already installed: $pyVer"
    } else {
        Write-Log "Python found but not 3.11 ($pyVer). Installing Python 3.11..." "WARN"
        winget install Python.Python.3.11 --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
} else {
    Write-Log "Installing Python 3.11..."
    winget install Python.Python.3.11 --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

# FFmpeg
if (Test-Command "ffmpeg") {
    $ffVer = & ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Log "FFmpeg already installed: $ffVer"
} else {
    Write-Log "Installing FFmpeg..."
    winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

# Git
if (Test-Command "git") {
    $gitVer = & git --version 2>&1
    Write-Log "Git already installed: $gitVer"
} else {
    Write-Log "Installing Git..."
    winget install Git.Git --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

# uv
if (Test-Command "uv") {
    $uvVer = & uv --version 2>&1
    Write-Log "uv already installed: $uvVer"
} else {
    Write-Log "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Refresh-Path
}

# Verify all installed
Write-Log "=== Verifying installations ==="
$required = @("python", "ffmpeg", "git", "uv")
$allGood = $true
foreach ($cmd in $required) {
    if (Test-Command $cmd) {
        Write-Log "  OK: $cmd"
    } else {
        Write-Log "  MISSING: $cmd -- please install manually and re-run" "ERROR"
        $allGood = $false
    }
}
if (-not $allGood) {
    Write-Log "Some dependencies are missing. Fix the errors above and re-run setup." "ERROR"
    exit 1
}

# -------------------------------------------------
# 3. Python virtual environment
# -------------------------------------------------
Write-Log "=== Phase 3: Python Environment ==="
Set-Location $PipelineRoot

$venvPath = Join-Path $PipelineRoot ".venv"
if (-not (Test-Path $venvPath)) {
    Write-Log "Creating virtual environment with Python 3.11..."
    & uv venv --python 3.11
} else {
    Write-Log "Virtual environment already exists"
}

# Activate venv
$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
if (Test-Path $activateScript) {
    . $activateScript
    Write-Log "Virtual environment activated"
} else {
    Write-Log "Cannot find venv activation script at $activateScript" "ERROR"
    exit 1
}

# Install Python packages
Write-Log "Installing Python packages..."
& uv pip install -r (Join-Path $PipelineRoot "requirements.txt")
Write-Log "Core packages installed"

# -------------------------------------------------
# 4. Install VieNeu-TTS
# -------------------------------------------------
Write-Log "=== Phase 4: VieNeu-TTS ==="
$vieneuDir = Join-Path $PipelineRoot "vieneu"

if (-not (Test-Path $vieneuDir)) {
    Write-Log "Cloning VieNeu-TTS..."
    & git clone https://github.com/pnnbao97/VieNeu-TTS.git $vieneuDir
} else {
    Write-Log "VieNeu-TTS directory already exists, pulling latest..."
    Set-Location $vieneuDir
    & git pull
    Set-Location $PipelineRoot
}

Write-Log "Installing VieNeu-TTS..."
try {
    Set-Location $vieneuDir
    & uv pip install -e .
    Write-Log "VieNeu-TTS installed (editable mode)"
} catch {
    Write-Log "Standard install failed, trying turbo mode: $_" "WARN"
    try {
        & uv pip install "vieneu[turbo]"
        Write-Log "VieNeu-TTS installed (turbo/pip fallback)"
    } catch {
        Write-Log "VieNeu-TTS install failed completely: $_" "ERROR"
        Write-Log "You may need to install PyTorch manually first: pip install torch torchaudio" "ERROR"
    }
}
Set-Location $PipelineRoot

# Quick test
Write-Log "Testing VieNeu-TTS import..."
Set-Location (Join-Path $PipelineRoot "scripts")
$testResult = & python -c "from vieneu import Vieneu; print('VieNeu-TTS OK')" 2>&1
Set-Location $PipelineRoot
if ($testResult -match "OK") {
    Write-Log "VieNeu-TTS test passed"
} else {
    Write-Log "VieNeu-TTS import test failed: $testResult" "WARN"
    Write-Log "Voice generation may not work -- check logs\setup.log for details" "WARN"
}

# -------------------------------------------------
# 5. GPU Detection
# -------------------------------------------------
Write-Log "=== Phase 5: GPU Detection ==="
$systemConfig = Join-Path $PipelineRoot "config\system.json"

$gpuAvailable = $false
$gpuName = ""
$vramMb = 0

try {
    $nvsmi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1
    if ($LASTEXITCODE -eq 0 -and $nvsmi -notmatch "not recognized") {
        $gpuAvailable = $true
        $parts = $nvsmi -split ","
        $gpuName = $parts[0].Trim()
        if ($parts[1] -match "(\d+)") {
            $vramMb = [int]$Matches[1]
        }
        Write-Log "GPU detected: $gpuName ($vramMb MB VRAM)"
    }
} catch {
    Write-Log "No NVIDIA GPU detected (nvidia-smi failed)"
}

# Determine optimal settings
if ($gpuAvailable -and $vramMb -ge 8000) {
    $whisperModel = "large-v3"
    $whisperDevice = "cuda"
    $whisperCompute = "float16"
    $vieneuMode = "full"
    $vieneuDevice = "cuda"
} elseif ($gpuAvailable -and $vramMb -ge 4000) {
    $whisperModel = "medium"
    $whisperDevice = "cuda"
    $whisperCompute = "int8"
    $vieneuMode = "turbo"
    $vieneuDevice = "cpu"
} else {
    $whisperModel = "medium"
    $whisperDevice = "cpu"
    $whisperCompute = "int8"
    $vieneuMode = "turbo"
    $vieneuDevice = "cpu"
}

$config = @{
    gpu = @{
        available = $gpuAvailable
        name = $gpuName
        vram_mb = $vramMb
    }
    whisper = @{
        model_size = $whisperModel
        device = $whisperDevice
        compute_type = $whisperCompute
    }
    vieneu = @{
        mode = $vieneuMode
        device = $vieneuDevice
    }
    _note = "Auto-generated by setup.ps1 on $(Get-Date -Format 'yyyy-MM-dd'). Edit manually only if GPU detection was wrong."
}

$config | ConvertTo-Json -Depth 3 | Set-Content $systemConfig -Encoding UTF8
Write-Log "System config written to config\system.json"
Write-Log "  Whisper: $whisperModel on $whisperDevice ($whisperCompute)"
Write-Log "  VieNeu: $vieneuMode mode on $vieneuDevice"

# -------------------------------------------------
# Done
# -------------------------------------------------
Write-Log "========================================="
Write-Log "Setup complete!"
Write-Log "========================================="
Write-Log ""
Write-Log "Next steps:"
Write-Log "  1. Add your API keys (Claude or Antigravity) to config\.env"
Write-Log "  2. Place your voice reference as voice_refs\reference.wav"
Write-Log "  3. Run: .\scripts\run.ps1 -VideoPath inbox\your_video.mp4"
