<#
.SYNOPSIS
    Tutorial Pipeline Orchestrator
.DESCRIPTION
    Runs the full bilingual tutorial video pipeline:
    1. Transcribe -> 2. Clean Script -> 3a. Local Voice -> 3b. Cloud Voice -> 5. Assemble
.PARAMETER VideoPath
    Path to the raw video file (e.g., inbox\my_tutorial.mp4)
.PARAMETER VoiceRef
    Path to voice reference WAV (default: voice_refs\reference.wav)
.PARAMETER SkipCloud
    Skip cloud voice generation even if API key is present
.EXAMPLE
    .\scripts\run.ps1 -VideoPath "inbox\my_tutorial.mp4"
    .\scripts\run.ps1 -VideoPath "C:\Users\me\Desktop\recording.mp4" -SkipCloud
#>

param(
    [string]$VideoPath = "",

    [string]$VoiceRef = "",

    [switch]$SkipCloud,
    
    [string]$YoutubeUrl = "",

    [string]$TranscriptPath = "",

    [switch]$SkipTranscribe,
    
    [switch]$ForceTranscribe,

    [string]$LLMProvider = ""
)

if (-not $VideoPath -and -not $YoutubeUrl) {
    Write-Host "You must provide either -VideoPath or -YoutubeUrl" -ForegroundColor Red
    exit 1
}

$ErrorActionPreference = "Stop"
$PipelineRoot = "C:\tutorial-pipeline"
$ScriptsDir = Join-Path $PipelineRoot "scripts"
$InboxDir = Join-Path $PipelineRoot "inbox"
$WorkingDir = Join-Path $PipelineRoot "working"
$OutputDir = Join-Path $PipelineRoot "output"
$LogDir = Join-Path $PipelineRoot "logs"
$ConfigDir = Join-Path $PipelineRoot "config"

# Default voice reference
if (-not $VoiceRef) {
    $VoiceRef = Join-Path $PipelineRoot "voice_refs\reference.wav"
}

# -------------------------------------------------
# Utilities
# -------------------------------------------------
$PipelineLog = Join-Path $LogDir "pipeline.log"

function Write-Pipeline {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $PipelineLog -Value $line
}

function Get-ElapsedString {
    param([TimeSpan]$Elapsed)
    if ($Elapsed.TotalMinutes -ge 1) {
        return "{0:N1} min" -f $Elapsed.TotalMinutes
    }
    return "{0:N1}s" -f $Elapsed.TotalSeconds
}

# -------------------------------------------------
# Pre-flight checks
# -------------------------------------------------
$pipelineStart = Get-Date

Write-Pipeline "============================================"
Write-Pipeline "  Tutorial Pipeline"
Write-Pipeline "============================================"
Write-Pipeline ""

# Ensure directories exist
foreach ($dir in @($InboxDir, $WorkingDir, $OutputDir, $LogDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

# Resolve video path
if ($YoutubeUrl) {
    # If downloading, make sure to use venv python
    $venvActivate = Join-Path $PipelineRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $venvActivate) { . $venvActivate }

    Write-Pipeline "Downloading from YouTube: $YoutubeUrl"
    $dlScript = Join-Path $ScriptsDir "download_youtube.py"
    $result = & python $dlScript $YoutubeUrl 2>&1
    $downloadOutput = ($result | Select-Object -Last 1).ToString().Trim()
    if (Test-Path $downloadOutput) {
        $VideoPath = $downloadOutput
        Write-Pipeline "Downloaded video to: $VideoPath"
    } else {
        Write-Pipeline "Failed to download from YouTube. Output was: $result" "ERROR"
        exit 1
    }
} else {
    $VideoPath = (Resolve-Path $VideoPath -ErrorAction Stop).Path
    if (-not (Test-Path $VideoPath)) {
        Write-Pipeline "Video not found: $VideoPath" "ERROR"
        exit 1
    }
    
    # Copy to inbox if not already there
    $inboxCopy = Join-Path $InboxDir ([System.IO.Path]::GetFileName($VideoPath))
    if ($VideoPath -ne $inboxCopy) {
        if (-not (Test-Path $inboxCopy)) {
            Write-Pipeline "Copying video to inbox..."
            Copy-Item $VideoPath $inboxCopy
        }
    }
}

$basename = [System.IO.Path]::GetFileNameWithoutExtension($VideoPath)
Write-Pipeline "Input video: $VideoPath"
Write-Pipeline "Basename: $basename"

# Check voice reference
if (-not (Test-Path $VoiceRef)) {
    Write-Pipeline "Voice reference not found: $VoiceRef" "ERROR"
    Write-Pipeline "Record a 30-60s reference and save as voice_refs\reference.wav" "ERROR"
    exit 1
}
Write-Pipeline "Voice reference: $VoiceRef"

# Activate venv
$venvActivate = Join-Path $PipelineRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
    Write-Pipeline "Virtual environment activated"
} else {
    Write-Pipeline "No venv found at $venvActivate -- using system Python" "WARN"
}

# -------------------------------------------------
# Stage 1: Transcribe (or Import SRT/JSON)
# -------------------------------------------------
Write-Pipeline ""
if ($SkipTranscribe) {
    Write-Pipeline ">>> Stage 1: Skipped (Using existing transcript)"
    $transcriptPath = Join-Path $WorkingDir "$basename.transcript.json"
    if (-not (Test-Path $transcriptPath)) {
        Write-Pipeline "SkipTranscribe requested but no existing transcript found at $transcriptPath" "ERROR"
        exit 1
    }
} elseif ($TranscriptPath -and (Test-Path $TranscriptPath)) {
    Write-Pipeline ">>> Stage 1: Import Custom Transcript ($TranscriptPath)"
    $stageStart = Get-Date

    $transcriptPathOutput = Join-Path $WorkingDir "$basename.transcript.json"
    
    if ($TranscriptPath.EndsWith(".json")) {
        Write-Pipeline "Copying JSON transcript..."
        Copy-Item $TranscriptPath $transcriptPathOutput -Force
    } else {
        $importScript = Join-Path $ScriptsDir "import_srt.py"
        $result = @()
        & python $importScript $TranscriptPath --output $transcriptPathOutput 2>&1 | Tee-Object -Variable result
        $transcriptPathFinal = ($result | Select-Object -Last 1).ToString().Trim()

        if (-not (Test-Path $transcriptPathOutput)) {
            if (Test-Path $transcriptPathFinal) {
                $transcriptPathOutput = $transcriptPathFinal
            } else {
                Write-Pipeline "Importing SRT failed." "ERROR"
                exit 1
            }
        }
    }
    $transcriptPath = $transcriptPathOutput
    
    $stageElapsed = (Get-Date) - $stageStart
    Write-Pipeline "Stage 1 (Import) complete: $(Get-ElapsedString $stageElapsed)"
    Write-Pipeline "Transcript: $transcriptPath"
} else {
    Write-Pipeline ">>> Stage 1: Transcribe"
    $stageStart = Get-Date

    $transcriptPath = Join-Path $WorkingDir "$basename.transcript.json"
    
    if ((Test-Path $transcriptPath) -and (-not $ForceTranscribe)) {
        Write-Pipeline "Found existing transcript at $transcriptPath. Skipping transcription."
    } else {
        if (Test-Path $transcriptPath) {
            Write-Pipeline "Overwriting existing transcript..."
        }
        
        $transcribeScript = Join-Path $ScriptsDir "transcribe.py"

        $result = @()
        & python $transcribeScript $VideoPath 2>&1 | Tee-Object -Variable result
        $transcriptOutput = ($result | Select-Object -Last 1).ToString().Trim()

        if (-not (Test-Path $transcriptPath)) {
            # Try the path from stdout
            if (Test-Path $transcriptOutput) {
                $transcriptPath = $transcriptOutput
            } else {
                Write-Pipeline "Transcription failed. Check logs\transcribe.log" "ERROR"
                exit 1
            }
        }
    }

    $stageElapsed = (Get-Date) - $stageStart
    Write-Pipeline "Stage 1 complete: $(Get-ElapsedString $stageElapsed)"
    Write-Pipeline "Transcript: $transcriptPath"
}

# -------------------------------------------------
# Stage 2: Clean Script
# -------------------------------------------------
Write-Pipeline ""
Write-Pipeline ">>> Stage 2: Clean Script (Claude API)"
$stageStart = Get-Date

$scriptPath = Join-Path $WorkingDir "$basename.script.json"
$cleanScript = Join-Path $ScriptsDir "clean_script.py"
$cleanCmd = "python `"$cleanScript`" `"$transcriptPath`""
if ($LLMProvider) {
    $cleanCmd += " --provider $LLMProvider"
}

$result = @()
Invoke-Expression "$cleanCmd 2>&1" | Tee-Object -Variable result
$scriptOutput = ($result | Select-Object -Last 1).ToString().Trim()

if (-not (Test-Path $scriptPath)) {
    if (Test-Path $scriptOutput) {
        $scriptPath = $scriptOutput
    } else {
        Write-Pipeline "Script cleanup failed. Check logs\clean_script.log" "ERROR"
        exit 1
    }
}

$stageElapsed = (Get-Date) - $stageStart
Write-Pipeline "Stage 2 complete: $(Get-ElapsedString $stageElapsed)"
Write-Pipeline "Script: $scriptPath"

# -------------------------------------------------
# Stage 3a: Local Voice (VieNeu-TTS)
# -------------------------------------------------
Write-Pipeline ""
Write-Pipeline ">>> Stage 3a: Local Voice Generation (VieNeu-TTS)"
$stageStart = Get-Date

$localVoiceScript = Join-Path $ScriptsDir "generate_voice_local.py"

$result = @()
& python $localVoiceScript $scriptPath $VoiceRef 2>&1 | Tee-Object -Variable result
$voiceOutput = ($result | Select-Object -Last 1).ToString().Trim()

$localNarration = ""
$localTiming = ""

if ($voiceOutput -match "\|") {
    $parts = $voiceOutput -split "\|"
    $localNarration = $parts[0]
    $localTiming = $parts[1]
} else {
    # Try default paths
    $localNarration = Join-Path $WorkingDir "$basename.narration_local.wav"
    $localTiming = Join-Path $WorkingDir "$basename.narration_timing.json"
}

if (-not (Test-Path $localNarration)) {
    Write-Pipeline "Local voice generation failed. Check logs\generate_voice_local.log" "ERROR"
    exit 1
}

$stageElapsed = (Get-Date) - $stageStart
Write-Pipeline "Stage 3a complete: $(Get-ElapsedString $stageElapsed)"

# -------------------------------------------------
# Stage 3b: Cloud Voice (MiniMax) -- optional
# -------------------------------------------------
$cloudNarration = ""
$cloudTiming = ""

if (-not $SkipCloud) {
    Write-Pipeline ""
    Write-Pipeline ">>> Stage 3b: Cloud Voice Generation (MiniMax)"
    $stageStart = Get-Date

    $cloudVoiceScript = Join-Path $ScriptsDir "generate_voice_cloud.py"

    $result = @()
    & python $cloudVoiceScript $scriptPath $VoiceRef 2>&1 | Tee-Object -Variable result
    $cloudOutput = ($result | Select-Object -Last 1).ToString().Trim()

    if ($cloudOutput -eq "SKIPPED") {
        Write-Pipeline "Cloud voice skipped (no MINIMAX_API_KEY)"
    } elseif ($cloudOutput -match "\|") {
        $parts = $cloudOutput -split "\|"
        $cloudNarration = $parts[0]
        $cloudTiming = $parts[1]
        $stageElapsed = (Get-Date) - $stageStart
        Write-Pipeline "Stage 3b complete: $(Get-ElapsedString $stageElapsed)"
    } else {
        Write-Pipeline "Cloud voice generation failed or skipped" "WARN"
    }
} else {
    Write-Pipeline ""
    Write-Pipeline ">>> Stage 3b: Skipped (--SkipCloud flag)"
}

# -------------------------------------------------
# Stage 5: Assemble -- Local version
# -------------------------------------------------
Write-Pipeline ""
Write-Pipeline ">>> Stage 5a: Assemble (local voice)"
$stageStart = Get-Date

$assembleScript = Join-Path $ScriptsDir "assemble.py"

& python $assembleScript $VideoPath $scriptPath $localNarration $localTiming --tag "local" 2>&1

$localDraft = Join-Path $OutputDir "${basename}_draft_local.mp4"
$srtFile = Join-Path $OutputDir "$basename.srt"

$stageElapsed = (Get-Date) - $stageStart
Write-Pipeline "Stage 5a complete: $(Get-ElapsedString $stageElapsed)"

# -------------------------------------------------
# Stage 5: Assemble -- Cloud version (if available)
# -------------------------------------------------
$cloudDraft = ""
if ($cloudNarration -and (Test-Path $cloudNarration)) {
    Write-Pipeline ""
    Write-Pipeline ">>> Stage 5b: Assemble (cloud voice)"
    $stageStart = Get-Date

    & python $assembleScript $VideoPath $scriptPath $cloudNarration $cloudTiming --tag "cloud" 2>&1

    $cloudDraft = Join-Path $OutputDir "${basename}_draft_cloud.mp4"
    $stageElapsed = (Get-Date) - $stageStart
    Write-Pipeline "Stage 5b complete: $(Get-ElapsedString $stageElapsed)"
}

# -------------------------------------------------
# Summary
# -------------------------------------------------
$totalElapsed = (Get-Date) - $pipelineStart

Write-Pipeline ""
Write-Pipeline "============================================"
Write-Pipeline "  Pipeline Complete!"
Write-Pipeline "============================================"
Write-Pipeline "Total time: $(Get-ElapsedString $totalElapsed)"
Write-Pipeline ""
Write-Pipeline "Outputs:"

if (Test-Path $localDraft) {
    Write-Pipeline "  Local draft:  $localDraft"
} else {
    Write-Pipeline "  Local draft:  FAILED" "WARN"
}

if ($cloudDraft -and (Test-Path $cloudDraft)) {
    Write-Pipeline "  Cloud draft:  $cloudDraft"
} else {
    Write-Pipeline "  Cloud draft:  not produced"
}

if (Test-Path $srtFile) {
    Write-Pipeline "  Subtitles:    $srtFile"
}

Write-Pipeline ""
Write-Pipeline "Open the output folder to review your drafts."
Write-Pipeline "Then open your preferred draft in CapCut for final polish."

# Open output folder in Explorer
if (Test-Path $OutputDir) {
    Start-Process explorer.exe $OutputDir
}
