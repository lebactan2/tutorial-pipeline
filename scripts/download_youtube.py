import argparse
import sys
import yt_dlp
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = PROJECT_ROOT / "inbox"

def download_youtube_video(url: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'restrictfilenames': True,
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info_dict)
    
    return Path(filepath)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download YouTube video to inbox.")
    parser.add_argument("url", help="YouTube video URL")
    args = parser.parse_args()

    try:
        downloaded_file = download_youtube_video(args.url, INBOX_DIR)
        print(downloaded_file)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
