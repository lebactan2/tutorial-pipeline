# Tutorial Pipeline

A bilingual tutorial video pipeline that automatically processes videos by downloading, transcribing, cleaning transcripts, generating voice-overs, and assembling the final video.

## Prerequisites
- **OS**: Windows 10 or 11
- **Privileges**: Administrator access (required for the initial setup script to install system dependencies via `winget`)
- **Hardware**: An NVIDIA GPU with at least 4GB VRAM is highly recommended for faster Whisper transcription and local TTS generation, though it will fall back to CPU if no GPU is detected.

---

## 1. Initial Setup

To get started on a fresh machine, open PowerShell as an **Administrator** and run the setup script. This script will automatically install Python 3.11, FFmpeg, Git, and `uv`. It will also create a virtual environment, install the required Python packages, and set up the local `VieNeu-TTS` engine.

```powershell
.\setup.ps1
```

## 2. Configuration

Once the setup is complete, you need to configure your environment variables and voice reference:

1. **API Keys**: Go to the `config` folder. The setup script created a `.env` file based on `.env.template`. Open the `.env` file and add your required API keys (e.g., Anthropic Claude or Antigravity).
2. **Voice Clone Reference**: If you are using local TTS cloning, place a clean audio sample of the voice you want to clone at `voice_refs\reference.wav`.

---

## 3. How to Use

You can run the pipeline either through the Command Line or via the Web UI.

### Option A: Command Line Interface (CLI)
To run a video through the entire pipeline automatically, use the `run.ps1` script. Place your source video in the `inbox` folder, then run:

```powershell
.\scripts\run.ps1 -VideoPath inbox\your_video.mp4
```

*(Note: The pipeline also supports fetching videos automatically if you configure it to download from YouTube via `download_youtube.py`)*

### Option B: Web UI
If you prefer a graphical interface to manage your tasks, you can use the built-in Web UI.

1. Start the UI server by running:
   ```powershell
   .\start_ui.ps1
   ```
2. Open your web browser and navigate to: [http://127.0.0.1:5000](http://127.0.0.1:5000)

---

## Pipeline Overview

1. **📥 Download**: Uses `download_youtube.py` to fetch videos (or grabs local files).
2. **📝 Transcribe**: Uses Whisper (`transcribe.py`) to generate an accurate transcript of the audio.
3. **✨ Clean & Translate**: Uses LLMs (`clean_script.py`) to refine the transcript, fix errors, and translate if needed.
4. **🗣️ Generate Voice**: Uses VieNeu-TTS (`generate_voice_local.py` or cloud alternative) to synthesize a new voice-over based on your reference audio.
5. **🎬 Assemble**: Uses FFmpeg (`assemble.py`) to stitch the new audio back into the original video, outputting the final file to the `output` folder.
