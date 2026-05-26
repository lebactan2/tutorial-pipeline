"""
Stage 2: Clean transcript using Claude API.
Removes filler, false starts, repetitions. Outputs cleaned script + keep_ranges.
"""

import argparse
import json
import logging
import sys
import time
import os
import re
import warnings
from pathlib import Path
from dotenv import load_dotenv

# Suppress FutureWarnings (e.g. from google.generativeai) that write to stderr and crash PowerShell
warnings.filterwarnings("ignore", category=FutureWarning)

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
            logging.FileHandler(LOG_DIR / "clean_script.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

def load_style_guide() -> str:
    style_path = CONFIG_DIR / "style.md"
    if style_path.exists():
        return style_path.read_text(encoding="utf-8")
    return ""

def build_system_prompt(style_guide: str, target_language: str = "original") -> str:
    language_rule = {
        "original": "Keep the original language of each segment. Do not translate.",
        "vietnamese": (
            "Translate the final cleaned script to natural Vietnamese. "
            "Keep software names, node names, commands, file formats, and technical terms in English when that is clearer. "
            "Set every output entry's lang to \"vi\"."
        ),
        "english": (
            "Translate the final cleaned script to natural English. "
            "Keep software names, node names, commands, and file formats unchanged. "
            "Set every output entry's lang to \"en\"."
        ),
    }.get(target_language, "Keep the original language of each segment. Do not translate.")

    return f"""You are a script editor for bilingual English/Vietnamese tutorial videos.

Your job is to take a raw transcript (with timestamps) and produce a cleaned script with filler removed.

STYLE GUIDE:
{style_guide}

LANGUAGE TARGET OVERRIDE (highest priority):
{language_rule}

RULES:
- Remove filler words: um, uh, so, like, you know, actually, basically, alright, ok, okay, all right, oh yeah, ừm, à, thì, kiểu, cái này, cái đó
- Remove false starts and self-corrections (keep the corrected version)
- Remove repeated sentences or phrases (keep the best delivery)
- Follow the LANGUAGE TARGET OVERRIDE above for translation behavior.
- For mixed English/Vietnamese sentences, keep the mix intact only when target language is original.
- Keep technical terms in English
- Combine very short consecutive segments in the same language into one entry
- For each entry in your cleaned script, you MUST preserve the original start and end timestamps from the raw transcript.
- If you combine multiple consecutive segments into one entry, set "start" to the start timestamp of the first segment and "end" to the end timestamp of the last segment in the combined group.

OUTPUT FORMAT — respond with valid JSON only, no markdown fences, no extra text:
{{
  "cleaned_script": [
    {{"lang": "en", "text": "cleaned text here", "start": 0.0, "end": 5.2}},
    {{"lang": "vi", "text": "cleaned text here", "start": 7.1, "end": 15.3}}
  ]
}}"""

def load_system_config():
    config_path = CONFIG_DIR / "system.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}

def compact_transcript(transcript: dict) -> dict:
    """Keep only the fields the editor needs so long videos fit LLM context."""
    compact_segments = []
    for segment in transcript.get("segments", []):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue

        compact_segments.append({
            "start": round(float(segment.get("start", 0.0)), 3),
            "end": round(float(segment.get("end", 0.0)), 3),
            "lang": segment.get("lang", "en"),
            "text": text,
        })

    return {"segments": compact_segments}

def build_rough_script(transcript: dict, logger: logging.Logger, target_language: str = "original") -> dict:
    """Local fallback when the LLM API/model is unavailable."""
    compact = compact_transcript(transcript)
    cleaned_script = []
    keep_ranges = []

    for segment in compact["segments"]:
        text = clean_text_locally(segment["text"])
        if not text:
            continue

        entry = {
            "lang": segment.get("lang", "en"),
            "text": text,
            "start": segment["start"],
            "end": segment["end"],
        }

        if (
            cleaned_script
            and cleaned_script[-1].get("lang") == entry["lang"]
            and segment["start"] - float(cleaned_script[-1].get("end", segment["start"])) <= 1.0
            and len(cleaned_script[-1]["text"]) + len(text) <= 320
        ):
            cleaned_script[-1]["text"] = f"{cleaned_script[-1]['text']} {text}"
            cleaned_script[-1]["end"] = segment["end"]
        else:
            cleaned_script.append(entry)

        keep_ranges.append({
            "start": segment["start"],
            "end": segment["end"],
            "reason": "rough fallback",
        })

    if target_language != "original":
        logger.warning(
            "Using local cleanup fallback: filler was removed locally, "
            "but translation was not applied because no LLM response was available."
        )
    else:
        logger.warning(
            "Using local script cleanup fallback: filler was removed locally, "
            "but no LLM rewrite was applied."
        )
    return {
        "cleaned_script": cleaned_script,
        "keep_ranges": merge_keep_ranges(keep_ranges),
    }

def clean_text_locally(text: str) -> str:
    """Conservative local cleanup for when the LLM API is unavailable."""
    text = str(text or "").strip()
    if not text:
        return ""

    replacements = [
        (r"\b(?:um+|uh+|erm+|ah+)\b", " "),
        (r"\b(?:okay|ok|alright|all right)\b[, ]*", " "),
        (r"\b(?:basically|actually|literally)\b[, ]*", " "),
        (r"\b(?:you know|i mean|sort of|kind of)\b[, ]*", " "),
        (r"\bso[, ]+(?=\w)", " "),
        (r"^(?:and|then|now|so)[,\s]+", ""),
        (r"\b(\w+)(?:\s+\1\b)+", r"\1"),
    ]

    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip(" ,;:-")

    if not text:
        return ""

    filler_only = {"yeah", "yes", "no", "right", "sorry", "thanks", "thank you"}
    if text.lower() in filler_only:
        return ""

    return text[:1].upper() + text[1:]

def merge_keep_ranges(keep_ranges: list, gap_seconds: float = 0.5) -> list:
    if not keep_ranges:
        return []

    sorted_ranges = sorted(keep_ranges, key=lambda x: x["start"])
    merged = []

    for current in sorted_ranges:
        if not merged or current["start"] > merged[-1]["end"] + gap_seconds:
            merged.append(dict(current))
            continue

        merged[-1]["end"] = max(merged[-1]["end"], current["end"])
        merged[-1]["reason"] = "merged content"

    return merged

def antigravity_models(system_config: dict) -> list:
    configured = system_config.get("antigravity_model") or system_config.get("gemini_model")
    if configured:
        return [configured]
    candidates = [
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-1.5-flash",
        "gemini-pro",
    ]
    return [model for i, model in enumerate(candidates) if model and model not in candidates[:i]]

def split_env_list(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip().strip("'\"") for item in re.split(r"[\n,;]+", value) if item.strip()]

def antigravity_api_keys() -> list[str]:
    keys = []
    primary = os.getenv("ANTIGRAVITY_API_KEY", "").strip().strip("'\"")
    if primary:
        keys.append(primary)
    keys.extend(split_env_list(os.getenv("ANTIGRAVITY_API_KEYS", "")))

    deduped = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped

def is_quota_or_rate_limit_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in [
            "429",
            "quota",
            "rate limit",
            "rate_limit",
            "resourceexhausted",
            "resource exhausted",
        ]
    )

def clean_script(
    transcript: dict,
    logger: logging.Logger,
    provider_override: str = None,
    target_language: str = "original",
    max_retries: int = 3,
    allow_chunking: bool = True,
) -> dict:
    load_dotenv(CONFIG_DIR / ".env")

    system_config = load_system_config()
    target_language = target_language or system_config.get("target_language", "original")
    style_guide = load_style_guide()
    system_prompt = build_system_prompt(style_guide, target_language)
    
    provider = provider_override if provider_override else system_config.get("llm_provider", "claude")

    # Build a compact user message. Word-level timestamps make long videos too large
    # for many model context windows, while segment timestamps are enough here.
    compact = compact_transcript(transcript)

    chunk_size = 40
    if allow_chunking and len(compact["segments"]) > chunk_size:
        logger.info(
            f"Long transcript detected ({len(compact['segments'])} segments). "
            f"Cleaning in chunks of {chunk_size}."
        )

        merged_script = []
        merged_ranges = []
        chunks = [
            compact["segments"][i:i + chunk_size]
            for i in range(0, len(compact["segments"]), chunk_size)
        ]

        for idx, chunk in enumerate(chunks, start=1):
            logger.info(f"Cleaning chunk {idx}/{len(chunks)} ({len(chunk)} segments)")
            chunk_result = clean_script(
                {"segments": chunk},
                logger,
                provider_override=provider,
                target_language=target_language,
                max_retries=1,
                allow_chunking=False,
            )
            merged_script.extend(chunk_result.get("cleaned_script", []))
            merged_ranges.extend(chunk_result.get("keep_ranges", []))
            if idx < len(chunks):
                time.sleep(7)

        result = {
            "cleaned_script": merged_script,
            "keep_ranges": merge_keep_ranges(merged_ranges),
        }
        logger.info(
            f"Chunked cleanup complete: {len(result['cleaned_script'])} segments, "
            f"{len(result['keep_ranges'])} keep ranges"
        )
        return result

    transcript_text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    logger.info(f"Transcript compacted to {len(compact['segments'])} segments")
    user_message = f"""Here is the raw transcript with segment timestamps. Clean it up according to the style guide and produce the JSON output.

TRANSCRIPT:
{transcript_text}"""

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Calling {provider.capitalize()} API (attempt {attempt}/{max_retries})...")
            start_time = time.time()

            raw_text = ""
            if provider == "claude":
                import anthropic
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    logger.error("ANTHROPIC_API_KEY not set. Add it to config/.env")
                    sys.exit(1)
                client = anthropic.Anthropic(api_key=api_key)
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=8192,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                raw_text = response.content[0].text.strip()
            elif provider == "antigravity":
                import google.generativeai as genai
                api_keys = antigravity_api_keys()
                if not api_keys:
                    logger.error("ANTIGRAVITY_API_KEY not set. Add it to config/.env")
                    sys.exit(1)
                last_model_error = None
                models = antigravity_models(system_config)
                for key_index, api_key in enumerate(api_keys, start=1):
                    genai.configure(api_key=api_key)
                    logger.info(f"Using Gemini API key {key_index}/{len(api_keys)}")
                    key_quota_exhausted = False
                    for model_name in models:
                        try:
                            logger.info(f"Using Gemini model: {model_name}")
                            generative_model = genai.GenerativeModel(model_name, system_instruction=system_prompt)
                            generation_config = genai.types.GenerationConfig(
                                max_output_tokens=16384,
                                response_mime_type="application/json"
                            )
                            response = generative_model.generate_content(
                                user_message,
                                generation_config=generation_config
                            )
                            raw_text = response.text.strip()
                            break
                        except Exception as model_error:
                            last_model_error = model_error
                            if is_quota_or_rate_limit_error(model_error):
                                key_quota_exhausted = True
                                logger.warning(
                                    f"Gemini API key {key_index}/{len(api_keys)} hit quota/rate limit; "
                                    "trying the next key."
                                )
                                break
                            logger.warning(f"Gemini model {model_name} failed: {model_error}")
                    if raw_text:
                        break
                    if not key_quota_exhausted and last_model_error:
                        logger.warning(
                            f"Gemini API key {key_index}/{len(api_keys)} returned no usable text; "
                            "trying the next key."
                        )
                if not raw_text:
                    raise last_model_error or RuntimeError("No Gemini model returned text")
            elif provider == "openrouter" or provider == "custom":
                import openai
                
                if provider == "openrouter":
                    api_key = os.getenv("OPENROUTER_API_KEY")
                    base_url = "https://openrouter.ai/api/v1"
                    model = system_config.get("openrouter_model", "openai/gpt-4o")
                    if not api_key:
                        logger.error("OPENROUTER_API_KEY not set.")
                        sys.exit(1)
                else:
                    api_key = os.getenv("CUSTOM_API_KEY")
                    base_url = os.getenv("CUSTOM_API_BASE_URL")
                    model = system_config.get("custom_model", "gpt-3.5-turbo")
                    if not api_key or not base_url:
                        logger.error("CUSTOM_API_KEY or CUSTOM_API_BASE_URL not set.")
                        sys.exit(1)
                
                logger.info(f"Using model: {model} from {base_url}")
                client = openai.OpenAI(base_url=base_url, api_key=api_key)
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ]
                )
                raw_text = response.choices[0].message.content.strip()
            else:
                if provider == "local":
                    return build_rough_script(transcript, logger, target_language)
                logger.error(f"Unknown provider: {provider}")
                return build_rough_script(transcript, logger, target_language)

            elapsed = time.time() - start_time
            logger.info(f"API response received in {elapsed:.1f}s")

            # Extract text content

            # Try to parse JSON — handle potential markdown fences
            if raw_text.startswith("```"):
                # Strip markdown code fences
                lines = raw_text.split("\n")
                start_idx = 1 if lines[0].startswith("```") else 0
                end_idx = -1 if lines[-1].strip() == "```" else len(lines)
                raw_text = "\n".join(lines[start_idx:end_idx]).strip()

            result = json.loads(raw_text)

            # Validate schema
            if "cleaned_script" not in result:
                raise ValueError("Missing required key: cleaned_script")

            keep_ranges = []
            for entry in result["cleaned_script"]:
                if "lang" not in entry or "text" not in entry:
                    raise ValueError(f"Invalid script entry: {entry}")
                if "start" in entry and "end" in entry:
                    try:
                        keep_ranges.append({
                            "start": round(float(entry["start"]), 3),
                            "end": round(float(entry["end"]), 3),
                            "reason": "cleaned segment"
                        })
                    except (ValueError, TypeError):
                        pass

            # Programmatically generate keep_ranges and merge them
            result["keep_ranges"] = merge_keep_ranges(keep_ranges)

            logger.info(f"Cleaned script: {len(result['cleaned_script'])} segments, "
                        f"{len(result['keep_ranges'])} keep ranges")
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"Attempt {attempt}: JSON parse error: {e}")
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"All {max_retries} attempts failed. Last response:\n{raw_text[:500]}")
                return build_rough_script(transcript, logger, target_language)

        except Exception as e:
            logger.warning(f"Attempt {attempt}: Error: {e}")
            if attempt < max_retries:
                if is_quota_or_rate_limit_error(e):
                    wait = 20 + attempt * 10
                else:
                    wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"All {max_retries} attempts failed: {e}")
                return build_rough_script(transcript, logger, target_language)

def main():
    parser = argparse.ArgumentParser(description="Clean transcript via AI API")
    parser.add_argument("transcript_path", help="Path to transcript JSON file")
    parser.add_argument("--output", help="Override output path")
    parser.add_argument("--provider", choices=["claude", "antigravity", "openrouter", "custom", "local"], help="Override LLM provider")
    parser.add_argument(
        "--target-language",
        choices=["original", "vietnamese", "english"],
        default="original",
        help="Output script language. Use vietnamese before VieNeu-TTS, original/english before NeuTTS.",
    )
    args = parser.parse_args()

    logger = setup_logging()

    transcript_path = Path(args.transcript_path).resolve()
    if not transcript_path.exists():
        logger.error(f"Transcript not found: {transcript_path}")
        sys.exit(1)

    WORKING_DIR.mkdir(exist_ok=True)

    basename = transcript_path.stem.replace(".transcript", "")
    output_path = Path(args.output) if args.output else WORKING_DIR / f"{basename}.script.json"

    logger.info("=== Stage 2: Script Cleanup ===")
    logger.info(f"Input: {transcript_path}")
    logger.info(f"Output: {output_path}")

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    result = clean_script(
        transcript,
        logger,
        provider_override=args.provider,
        target_language=args.target_language,
    )
    result["target_language"] = args.target_language

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"Cleaned script saved to: {output_path}")
    print(str(output_path))

if __name__ == "__main__":
    main()
