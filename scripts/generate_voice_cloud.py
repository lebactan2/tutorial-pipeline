"""
Stage 3b: Generate narration using MiniMax Audio TTS API (cloud).
Same input/output contract as local version.
Skips gracefully if MINIMAX_API_KEY is not set.
"""

import argparse
import json
import logging
import os
import sys
import time
import wave
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
WORKING_DIR = PROJECT_ROOT / "working"
LOG_DIR = PROJECT_ROOT / "logs"

# MiniMax API endpoints (Western US region)
MINIMAX_BASE_URL = "https://api-uw.minimax.io/v1"
MINIMAX_UPLOAD_URL = f"{MINIMAX_BASE_URL}/files/upload"
MINIMAX_CLONE_URL = f"{MINIMAX_BASE_URL}/voice_clone"
MINIMAX_TTS_URL = f"{MINIMAX_BASE_URL}/t2a_v2"

def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "generate_voice_cloud.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

def upload_voice_reference(api_key: str, voice_ref_path: str, logger: logging.Logger) -> str:
    """Upload voice reference to MiniMax and return file_id."""
    logger.info(f"Uploading voice reference to MiniMax: {voice_ref_path}")

    headers = {"Authorization": f"Bearer {api_key}"}

    with open(voice_ref_path, "rb") as f:
        files = {"file": (os.path.basename(voice_ref_path), f, "audio/wav")}
        data = {"purpose": "voice_clone"}
        resp = requests.post(MINIMAX_UPLOAD_URL, headers=headers, files=files, data=data, timeout=60)

    resp.raise_for_status()
    result = resp.json()

    file_id = result.get("file", {}).get("file_id") or result.get("file_id")
    if not file_id:
        raise ValueError(f"No file_id in upload response: {result}")

    logger.info(f"Voice reference uploaded, file_id: {file_id}")
    return file_id

def clone_voice(api_key: str, file_id: str, voice_id: str, logger: logging.Logger) -> str:
    """Create a cloned voice from the uploaded reference."""
    logger.info(f"Creating voice clone: {voice_id}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "file_id": file_id,
        "voice_id": voice_id,
        "need_noise_reduction": True,
        "need_volumn_normalization": True,
    }

    resp = requests.post(MINIMAX_CLONE_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()

    logger.info(f"Voice clone created: {voice_id}")
    return voice_id

def synthesize_segment(api_key: str, text: str, voice_id: str, lang: str, logger: logging.Logger) -> bytes:
    """Synthesize a single text segment using MiniMax TTS."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Map language codes to MiniMax language hints
    lang_map = {"en": "English", "vi": "Vietnamese"}

    payload = {
        "model": "speech-2.6-hd",
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
        },
        "audio_setting": {
            "sample_rate": 24000,
            "format": "wav",
        },
    }

    resp = requests.post(MINIMAX_TTS_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    result = resp.json()

    # Get audio URL from response
    audio_url = result.get("audio_file", {}).get("url") or result.get("data", {}).get("audio", {}).get("url")

    if audio_url:
        # Download the audio file
        audio_resp = requests.get(audio_url, timeout=60)
        audio_resp.raise_for_status()
        return audio_resp.content
    elif "audio" in result:
        # Some responses include base64 audio directly
        import base64
        return base64.b64decode(result["audio"])
    else:
        raise ValueError(f"No audio in TTS response: {list(result.keys())}")

def concatenate_wavs(wav_paths: list, output_path: str, gap_seconds: float = 0.3):
    """Concatenate multiple WAV files with gaps. Returns timing list."""
    if not wav_paths:
        return []

    timings = []
    current_time = 0.0

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

def generate_voice_cloud(script_path: str, voice_ref_path: str, logger: logging.Logger) -> tuple:
    """Generate narration using MiniMax TTS. Returns (narration_path, timing_path) or (None, None)."""

    load_dotenv(CONFIG_DIR / ".env")
    api_key = os.getenv("MINIMAX_API_KEY")

    if not api_key:
        logger.info("MINIMAX_API_KEY not set — skipping cloud voice generation")
        return None, None

    basename = Path(script_path).stem.replace(".script", "")

    # Load script
    with open(script_path, "r", encoding="utf-8") as f:
        script_data = json.load(f)

    cleaned_script = script_data.get("cleaned_script", [])
    if not cleaned_script:
        logger.error("No segments in cleaned_script")
        return None, None

    # Upload voice reference and create clone
    try:
        file_id = upload_voice_reference(api_key, voice_ref_path, logger)
        voice_id = f"tutorial_voice_{int(time.time())}"
        clone_voice(api_key, file_id, voice_id, logger)
    except Exception as e:
        logger.error(f"Voice cloning failed: {e}")
        logger.info("Cloud voice generation aborted")
        return None, None

    # Generate each segment
    WORKING_DIR.mkdir(exist_ok=True)
    segment_wavs = []
    segment_texts = []

    for i, entry in enumerate(cleaned_script):
        text = entry["text"]
        lang = entry.get("lang", "en")

        logger.info(f"  Segment {i+1}/{len(cleaned_script)} [{lang}]: {text[:60]}...")

        seg_path = str(WORKING_DIR / f"{basename}_cloud_seg_{i:04d}.wav")

        try:
            start_time = time.time()
            audio_bytes = synthesize_segment(api_key, text, voice_id, lang, logger)

            with open(seg_path, "wb") as f:
                f.write(audio_bytes)

            elapsed = time.time() - start_time
            logger.info(f"    Generated in {elapsed:.1f}s")

            segment_wavs.append(seg_path)
            segment_texts.append({"lang": lang, "text": text})

        except Exception as e:
            logger.error(f"    Segment {i+1} failed: {e}")
            # Create silence placeholder
            sample_rate = 24000
            with wave.open(seg_path, "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(b"\x00" * (sample_rate * 2))
            segment_wavs.append(seg_path)
            segment_texts.append({"lang": lang, "text": f"[FAILED: {text[:40]}]"})

    # Concatenate
    narration_path = str(WORKING_DIR / f"{basename}.narration_cloud.wav")
    logger.info(f"Concatenating {len(segment_wavs)} segments...")
    timings = concatenate_wavs(segment_wavs, narration_path)

    # Timing JSON
    timing_data = []
    for i, (timing, text_entry) in enumerate(zip(timings, segment_texts)):
        timing_data.append({
            "index": i + 1,
            "start": timing["start"],
            "end": timing["end"],
            "lang": text_entry["lang"],
            "text": text_entry["text"],
        })

    timing_path = str(WORKING_DIR / f"{basename}.narration_cloud_timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing_data, f, ensure_ascii=False, indent=2)

    # Cleanup
    for seg_path in segment_wavs:
        try:
            os.remove(seg_path)
        except OSError:
            pass

    total_duration = timings[-1]["end"] if timings else 0
    logger.info(f"Cloud narration complete: {total_duration:.1f}s total")

    return narration_path, timing_path

def main():
    parser = argparse.ArgumentParser(description="Generate narration via MiniMax TTS (cloud)")
    parser.add_argument("script_path", help="Path to cleaned script JSON")
    parser.add_argument("voice_ref_path", help="Path to voice reference WAV")
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

    logger.info("=== Stage 3b: Cloud Voice Generation (MiniMax) ===")

    narration_path, timing_path = generate_voice_cloud(str(script_path), str(voice_ref_path), logger)

    if narration_path:
        logger.info(f"Narration: {narration_path}")
        logger.info(f"Timing: {timing_path}")
        print(f"{narration_path}|{timing_path}")
    else:
        logger.info("Cloud voice generation was skipped or failed")
        print("SKIPPED")

if __name__ == "__main__":
    main()
