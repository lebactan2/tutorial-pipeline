import argparse
import json
import logging
import sys
import os
import re
from pathlib import Path

def parse_srt(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    blocks = content.strip().split('\n\n')
    segments = []
    
    for block in blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            # Parse timing
            timing_line = lines[1]
            match = re.match(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', timing_line)
            if match:
                start_str, end_str = match.groups()
                start = parse_time(start_str)
                end = parse_time(end_str)
                text = " ".join(lines[2:]).strip()
                segments.append({
                    "start": start,
                    "end": end,
                    "text": text,
                    "lang": "en", # default to en, LLM will fix it or doesn't matter for clean_script
                    "words": [] # empty words list since we don't have word-level timings
                })
    return segments

def parse_time(time_str):
    parts = time_str.split(',')
    ms = int(parts[1]) if len(parts) > 1 else 0
    time_parts = parts[0].split(':')
    h, m, s = int(time_parts[0]), int(time_parts[1]), int(time_parts[2])
    return round(h * 3600 + m * 60 + s + ms / 1000.0, 3)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("srt_path", help="Path to SRT file")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    srt_path = Path(args.srt_path)
    if not srt_path.exists():
        print(f"Error: {srt_path} not found")
        sys.exit(1)

    segments = parse_srt(srt_path)
    result = {"segments": segments}
    
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = Path("working") / f"{srt_path.stem}.transcript.json"
        
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(str(out_path))

if __name__ == "__main__":
    main()
