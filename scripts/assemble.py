"""
Stage 5: Assemble final video using FFmpeg.
Cuts footage by keep_ranges, strips original audio, overlays narration, exports SRT.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKING_DIR = PROJECT_ROOT / "working"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"

def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "assemble.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

def run_ffmpeg(args: list, logger: logging.Logger, description: str = "") -> subprocess.CompletedProcess:
    """Run an FFmpeg command and log output."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + args
    logger.info(f"FFmpeg: {description or ' '.join(cmd[:6])}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error(f"FFmpeg failed: {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result

def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())

def get_audio_duration(audio_path: str) -> float:
    """Get audio duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())

def extract_segments(video_path: str, keep_ranges: list, work_dir: str, logger: logging.Logger) -> list:
    """Extract video segments for each keep_range. Returns list of segment file paths."""
    segment_paths = []

    for i, kr in enumerate(keep_ranges):
        start = kr["start"]
        end = kr["end"]
        seg_path = os.path.join(work_dir, f"seg_{i:04d}.mp4")

        run_ffmpeg(
            ["-ss", str(start), "-to", str(end), "-i", video_path,
             "-c", "copy", "-avoid_negative_ts", "make_zero", seg_path],
            logger,
            f"Extract segment {i+1}/{len(keep_ranges)} [{start:.1f}s-{end:.1f}s]",
        )
        segment_paths.append(seg_path)

    return segment_paths

def concatenate_segments(segment_paths: list, output_path: str, work_dir: str, logger: logging.Logger):
    """Concatenate video segments using FFmpeg concat demuxer."""
    # Create concat list file
    list_file = os.path.join(work_dir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for seg in segment_paths:
            # Escape single quotes in path for FFmpeg
            safe_path = seg.replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", list_file,
         "-c", "copy", output_path],
        logger,
        f"Concatenate {len(segment_paths)} segments",
    )

def overlay_narration(video_path: str, narration_path: str, output_path: str, logger: logging.Logger):
    """Strip original audio and attach narration."""
    video_duration = get_video_duration(video_path)
    audio_duration = get_audio_duration(narration_path)

    logger.info(f"Video duration: {video_duration:.1f}s, Narration duration: {audio_duration:.1f}s")

    if abs(audio_duration - video_duration) > 5.0:
        logger.warning(
            f"Duration mismatch: video={video_duration:.1f}s, narration={audio_duration:.1f}s. "
            f"This may indicate bad Stage 2 output. Using -shortest to handle."
        )

    if abs(audio_duration - video_duration) > 2.0:
        # Duration mismatch — stretch/squeeze video to match narration
        speed_factor = video_duration / audio_duration
        logger.warning(
            f"Duration mismatch: video={video_duration:.1f}s, narration={audio_duration:.1f}s. "
            f"Stretching/squeezing video by {speed_factor:.2f}x"
        )
        run_ffmpeg(
            ["-i", video_path, "-i", narration_path,
             "-filter_complex", f"[0:v]setpts={1/speed_factor:.4f}*PTS[v]",
             "-map", "[v]", "-map", "1:a",
             "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             "-c:a", "aac", "-b:a", "192k",
             "-shortest", output_path],
            logger,
            "Overlay narration (with video stretch)",
        )
    else:
        # Duration roughly matches
        run_ffmpeg(
            ["-i", video_path, "-i", narration_path,
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             output_path],
            logger,
            "Overlay narration",
        )

def generate_srt(timing_path: str, output_path: str, logger: logging.Logger):
    """Generate SRT subtitle file from narration timing JSON."""
    with open(timing_path, "r", encoding="utf-8") as f:
        timing_data = json.load(f)

    def format_timestamp(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for entry in timing_data:
        idx = entry["index"]
        start = format_timestamp(entry["start"])
        end = format_timestamp(entry["end"])
        text = entry["text"]

        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"SRT generated: {output_path} ({len(timing_data)} entries)")

def assemble(
    video_path: str,
    script_path: str,
    narration_path: str,
    timing_path: str,
    output_tag: str,
    logger: logging.Logger,
) -> str:
    """Full assembly pipeline. Returns path to final output."""

    WORKING_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    basename = Path(video_path).stem

    # Load keep_ranges from script
    with open(script_path, "r", encoding="utf-8") as f:
        script_data = json.load(f)

    keep_ranges = script_data.get("keep_ranges", [])
    if not keep_ranges:
        logger.error("No keep_ranges in script JSON")
        sys.exit(1)

    # Sort ranges by start time
    keep_ranges.sort(key=lambda x: x["start"])
    logger.info(f"Processing {len(keep_ranges)} keep ranges")

    # Step 1: Extract segments
    work_dir = str(WORKING_DIR)
    segment_paths = extract_segments(video_path, keep_ranges, work_dir, logger)

    # Step 2: Concatenate segments
    cut_video = str(WORKING_DIR / f"{basename}_cut.mp4")
    concatenate_segments(segment_paths, cut_video, work_dir, logger)

    # Step 3: Overlay narration (strips original audio)
    final_output = str(OUTPUT_DIR / f"{basename}_draft_{output_tag}.mp4")
    overlay_narration(cut_video, narration_path, final_output, logger)

    # Step 4: Generate SRT
    srt_output = str(OUTPUT_DIR / f"{basename}.srt")
    generate_srt(timing_path, srt_output, logger)

    # Cleanup intermediate segment files
    for seg in segment_paths:
        try:
            os.remove(seg)
        except OSError:
            pass
    try:
        os.remove(cut_video)
    except OSError:
        pass
    try:
        os.remove(str(WORKING_DIR / "concat_list.txt"))
    except OSError:
        pass

    logger.info(f"Final output: {final_output}")
    return final_output

def main():
    parser = argparse.ArgumentParser(description="Assemble final video with narration")
    parser.add_argument("video_path", help="Path to original video")
    parser.add_argument("script_path", help="Path to cleaned script JSON")
    parser.add_argument("narration_path", help="Path to narration WAV")
    parser.add_argument("timing_path", help="Path to narration timing JSON")
    parser.add_argument("--tag", default="local", help="Output tag: 'local' or 'cloud'")
    args = parser.parse_args()

    logger = setup_logging()

    for name, path in [("Video", args.video_path), ("Script", args.script_path),
                        ("Narration", args.narration_path), ("Timing", args.timing_path)]:
        if not Path(path).exists():
            logger.error(f"{name} not found: {path}")
            sys.exit(1)

    logger.info(f"=== Stage 5: Assembly ({args.tag}) ===")

    output = assemble(
        args.video_path, args.script_path,
        args.narration_path, args.timing_path,
        args.tag, logger,
    )

    logger.info(f"Done: {output}")
    print(output)

if __name__ == "__main__":
    main()
