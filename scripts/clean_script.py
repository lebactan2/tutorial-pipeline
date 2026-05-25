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

def build_system_prompt(style_guide: str) -> str:
    return f"""You are a script editor for bilingual English/Vietnamese tutorial videos.

Your job is to take a raw transcript (with timestamps) and produce:
1. A cleaned script with filler removed
2. A list of timestamp ranges from the ORIGINAL video to keep

STYLE GUIDE:
{style_guide}

RULES:
- Remove filler words: um, uh, so, like, you know, actually, basically, ừm, à, thì, kiểu, cái này, cái đó
- Remove false starts and self-corrections (keep the corrected version)
- Remove repeated sentences or phrases (keep the best delivery)
- Preserve the ORIGINAL language of each segment — never translate
- For mixed English/Vietnamese sentences, keep the mix intact
- Keep technical terms in English
- Combine very short consecutive segments in the same language into one entry
- The keep_ranges must use the ORIGINAL video timestamps and should cover all content you kept
- Merge adjacent keep_ranges that are less than 0.5s apart
- Remove ranges corresponding to: filler, long silences (>1.5s), verbal mistakes

OUTPUT FORMAT — respond with valid JSON only, no markdown fences, no extra text:
{{
  "cleaned_script": [
    {{"lang": "en", "text": "cleaned text here"}},
    {{"lang": "vi", "text": "cleaned text here"}}
  ],
  "keep_ranges": [
    {{"start": 0.0, "end": 5.2, "reason": "introduction"}},
    {{"start": 7.1, "end": 15.3, "reason": "main explanation"}}
  ]
}}"""

def load_system_config():
    config_path = CONFIG_DIR / "system.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}

def clean_script(transcript: dict, logger: logging.Logger, provider_override: str = None, max_retries: int = 3) -> dict:
    load_dotenv(CONFIG_DIR / ".env")

    style_guide = load_style_guide()
    system_prompt = build_system_prompt(style_guide)
    system_config = load_system_config()
    
    provider = provider_override if provider_override else system_config.get("llm_provider", "claude")

    # Build the user message with the transcript
    transcript_text = json.dumps(transcript, ensure_ascii=False, indent=2)
    user_message = f"""Here is the raw transcript with word-level timestamps. Clean it up according to the style guide and produce the JSON output.

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
                api_key = os.getenv("ANTIGRAVITY_API_KEY")
                if not api_key:
                    logger.error("ANTIGRAVITY_API_KEY not set. Add it to config/.env")
                    sys.exit(1)
                genai.configure(api_key=api_key)
                generative_model = genai.GenerativeModel('gemini-1.5-pro', system_instruction=system_prompt)
                response = generative_model.generate_content(user_message)
                raw_text = response.text.strip()
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
                logger.error(f"Unknown provider: {provider}")
                sys.exit(1)

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
            if "cleaned_script" not in result or "keep_ranges" not in result:
                raise ValueError("Missing required keys: cleaned_script, keep_ranges")

            for entry in result["cleaned_script"]:
                if "lang" not in entry or "text" not in entry:
                    raise ValueError(f"Invalid script entry: {entry}")

            for kr in result["keep_ranges"]:
                if "start" not in kr or "end" not in kr:
                    raise ValueError(f"Invalid keep_range: {kr}")

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
                sys.exit(1)

        except Exception as e:
            logger.warning(f"Attempt {attempt}: Error: {e}")
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"All {max_retries} attempts failed: {e}")
                sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Clean transcript via AI API")
    parser.add_argument("transcript_path", help="Path to transcript JSON file")
    parser.add_argument("--output", help="Override output path")
    parser.add_argument("--provider", choices=["claude", "antigravity", "openrouter", "custom"], help="Override LLM provider")
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

    result = clean_script(transcript, logger, provider_override=args.provider)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"Cleaned script saved to: {output_path}")
    print(str(output_path))

if __name__ == "__main__":
    main()
