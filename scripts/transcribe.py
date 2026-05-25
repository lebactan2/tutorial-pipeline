"""
Stage 1: Transcribe video to word-level JSON transcript.
Uses faster-whisper with auto language detection for EN+VN code-switching.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Project root is one level up from scripts/
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
            logging.FileHandler(LOG_DIR / "transcribe.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

def load_system_config():
    config_path = CONFIG_DIR / "system.json"
    if not config_path.exists():
        return {
            "whisper": {
                "model_size": "medium",
                "device": "cpu",
                "compute_type": "int8",
            }
        }
    with open(config_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)

def transcribe(video_path: str, logger: logging.Logger) -> dict:
    from faster_whisper import WhisperModel

    config = load_system_config()
    whisper_cfg = config.get("whisper", {})
    model_size = whisper_cfg.get("model_size", "medium")
    device = whisper_cfg.get("device", "cpu")
    compute_type = whisper_cfg.get("compute_type", "int8")

    logger.info(f"Loading Whisper model: {model_size} on {device} ({compute_type})")

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        if "large" in model_size:
            logger.warning(f"Failed to load {model_size}: {e}. Falling back to 'medium'")
            model = WhisperModel("medium", device="cpu", compute_type="int8")
        else:
            raise

    logger.info(f"Transcribing: {video_path}")
    start_time = time.time()

    # language=None enables auto-detection per segment (critical for code-switching)
    segments_iter, info = model.transcribe(
        video_path,
        language=None,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
            speech_pad_ms=200,
        ),
    )

    logger.info(f"Detected language: {info.language} (probability: {info.language_probability:.2f})")

    segments = []
    for segment in segments_iter:
        words = []
        if segment.words:
            for w in segment.words:
                words.append({
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": w.word.strip(),
                })

        # Detect language per segment using the segment's own language info if available
        seg_lang = getattr(segment, "language", None) or info.language or "en"
        # Normalize to two-letter code
        if seg_lang.startswith("vi"):
            seg_lang = "vi"
        else:
            seg_lang = "en"

        segments.append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
            "lang": seg_lang,
            "words": words,
        })

        logger.info(f"  [{segment.start:.1f}s - {segment.end:.1f}s] [{seg_lang}] {segment.text.strip()[:80]}")

    elapsed = time.time() - start_time
    logger.info(f"Transcription complete: {len(segments)} segments in {elapsed:.1f}s")

    return {"segments": segments}

def main():
    parser = argparse.ArgumentParser(description="Transcribe video to word-level JSON")
    parser.add_argument("video_path", help="Path to the input video file")
    parser.add_argument("--output", help="Override output path (default: working/<basename>.transcript.json)")
    args = parser.parse_args()

    logger = setup_logging()

    video_path = Path(args.video_path).resolve()
    if not video_path.exists():
        logger.error(f"Video not found: {video_path}")
        sys.exit(1)

    WORKING_DIR.mkdir(exist_ok=True)

    basename = video_path.stem
    output_path = Path(args.output) if args.output else WORKING_DIR / f"{basename}.transcript.json"

    logger.info(f"=== Stage 1: Transcription ===")
    logger.info(f"Input: {video_path}")
    logger.info(f"Output: {output_path}")

    result = transcribe(str(video_path), logger)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"Transcript saved to: {output_path}")
    print(str(output_path))  # Print path for pipeline chaining

if __name__ == "__main__":
    main()
