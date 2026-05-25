"""
Stage 3a: Generate narration using VieNeu-TTS (local voice cloning).
Produces narration WAV + per-segment timing JSON for SRT generation.
"""

import argparse
import json
import logging
import os
import sys
import time
import wave
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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

def generate_voice(script_path: str, voice_ref_path: str, logger: logging.Logger) -> tuple:
    """Generate narration from cleaned script using VieNeu-TTS.
    Returns (narration_wav_path, timing_json)."""

    from vieneu import Vieneu

    config = load_system_config()
    vieneu_cfg = config.get("vieneu", {})
    mode = vieneu_cfg.get("mode", "turbo")

    logger.info(f"Initializing VieNeu-TTS (mode: {mode})")

    try:
        tts = Vieneu()
        logger.info("VieNeu-TTS initialized")
    except Exception as e:
        logger.error(f"Failed to initialize VieNeu-TTS: {e}")
        raise

    # Encode voice reference
    logger.info(f"Encoding voice reference: {voice_ref_path}")
    try:
        voice = tts.encode_reference(voice_ref_path)
        logger.info("Voice reference encoded successfully")
    except Exception as e:
        logger.error(f"Failed to encode voice reference: {e}")
        logger.info("Falling back to default voice")
        voice = None

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

    for i, entry in enumerate(cleaned_script):
        text = entry["text"]
        lang = entry.get("lang", "en")

        logger.info(f"  Segment {i+1}/{len(cleaned_script)} [{lang}]: {text[:60]}...")

        seg_path = str(WORKING_DIR / f"{basename}_seg_{i:04d}.wav")

        try:
            start_time = time.time()
            audio = tts.infer(text=text, voice=voice)
            tts.save(audio, seg_path)
            elapsed = time.time() - start_time
            logger.info(f"    Generated in {elapsed:.1f}s")

            segment_wavs.append(seg_path)
            segment_texts.append({"lang": lang, "text": text})

        except Exception as e:
            logger.error(f"    Failed to generate segment {i+1}: {e}")
            # Create a short silence as placeholder
            logger.info("    Inserting 1s silence as placeholder")
            sample_rate = 24000
            silence_frames = sample_rate  # 1 second
            with wave.open(seg_path, "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(b"\x00" * (silence_frames * 2))
            segment_wavs.append(seg_path)
            segment_texts.append({"lang": lang, "text": f"[FAILED: {text[:40]}]"})

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
        })

    timing_path = str(WORKING_DIR / f"{basename}.narration_timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing_data, f, ensure_ascii=False, indent=2)

    # Cleanup segment files
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
    args = parser.parse_args()

    logger = setup_logging()

    script_path = Path(args.script_path).resolve()
    voice_ref_path = Path(args.voice_ref_path).resolve()

    if not script_path.exists():
        logger.error(f"Script not found: {script_path}")
        sys.exit(1)
    if not voice_ref_path.exists():
        logger.error(f"Voice reference not found: {voice_ref_path}")
        sys.exit(1)

    logger.info("=== Stage 3a: Local Voice Generation (VieNeu-TTS) ===")
    logger.info(f"Script: {script_path}")
    logger.info(f"Voice ref: {voice_ref_path}")

    narration_path, timing_path = generate_voice(str(script_path), str(voice_ref_path), logger)

    logger.info(f"Narration: {narration_path}")
    logger.info(f"Timing: {timing_path}")
    print(f"{narration_path}|{timing_path}")

if __name__ == "__main__":
    main()
