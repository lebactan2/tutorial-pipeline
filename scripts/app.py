import os
import subprocess
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import json
import dotenv

app = Flask(__name__, static_folder="../ui")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_PATH = CONFIG_DIR / ".env"
SYSTEM_JSON_PATH = CONFIG_DIR / "system.json"
VOICE_REFS_DIR = PROJECT_ROOT / "voice_refs"
INBOX_DIR = PROJECT_ROOT / "inbox"

VOICE_REFS_DIR.mkdir(exist_ok=True)
INBOX_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/<path:path>")
def static_files(path):
    return app.send_static_file(path)

@app.route("/api/videos")
def list_videos():
    output_dir = PROJECT_ROOT / "output"
    videos = []
    if output_dir.exists():
        for file in output_dir.glob("*_draft_*.mp4"):
            basename = file.name.split("_draft_")[0]
            if basename not in [v['id'] for v in videos]:
                videos.append({
                    "id": basename,
                    "local_draft": f"{basename}_draft_local.mp4" if (output_dir / f"{basename}_draft_local.mp4").exists() else None,
                    "cloud_draft": f"{basename}_draft_cloud.mp4" if (output_dir / f"{basename}_draft_cloud.mp4").exists() else None,
                    "srt": f"{basename}.srt" if (output_dir / f"{basename}.srt").exists() else None,
                })
    return jsonify(videos)

@app.route("/api/settings", methods=["GET", "POST"])
def manage_settings():
    if request.method == "GET":
        env_vars = dotenv.dotenv_values(ENV_PATH)
        system_config = {}
        if SYSTEM_JSON_PATH.exists():
            with open(SYSTEM_JSON_PATH, "r", encoding="utf-8-sig") as f:
                system_config = json.load(f)
        
        return jsonify({
            "ANTHROPIC_API_KEY": env_vars.get("ANTHROPIC_API_KEY", ""),
            "ANTIGRAVITY_API_KEY": env_vars.get("ANTIGRAVITY_API_KEY", ""),
            "MINIMAX_API_KEY": env_vars.get("MINIMAX_API_KEY", ""),
            "OPENROUTER_API_KEY": env_vars.get("OPENROUTER_API_KEY", ""),
            "CUSTOM_API_BASE_URL": env_vars.get("CUSTOM_API_BASE_URL", ""),
            "CUSTOM_API_KEY": env_vars.get("CUSTOM_API_KEY", ""),
            "llm_provider": system_config.get("llm_provider", "claude"),
            "openrouter_model": system_config.get("openrouter_model", "openai/gpt-4o"),
            "custom_model": system_config.get("custom_model", "")
        })
    else:
        data = request.json
        # Write .env
        dotenv.set_key(ENV_PATH, "ANTHROPIC_API_KEY", data.get("ANTHROPIC_API_KEY", ""))
        dotenv.set_key(ENV_PATH, "ANTIGRAVITY_API_KEY", data.get("ANTIGRAVITY_API_KEY", ""))
        dotenv.set_key(ENV_PATH, "MINIMAX_API_KEY", data.get("MINIMAX_API_KEY", ""))
        dotenv.set_key(ENV_PATH, "OPENROUTER_API_KEY", data.get("OPENROUTER_API_KEY", ""))
        dotenv.set_key(ENV_PATH, "CUSTOM_API_BASE_URL", data.get("CUSTOM_API_BASE_URL", ""))
        dotenv.set_key(ENV_PATH, "CUSTOM_API_KEY", data.get("CUSTOM_API_KEY", ""))
        
        # Write system.json
        system_config = {}
        if SYSTEM_JSON_PATH.exists():
            with open(SYSTEM_JSON_PATH, "r", encoding="utf-8-sig") as f:
                system_config = json.load(f)
                
        system_config["llm_provider"] = data.get("llm_provider", "claude")
        system_config["openrouter_model"] = data.get("openrouter_model", "openai/gpt-4o")
        system_config["custom_model"] = data.get("custom_model", "")
        
        with open(SYSTEM_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(system_config, f, indent=4)
            
        return jsonify({"status": "Settings saved"})

@app.route("/api/voices", methods=["GET", "POST"])
def manage_voices():
    if request.method == "GET":
        voices = sorted([f.name for f in VOICE_REFS_DIR.glob("*.wav")] + [f.name for f in VOICE_REFS_DIR.glob("*.mp3")])
        return jsonify(voices)
    else:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        if file and file.filename.lower().endswith(('.wav', '.mp3')):
            filename = secure_filename(file.filename)
            file.save(VOICE_REFS_DIR / filename)
            return jsonify({"status": "Voice uploaded successfully", "filename": filename})
        return jsonify({"error": "Invalid file type. Must be .wav or .mp3"}), 400

@app.route("/api/upload_transcript", methods=["POST"])
def upload_transcript():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file and file.filename.endswith(('.srt', '.json')):
        filename = secure_filename(file.filename)
        path = INBOX_DIR / filename
        file.save(path)
        return jsonify({"status": "Transcript uploaded successfully", "transcript_path": str(path)})
    return jsonify({"error": "Invalid file type. Must be .srt or .json"}), 400

@app.route("/api/check_video", methods=["POST"])
def check_video():
    data = request.json
    video_source = data.get("video_source")
    if not video_source:
        return jsonify({"error": "No video source provided"}), 400
    
    try:
        if video_source.startswith("http"):
            result = subprocess.run(["python", str(PROJECT_ROOT / "scripts" / "get_video_info.py"), video_source], capture_output=True, text=True, check=True)
            basename = result.stdout.strip()
        else:
            basename = Path(video_source).stem
            
        transcript_exists = (PROJECT_ROOT / "working" / f"{basename}.transcript.json").exists()
        return jsonify({"basename": basename, "transcript_exists": transcript_exists})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/run", methods=["POST"])
def run_pipeline():
    data = request.json
    video_source = data.get("video_source")
    transcript_path = data.get("transcript_path")
    voice_ref = data.get("voice_ref")
    skip_transcribe = data.get("skip_transcribe", False)
    force_transcribe = data.get("force_transcribe", False)
    target_language = data.get("target_language", "original")
    voice_engine = data.get("voice_engine", "auto")
    
    if not video_source:
        return jsonify({"error": "No video source provided"}), 400
    
    def run_script(v_source, t_path, voice, skip_t, force_t, language, engine):
        script_path = PROJECT_ROOT / "scripts" / "run.ps1"
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
        
        if v_source.startswith("http"):
            cmd.extend(["-YoutubeUrl", v_source])
        else:
            cmd.extend(["-VideoPath", v_source])
            
        if t_path:
            cmd.extend(["-TranscriptPath", t_path])
        if voice:
            cmd.extend(["-VoiceRef", str(VOICE_REFS_DIR / voice)])
        if skip_t:
            cmd.append("-SkipTranscribe")
        if force_t:
            cmd.append("-ForceTranscribe")
        cmd.extend(["-TargetLanguage", language])
        cmd.extend(["-VoiceEngine", engine])
            
        subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        
    threading.Thread(
        target=run_script,
        args=(video_source, transcript_path, voice_ref, skip_transcribe, force_transcribe, target_language, voice_engine),
    ).start()
    return jsonify({"status": "Pipeline started in background"})

@app.route("/files/<folder>/<path:filename>")
def serve_file(folder, filename):
    if folder not in ["output", "working", "inbox"]:
        return "Unauthorized", 403
    return send_from_directory(PROJECT_ROOT / folder, filename)

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=debug)
