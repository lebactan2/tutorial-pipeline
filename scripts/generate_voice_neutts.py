"""
Stage 3a alternative: Generate English narration using NeuTTS.
Produces narration WAV + per-segment timing JSON for aligned assembly.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NEUTTS_DIR = PROJECT_ROOT / "neutts-main"
if NEUTTS_DIR.exists():
    sys.path.insert(0, str(NEUTTS_DIR))

CONFIG_DIR = PROJECT_ROOT / "config"
WORKING_DIR = PROJECT_ROOT / "working"
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "generate_voice_neutts.log", encoding="utf-8"),
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


def get_audio_duration(path: str) -> float:
    info = sf.info(path)
    return float(info.frames) / float(info.samplerate)


def get_wav_duration(wav_path: str) -> float:
    with wave.open(wav_path, "r") as w:
        return w.getnframes() / float(w.getframerate())


def create_silence_wav(output_path: str, seconds: float = 1.0, sample_rate: int = 24000):
    frame_count = int(sample_rate * seconds)
    with wave.open(output_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00" * (frame_count * 2))


def synthesize_windows_tts(text: str, output_path: str):
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


def split_text(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return [" "]
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            words = sentence.split()
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > max_chars:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def expand_script_entries(cleaned_script: list[dict], max_chars: int) -> list[dict]:
    expanded = []
    for entry in cleaned_script:
        chunks = split_text(entry.get("text", ""), max_chars)
        source_start = entry.get("start")
        source_end = entry.get("end")

        if len(chunks) == 1 or source_start is None or source_end is None:
            clone = dict(entry)
            clone["text"] = chunks[0]
            expanded.append(clone)
            continue

        total_chars = sum(max(1, len(chunk)) for chunk in chunks)
        source_duration = float(source_end) - float(source_start)
        cursor = float(source_start)
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                chunk_end = float(source_end)
            else:
                chunk_duration = source_duration * (max(1, len(chunk)) / total_chars)
                chunk_end = cursor + chunk_duration

            expanded.append(
                {
                    **entry,
                    "text": chunk,
                    "start": round(cursor, 3),
                    "end": round(chunk_end, 3),
                }
            )
            cursor = chunk_end

    return expanded


def transcribe_reference(audio_path: Path, logger: logging.Logger, max_duration: float) -> tuple[float, float, str]:
    from faster_whisper import WhisperModel

    config = load_system_config()
    whisper_cfg = config.get("whisper", {})
    model_size = whisper_cfg.get("model_size", "medium")
    device = whisper_cfg.get("device", "cpu")
    compute_type = whisper_cfg.get("compute_type", "int8")

    cache_path = WORKING_DIR / f"{audio_path.stem}.neutts_ref_transcript.json"
    if cache_path.exists() and cache_path.stat().st_mtime >= audio_path.stat().st_mtime:
        logger.info("Using cached NeuTTS reference transcript: %s", cache_path)
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        logger.info("Transcribing NeuTTS reference audio with Whisper: %s", audio_path)
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments_iter, _ = model.transcribe(
            str(audio_path),
            language="en",
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=100),
        )
        segments = []
        for segment in segments_iter:
            words = []
            for word in segment.words or []:
                words.append(
                    {
                        "start": round(float(word.start), 3),
                        "end": round(float(word.end), 3),
                        "word": word.word.strip(),
                    }
                )
            segments.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": segment.text.strip(),
                    "words": words,
                }
            )
        data = {"segments": segments}
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    segments = data.get("segments", [])
    if not segments:
        raise RuntimeError("Could not transcribe the NeuTTS reference audio.")

    selected_words = []
    start_time = float(segments[0]["start"])
    end_limit = start_time + max_duration

    for segment in segments:
        for word in segment.get("words", []):
            word_start = float(word["start"])
            word_end = float(word["end"])
            if word_start < start_time:
                continue
            if word_end > end_limit:
                break
            selected_words.append(word)
        if selected_words and float(segment["end"]) >= end_limit:
            break

    if selected_words:
        ref_start = max(0.0, float(selected_words[0]["start"]) - 0.15)
        ref_end = min(get_audio_duration(str(audio_path)), float(selected_words[-1]["end"]) + 0.2)
        ref_text = " ".join(word["word"] for word in selected_words)
    else:
        selected_segments = []
        for segment in segments:
            if float(segment["start"]) >= end_limit:
                break
            selected_segments.append(segment)
        ref_start = max(0.0, float(selected_segments[0]["start"]) - 0.15)
        ref_end = min(get_audio_duration(str(audio_path)), min(float(selected_segments[-1]["end"]) + 0.2, end_limit))
        ref_text = " ".join(segment["text"] for segment in selected_segments)

    ref_text = re.sub(r"\s+", " ", ref_text).strip()
    if not ref_text:
        raise RuntimeError("NeuTTS reference transcript is empty.")

    return ref_start, ref_end, ref_text


def prepare_reference_audio(audio_path: Path, logger: logging.Logger, max_duration: float) -> tuple[Path, str]:
    text_path = audio_path.with_suffix(".txt")
    audio_duration = get_audio_duration(str(audio_path))

    if text_path.exists() and audio_duration <= max_duration:
        ref_text = text_path.read_text(encoding="utf-8").strip()
        if not ref_text:
            raise RuntimeError(f"Reference text file is empty: {text_path}")
        return audio_path, ref_text

    ref_start, ref_end, ref_text = transcribe_reference(audio_path, logger, max_duration)
    trim_duration = ref_end - ref_start
    if trim_duration <= 0:
        raise RuntimeError("Invalid NeuTTS reference trim duration.")

    import librosa

    y, _ = librosa.load(str(audio_path), sr=16000, mono=True, offset=ref_start, duration=trim_duration)
    trim_path = WORKING_DIR / f"{audio_path.stem}.neutts_ref.wav"
    sf.write(trim_path, y, 16000)
    ref_text_path = WORKING_DIR / f"{audio_path.stem}.neutts_ref.txt"
    ref_text_path.write_text(ref_text, encoding="utf-8")

    logger.info("Prepared NeuTTS reference: %s (%.1fs)", trim_path, trim_duration)
    logger.info("Reference text: %s", ref_text[:180])
    return trim_path, ref_text


def initialize_neutts(logger: logging.Logger):
    try:
        from neutts import NeuTTS
    except Exception as e:
        raise RuntimeError(
            "NeuTTS is not importable. Install it with: uv pip install -e neutts-main[all]"
        ) from e

    config = load_system_config()
    neutts_cfg = config.get("neutts", {})
    backbone_repo = neutts_cfg.get("backbone_repo", "neuphonic/neutts-nano-q4-gguf")
    backbone_device = neutts_cfg.get("backbone_device", "cpu")
    codec_repo = neutts_cfg.get("codec_repo", "neuphonic/neucodec-onnx-decoder-int8")
    codec_device = neutts_cfg.get("codec_device", "cpu")

    logger.info("Loading NeuTTS backbone: %s on %s", backbone_repo, backbone_device)
    logger.info("Loading NeuTTS codec: %s on %s", codec_repo, codec_device)
    return NeuTTS(
        backbone_repo=backbone_repo,
        backbone_device=backbone_device,
        codec_repo=codec_repo,
        codec_device=codec_device,
    )


def encode_reference_codes(tts, ref_audio_path: Path, logger: logging.Logger):
    import torch

    cache_path = WORKING_DIR / f"{ref_audio_path.stem}.neutts_ref_codes.pt"
    if cache_path.exists() and cache_path.stat().st_mtime >= ref_audio_path.stat().st_mtime:
        logger.info("Using cached NeuTTS reference codes: %s", cache_path)
        return torch.load(cache_path, map_location="cpu")

    if not getattr(tts, "_is_onnx_codec", False):
        ref_codes = tts.encode_reference(str(ref_audio_path))
        torch.save(ref_codes.cpu() if hasattr(ref_codes, "cpu") else ref_codes, cache_path)
        return ref_codes

    logger.info("Encoding reference with torch NeuCodec because ONNX decoder cannot encode.")
    import librosa
    from neucodec import NeuCodec

    codec = NeuCodec.from_pretrained("neuphonic/neucodec")
    codec.eval().to("cpu")

    wav, _ = librosa.load(str(ref_audio_path), sr=16000, mono=True)
    wav_tensor = torch.from_numpy(wav).float().unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        ref_codes = codec.encode_code(audio_or_path=wav_tensor).squeeze(0).squeeze(0)

    torch.save(ref_codes.cpu(), cache_path)
    return ref_codes


def save_float_wav(audio: np.ndarray, output_path: str, sample_rate: int = 24000):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.squeeze(audio)
    sf.write(output_path, audio, sample_rate)


def concatenate_wavs(wav_paths: list[str], output_path: str, gap_seconds: float = 0.2):
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


def generate_voice(args, logger: logging.Logger) -> tuple[str, str]:
    script_path = Path(args.script_path).resolve()
    voice_ref_path = Path(args.voice_ref_path).resolve()

    with open(script_path, "r", encoding="utf-8") as f:
        script_data = json.load(f)

    cleaned_script = script_data.get("cleaned_script", [])
    if not cleaned_script:
        raise RuntimeError("No segments in cleaned_script.")

    entries = expand_script_entries(cleaned_script, args.max_chars)
    logger.info("NeuTTS entries: %s expanded from %s script segments", len(entries), len(cleaned_script))

    tts = None
    ref_codes = None
    ref_text = ""
    try:
        ref_audio_path, ref_text = prepare_reference_audio(voice_ref_path, logger, args.max_ref_duration)
        tts = initialize_neutts(logger)
        logger.info("Encoding NeuTTS voice reference: %s", ref_audio_path)
        ref_codes = encode_reference_codes(tts, ref_audio_path, logger)
    except Exception as e:
        if args.require_clone:
            raise
        logger.warning("NeuTTS initialization/reference encoding failed: %s", e)
        logger.warning("Using Windows TTS fallback. Voice cloning is disabled for this run.")

    WORKING_DIR.mkdir(exist_ok=True)
    basename = script_path.stem.replace(".script", "")
    segment_dir = WORKING_DIR / f"{basename}_neutts_segments"
    if args.keep_segments:
        segment_dir.mkdir(parents=True, exist_ok=True)

    segment_wavs = []
    segment_texts = []

    for i, entry in enumerate(entries):
        text = entry.get("text", " ").strip() or " "
        lang = entry.get("lang", "en")

        if args.keep_segments:
            seg_path = str(segment_dir / f"seg_{i + 1:04d}.wav")
        else:
            seg_path = str(WORKING_DIR / f"{basename}_neutts_seg_{i + 1:04d}.wav")

        logger.info("  Segment %s/%s [%s]: %s", i + 1, len(entries), lang, text[:80])
        try:
            start_time = time.time()
            if tts is None:
                synthesize_windows_tts(text, seg_path)
            else:
                wav = tts.infer(text, ref_codes, ref_text)
                save_float_wav(wav, seg_path, sample_rate=24000)
            elapsed = time.time() - start_time
            logger.info("    Generated in %.1fs", elapsed)
        except Exception as e:
            if args.require_clone:
                raise RuntimeError(f"NeuTTS failed for segment {i + 1}: {e}") from e
            logger.warning("    NeuTTS failed for segment %s: %s", i + 1, e)
            try:
                synthesize_windows_tts(text, seg_path)
                logger.info("    Windows TTS fallback generated segment")
            except Exception as fallback_error:
                logger.error("    Windows TTS fallback failed: %s", fallback_error)
                create_silence_wav(seg_path)

        segment_wavs.append(seg_path)
        segment_texts.append(
            {
                "lang": lang,
                "text": text,
                "source_start": entry.get("start"),
                "source_end": entry.get("end"),
                "segment_file": seg_path,
            }
        )

    narration_path = str(WORKING_DIR / f"{basename}.narration_neutts.wav")
    logger.info("Concatenating %s NeuTTS segments...", len(segment_wavs))
    timings = concatenate_wavs(segment_wavs, narration_path)

    timing_data = []
    for i, (timing, text_entry) in enumerate(zip(timings, segment_texts)):
        timing_data.append(
            {
                "index": i + 1,
                "start": timing["start"],
                "end": timing["end"],
                "lang": text_entry["lang"],
                "text": text_entry["text"],
                "source_start": text_entry.get("source_start"),
                "source_end": text_entry.get("source_end"),
                "segment_file": text_entry.get("segment_file"),
                "audio_duration": round(timing["end"] - timing["start"], 3),
            }
        )

    timing_path = str(WORKING_DIR / f"{basename}.narration_neutts_timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing_data, f, ensure_ascii=False, indent=2)

    if not args.keep_segments:
        for seg_path in segment_wavs:
            try:
                os.remove(seg_path)
            except OSError:
                pass

    total_duration = timings[-1]["end"] if timings else 0
    logger.info("NeuTTS narration complete: %.1fs total, %s segments", total_duration, len(timings))
    return narration_path, timing_path


def main():
    parser = argparse.ArgumentParser(description="Generate narration via NeuTTS")
    parser.add_argument("script_path", help="Path to cleaned script JSON")
    parser.add_argument("voice_ref_path", help="Path to English voice reference audio")
    parser.add_argument(
        "--require-clone",
        action="store_true",
        help="Fail instead of falling back when NeuTTS voice cloning fails",
    )
    parser.add_argument(
        "--keep-segments",
        action="store_true",
        help="Keep per-segment WAV files and record them in timing JSON for aligned assembly",
    )
    parser.add_argument("--max-ref-duration", type=float, default=12.0, help="Max seconds to use from voice ref")
    parser.add_argument("--max-chars", type=int, default=240, help="Max chars per generated TTS segment")
    args = parser.parse_args()

    logger = setup_logging()
    script_path = Path(args.script_path).resolve()
    voice_ref_path = Path(args.voice_ref_path).resolve()

    if not script_path.exists():
        logger.error("Script not found: %s", script_path)
        sys.exit(1)
    if not voice_ref_path.exists():
        logger.error("Voice reference not found: %s", voice_ref_path)
        sys.exit(1)

    logger.info("=== Stage 3a: NeuTTS Voice Generation ===")
    logger.info("Script: %s", script_path)
    logger.info("Voice ref: %s", voice_ref_path)

    narration_path, timing_path = generate_voice(args, logger)
    logger.info("Narration: %s", narration_path)
    logger.info("Timing: %s", timing_path)
    print(f"{narration_path}|{timing_path}")


if __name__ == "__main__":
    main()
