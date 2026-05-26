"""
Stage 3a: Generate narration using VieNeu-TTS (local voice cloning).
Produces narration WAV + per-segment timing JSON for SRT generation.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Prevent local "vieneu" directory from shadowing the installed library
sys.path.insert(0, str(PROJECT_ROOT / "vieneu" / "src"))

CONFIG_DIR = PROJECT_ROOT / "config"
WORKING_DIR = PROJECT_ROOT / "working"
LOG_DIR = PROJECT_ROOT / "logs"

def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "generate_voice_local.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

def load_system_config():
    config_path = CONFIG_DIR / "system.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}

def get_wav_duration(wav_path: str) -> float:
    """Get duration of a WAV file in seconds."""
    with wave.open(wav_path, "r") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate)

def create_silence_wav(output_path: str, seconds: float = 1.0, sample_rate: int = 24000):
    """Create a short PCM silence WAV as a last-resort placeholder."""
    frame_count = int(sample_rate * seconds)
    with wave.open(output_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00" * (frame_count * 2))

def synthesize_windows_tts(text: str, output_path: str, logger: logging.Logger):
    """Use built-in Windows SAPI TTS when VieNeu is not installed.

    This does not clone the reference voice, but it keeps the pipeline runnable.
    """
    if os.name != "nt":
        raise RuntimeError("Windows TTS fallback is only available on Windows")

    WORKING_DIR.mkdir(exist_ok=True)
    output = Path(output_path)
    text_path = output.with_suffix(".txt")
    ps_path = WORKING_DIR / "_windows_tts.ps1"

    text_path.write_text(text or " ", encoding="utf-8")
    ps_path.write_text(
        """param(
    [Parameter(Mandatory=$true)][string]$TextPath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.Rate = 0
    $synth.Volume = 100
    $synth.SetOutputToWaveFile($OutputPath)
    $text = Get-Content -LiteralPath $TextPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($text)) { $text = " " }
    $synth.Speak($text)
}
finally {
    $synth.Dispose()
}
""",
        encoding="utf-8",
    )

    timeout = max(60, min(300, 30 + len(text or "") // 8))
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps_path),
            str(text_path),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    try:
        text_path.unlink()
    except OSError:
        pass

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Windows TTS failed: {detail}")
    if not output.exists():
        raise RuntimeError("Windows TTS did not create an output WAV")

def initialize_vieneu(mode: str, logger: logging.Logger):
    """Return a VieNeu instance, or None if the optional engine is unavailable."""
    try:
        from vieneu import Vieneu
    except Exception as e:
        logger.warning(f"VieNeu-TTS is not importable: {e}")
        return None

    try:
        tts = Vieneu(mode=mode)
        logger.info(f"VieNeu-TTS ({mode}) initialized")
        return tts
    except Exception as e:
        logger.warning(f"Failed to initialize VieNeu-TTS ({mode}): {e}")
        return None

def concatenate_wavs(wav_paths: list, output_path: str, gap_seconds: float = 0.3):
    """Concatenate multiple WAV files with short gaps between them.
    Returns list of (start_time, end_time) for each segment."""
    if not wav_paths:
        return []

    timings = []
    current_time = 0.0

    # Read first file to get parameters
    with wave.open(wav_paths[0], "r") as first:
        params = first.getparams()
        sample_rate = params.framerate
        sample_width = params.sampwidth
        n_channels = params.nchannels

    gap_frames = int(gap_seconds * sample_rate)
    silence = b"\x00" * (gap_frames * sample_width * n_channels)

    with wave.open(output_path, "w") as out:
        out.setparams(params)

        for i, wav_path in enumerate(wav_paths):
            # Add gap before segment (except first)
            if i > 0:
                out.writeframes(silence)
                current_time += gap_seconds

            seg_start = current_time

            with wave.open(wav_path, "r") as seg:
                frames = seg.readframes(seg.getnframes())
                out.writeframes(frames)
                seg_duration = seg.getnframes() / float(seg.getframerate())

            current_time += seg_duration
            timings.append({"start": round(seg_start, 3), "end": round(current_time, 3)})

    return timings

def generate_voice(
    script_path: str,
    voice_ref_path: str,
    logger: logging.Logger,
    require_clone: bool = False,
    keep_segments: bool = False,
) -> tuple:
    """Generate narration from cleaned script using VieNeu-TTS.
    Returns (narration_wav_path, timing_json)."""

    config = load_system_config()
    vieneu_cfg = config.get("vieneu", {})
    mode = vieneu_cfg.get("mode", "turbo")

    logger.info(f"Initializing VieNeu-TTS (mode: {mode})")
    tts = initialize_vieneu(mode, logger)

    if tts is None:
        if require_clone:
            raise RuntimeError("Voice cloning is required, but VieNeu-TTS could not be initialized.")
        logger.warning("Using Windows TTS fallback. Voice cloning is disabled for this run.")

    # Encode voice reference
    voice = None
    if tts is not None:
        if Path(voice_ref_path).exists():
            logger.info(f"Encoding voice reference: {voice_ref_path}")
            try:
                voice = tts.encode_reference(voice_ref_path)
                logger.info("Voice reference encoded successfully")
            except Exception as e:
                if require_clone:
                    raise RuntimeError(f"Voice cloning is required, but the reference could not be encoded: {e}") from e
                logger.warning(f"Failed to encode voice reference: {e}")
                logger.info("Falling back to default VieNeu voice")
        else:
            if require_clone:
                raise FileNotFoundError(f"Voice cloning is required, but voice reference was not found: {voice_ref_path}")
            logger.warning(f"Voice reference not found: {voice_ref_path}")
            logger.info("Using default VieNeu voice")

    # Load cleaned script
    with open(script_path, "r", encoding="utf-8") as f:
        script_data = json.load(f)

    cleaned_script = script_data.get("cleaned_script", [])
    if not cleaned_script:
        logger.error("No segments in cleaned_script")
        sys.exit(1)

    # Generate audio for each segment
    WORKING_DIR.mkdir(exist_ok=True)
    basename = Path(script_path).stem.replace(".script", "")
    segment_wavs = []
    segment_texts = []
    segment_dir = WORKING_DIR / f"{basename}_local_segments"
    if keep_segments:
        segment_dir.mkdir(parents=True, exist_ok=True)

    for i, entry in enumerate(cleaned_script):
        text = entry["text"]
        lang = entry.get("lang", "en")

        logger.info(f"  Segment {i+1}/{len(cleaned_script)} [{lang}]: {text[:60]}...")

        if keep_segments:
            seg_path = str(segment_dir / f"seg_{i:04d}.wav")
        else:
            seg_path = str(WORKING_DIR / f"{basename}_seg_{i:04d}.wav")

        try:
            start_time = time.time()
            if tts is not None:
                audio = tts.infer(text=text, voice=voice)
                tts.save(audio, seg_path)
            else:
                synthesize_windows_tts(text, seg_path, logger)
            elapsed = time.time() - start_time
            logger.info(f"    Generated in {elapsed:.1f}s")

            segment_wavs.append(seg_path)
            segment_texts.append({
                "lang": lang,
                "text": text,
                "source_start": entry.get("start"),
                "source_end": entry.get("end"),
                "segment_file": seg_path,
            })

        except Exception as e:
            if require_clone:
                raise RuntimeError(
                    f"Voice cloning is required, but cloned generation failed for segment {i+1}: {e}"
                ) from e
            logger.warning(f"    Primary local voice failed for segment {i+1}: {e}")
            if tts is not None:
                try:
                    logger.info("    Trying Windows TTS fallback")
                    synthesize_windows_tts(text, seg_path, logger)
                    segment_wavs.append(seg_path)
                    segment_texts.append({
                        "lang": lang,
                        "text": text,
                        "source_start": entry.get("start"),
                        "source_end": entry.get("end"),
                        "segment_file": seg_path,
                    })
                    continue
                except Exception as fallback_error:
                    logger.error(f"    Windows TTS fallback failed: {fallback_error}")

            logger.info("    Inserting 1s silence as placeholder")
            create_silence_wav(seg_path)
            segment_wavs.append(seg_path)
            segment_texts.append({
                "lang": lang,
                "text": f"[FAILED: {text[:40]}]",
                "source_start": entry.get("start"),
                "source_end": entry.get("end"),
                "segment_file": seg_path,
            })

    # Concatenate all segments
    narration_path = str(WORKING_DIR / f"{basename}.narration_local.wav")
    logger.info(f"Concatenating {len(segment_wavs)} segments...")
    timings = concatenate_wavs(segment_wavs, narration_path)

    # Build timing JSON (for SRT generation)
    timing_data = []
    for i, (timing, text_entry) in enumerate(zip(timings, segment_texts)):
        timing_data.append({
            "index": i + 1,
            "start": timing["start"],
            "end": timing["end"],
            "lang": text_entry["lang"],
            "text": text_entry["text"],
            "source_start": text_entry.get("source_start"),
            "source_end": text_entry.get("source_end"),
            "segment_file": text_entry.get("segment_file"),
            "audio_duration": round(timing["end"] - timing["start"], 3),
        })

    timing_path = str(WORKING_DIR / f"{basename}.narration_timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing_data, f, ensure_ascii=False, indent=2)

    # Cleanup segment files
    if not keep_segments:
        for seg_path in segment_wavs:
            try:
                os.remove(seg_path)
            except OSError:
                pass

    total_duration = timings[-1]["end"] if timings else 0
    logger.info(f"Narration complete: {total_duration:.1f}s total, {len(timings)} segments")

    return narration_path, timing_path

def main():
    parser = argparse.ArgumentParser(description="Generate narration via VieNeu-TTS (local)")
    parser.add_argument("script_path", help="Path to cleaned script JSON")
    parser.add_argument("voice_ref_path", help="Path to voice reference WAV")
    parser.add_argument("--output", help="Override narration output path")
    parser.add_argument(
        "--require-clone",
        action="store_true",
        help="Fail instead of falling back when the reference voice cannot be cloned",
    )
    parser.add_argument(
        "--keep-segments",
        action="store_true",
        help="Keep per-segment WAV files and record them in timing JSON for aligned assembly",
    )
    args = parser.parse_args()

    logger = setup_logging()

    script_path = Path(args.script_path).resolve()
    voice_ref_path = Path(args.voice_ref_path).resolve()

    if not script_path.exists():
        logger.error(f"Script not found: {script_path}")
        sys.exit(1)
    logger.info("=== Stage 3a: Local Voice Generation ===")
    logger.info(f"Script: {script_path}")
    logger.info(f"Voice ref: {voice_ref_path}")
    if not voice_ref_path.exists():
        logger.warning("Voice reference not found. Continuing with default/fallback voice.")

    narration_path, timing_path = generate_voice(
        str(script_path),
        str(voice_ref_path),
        logger,
        require_clone=args.require_clone,
        keep_segments=args.keep_segments,
    )

    logger.info(f"Narration: {narration_path}")
    logger.info(f"Timing: {timing_path}")
    print(f"{narration_path}|{timing_path}")

if __name__ == "__main__":
    main()
