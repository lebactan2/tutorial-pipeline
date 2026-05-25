import argparse
import sys
import yt_dlp
from pathlib import Path

def get_youtube_basename(url: str) -> str:
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'restrictfilenames': True,
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        filename = ydl.prepare_filename(info_dict)
    
    return Path(filename).stem

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Get YouTube video basename.")
    parser.add_argument("url", help="YouTube video URL")
    args = parser.parse_args()

    try:
        basename = get_youtube_basename(args.url)
        print(basename)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
