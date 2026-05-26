"""
Post-process a cleaned script before TTS.

This removes low-information classroom chatter and merges tiny fragments that
can make cloned TTS produce breathy or moaning artifacts.
"""

import argparse
import json
import re
from pathlib import Path


DROP_PATTERNS = [
    r"^yeah[.!?]*$",
    r"^cool[.!?]*$",
    r"^hello[.!?]*$",
    r"^yes[.!?]*$",
    r"^no[.!?]*$",
    r"^no not yet[.!?]*$",
    r"^have you learnt[.!?]*$",
    r"^now any questions[.!?]*$",
    r"^is there any question",
    r"question\. yes\. now\. what's the question",
    r"help the students",
    r"raise your hand",
    r"who finished",
    r"all good",
    r"show me the notes",
    r"show me all the material",
    r"show me",
    r"where is the file",
    r"it's cool",
    r"^every object, every material[.!?]*$",
    r"just a minute",
    r"anyone needs help",
    r"^the bag[.!?]*$",
    r"^bugs[.!?]*$",
    r"^select[.!?]*$",
    r"^look at this[.!?]*$",
    r"^white color[.!?]*$",
    r"^glow colors\. hold on[.!?]*$",
    r"^now\. anyone\. have\. to let[.!?]*$",
]

REPLACEMENTS = [
    (r"\bbugging\b", "baking"),
    (r"\bbug\b", "bake"),
    (r"\bbag\b", "bake"),
    (r"^Come\. And light\.$", "Add a light."),
    (r"^No camera confound\.$", "No camera component is needed."),
    (r"^The panel view right\.$", "Use the panel view on the right."),
    (r"^Yeah\. 360 degree camera\.$", "It works like a 360 degree camera."),
    (
        r"^This is the\. Top bloom\. Yeah\. And this is the\. Bloom\. Palette\. Different effects\.$",
        "This is the TOP bloom, and this is the palette bloom. They are different effects.",
    ),
]


def merge_keep_ranges(entries, gap_seconds=0.5):
    ranges = [
        {"start": float(e["start"]), "end": float(e["end"]), "reason": "sanitized segment"}
        for e in entries
        if "start" in e and "end" in e
    ]
    ranges.sort(key=lambda item: item["start"])

    merged = []
    for current in ranges:
        if not merged or current["start"] > merged[-1]["end"] + gap_seconds:
            merged.append(dict(current))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], current["end"])
            merged[-1]["reason"] = "merged sanitized content"
    return merged


def clean_text(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    for pattern, replacement in REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"Now,? who finished\?.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Now who can do it\?.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(Okay|Ok|Alright)\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bYeah\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def should_drop(text):
    compact = text.strip().lower()
    if not compact:
        return True
    for pattern in DROP_PATTERNS:
        if re.search(pattern, compact, flags=re.IGNORECASE):
            return True
    words = re.findall(r"[A-Za-z0-9]+", compact)
    if len(words) <= 2 and not re.search(r"\b(obj|uv|hdri|pbr|render|camera|texture|material)\b", compact):
        return True
    return False


def sanitize(data, min_merge_chars=70):
    entries = []
    dropped = 0

    for original in data.get("cleaned_script", []):
        entry = dict(original)
        entry["text"] = clean_text(entry.get("text", ""))
        if should_drop(entry["text"]):
            dropped += 1
            continue
        entries.append(entry)

    merged = []
    for entry in entries:
        text = entry["text"]
        if (
            merged
            and entry.get("lang", "en") == merged[-1].get("lang", "en")
            and len(text) < min_merge_chars
            and len(merged[-1]["text"]) < 280
            and "start" in entry
            and "end" in merged[-1]
            and float(entry["start"]) - float(merged[-1]["end"]) <= 2.0
        ):
            merged[-1]["text"] = f"{merged[-1]['text']} {text}".strip()
            if "end" in entry:
                merged[-1]["end"] = entry["end"]
            continue
        merged.append(entry)

    return {
        "cleaned_script": merged,
        "keep_ranges": merge_keep_ranges(merged),
        "_sanitize": {
            "input_segments": len(data.get("cleaned_script", [])),
            "dropped_segments": dropped,
            "output_segments": len(merged),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Sanitize cleaned script before TTS")
    parser.add_argument("input", help="Input script JSON")
    parser.add_argument("--output", help="Output path. Defaults to overwriting input.")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path

    data = json.loads(input_path.read_text(encoding="utf-8"))
    result = sanitize(data)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    info = result["_sanitize"]
    print(
        f"{input_path} -> {output_path}: "
        f"{info['input_segments']} in, {info['dropped_segments']} dropped, "
        f"{info['output_segments']} out"
    )


if __name__ == "__main__":
    main()
