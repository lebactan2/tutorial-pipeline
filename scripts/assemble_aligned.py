"""
Stage 5 alternative: Assemble video by aligning each source timestamp range to
its own generated narration segment.

This avoids the global stretch that can push narration out of sync with the
tutorial screen recording.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
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
            logging.FileHandler(LOG_DIR / "assemble_aligned.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


def run_command(cmd: list[str], logger: logging.Logger, timeout: int = 1800):
    logger.info("Running: %s", " ".join(str(part) for part in cmd[:8]))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def media_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path}")
    return float(result.stdout.strip())


def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        secs += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(timing_data: list[dict], srt_path: Path, logger: logging.Logger):
    lines = []
    for i, entry in enumerate(timing_data, start=1):
        lines.append(str(i))
        lines.append(f"{format_timestamp(entry['start'])} --> {format_timestamp(entry['end'])}")
        lines.append(str(entry.get("text", "")).strip())
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("SRT generated: %s", srt_path)


def concat_videos(segment_paths: list[Path], output_path: Path, work_dir: Path, logger: logging.Logger):
    list_file = work_dir / "concat_aligned.txt"
    with list_file.open("w", encoding="utf-8") as f:
        for segment in segment_paths:
            safe_path = segment.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        logger,
        timeout=3600,
    )


def render_segment(
    video_path: Path,
    audio_path: Path,
    source_start: float,
    source_end: float,
    output_path: Path,
    logger: logging.Logger,
    preset: str,
    crf: int,
):
    source_duration = max(0.1, source_end - source_start)
    audio_duration = media_duration(str(audio_path))
    setpts_factor = max(0.05, audio_duration / source_duration)

    filter_complex = (
        f"[0:v]setpts={setpts_factor:.8f}*PTS,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2[v]"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            f"{source_start:.3f}",
            "-t",
            f"{source_duration:.3f}",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_path),
        ],
        logger,
        timeout=1800,
    )

    return audio_duration


def assemble(video_path: str, timing_path: str, tag: str, logger: logging.Logger, preset: str, crf: int) -> str:
    video = Path(video_path).resolve()
    timing = Path(timing_path).resolve()
    timing_data = json.loads(timing.read_text(encoding="utf-8"))
    if not timing_data:
        raise RuntimeError("Timing file is empty.")

    for entry in timing_data:
        if "segment_file" not in entry:
            raise RuntimeError("Aligned assembly requires timing entries with segment_file.")
        if "source_start" not in entry or "source_end" not in entry:
            raise RuntimeError("Aligned assembly requires source_start/source_end on each timing entry.")

    WORKING_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    basename = video.stem
    work_dir = WORKING_DIR / f"{basename}_{tag}_aligned"
    work_dir.mkdir(parents=True, exist_ok=True)

    segment_paths = []
    final_timing = []
    cursor = 0.0

    for i, entry in enumerate(timing_data, start=1):
        audio_path = Path(entry["segment_file"]).resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Missing audio segment: {audio_path}")

        source_start = float(entry["source_start"])
        source_end = float(entry["source_end"])
        segment_output = work_dir / f"video_{i:04d}.mp4"

        logger.info(
            "Render aligned segment %s/%s source %.2f-%.2fs",
            i,
            len(timing_data),
            source_start,
            source_end,
        )

        audio_duration = render_segment(
            video,
            audio_path,
            source_start,
            source_end,
            segment_output,
            logger,
            preset,
            crf,
        )
        segment_paths.append(segment_output)

        final_entry = dict(entry)
        final_entry["index"] = i
        final_entry["start"] = round(cursor, 3)
        final_entry["end"] = round(cursor + audio_duration, 3)
        final_timing.append(final_entry)
        cursor += audio_duration

    output_path = OUTPUT_DIR / f"{basename}_draft_{tag}.mp4"
    concat_videos(segment_paths, output_path, work_dir, logger)

    timing_output = WORKING_DIR / f"{basename}.aligned_{tag}_timing.json"
    timing_output.write_text(json.dumps(final_timing, ensure_ascii=False, indent=2), encoding="utf-8")

    srt_path = OUTPUT_DIR / f"{basename}.srt"
    write_srt(final_timing, srt_path, logger)

    logger.info("Aligned output: %s", output_path)
    logger.info("Final duration: %.1fs", cursor)
    return str(output_path)


def main():
    parser = argparse.ArgumentParser(description="Assemble final video with per-segment timestamp alignment")
    parser.add_argument("video_path", help="Path to original video")
    parser.add_argument("timing_path", help="Timing JSON with source timestamps and segment_file paths")
    parser.add_argument("--tag", default="local", help="Output tag")
    parser.add_argument("--preset", default="veryfast", help="x264 preset")
    parser.add_argument("--crf", type=int, default=20, help="x264 CRF")
    args = parser.parse_args()

    logger = setup_logging()
    logger.info("=== Stage 5: Aligned Assembly ===")

    for name, path in [("Video", args.video_path), ("Timing", args.timing_path)]:
        if not Path(path).exists():
            logger.error("%s not found: %s", name, path)
            sys.exit(1)

    output = assemble(args.video_path, args.timing_path, args.tag, logger, args.preset, args.crf)
    print(output)


if __name__ == "__main__":
    main()
