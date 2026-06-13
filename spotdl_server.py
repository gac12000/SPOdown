"""
Servidor backend - Descàrregador universal de música
Spotify → spotdl | Resta → yt-dlp
"""

import os
import sys
import uuid
import shutil
import subprocess
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory

app = Flask(__name__, static_folder='static')

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

jobs = {}  # {job_id: {status, progress, status_text, filenames, error, log}}


# ── Detecció de plataforma ──────────────────────────────────────────────────

PLATFORM_MAP = {
    "spotify.com":      ("spotify",   "#1DB954"),
    "soundcloud.com":   ("soundcloud","#FF5500"),
    "mixcloud.com":     ("mixcloud",  "#5000FF"),
    "bandcamp.com":     ("bandcamp",  "#1DA0C3"),
    "youtube.com":      ("youtube",   "#FF0000"),
    "youtu.be":         ("youtube",   "#FF0000"),
    "music.youtube.com":("youtube",   "#FF0000"),
    "tiktok.com":       ("tiktok",    "#010101"),
    "twitter.com":      ("twitter",   "#1D9BF0"),
    "x.com":            ("twitter",   "#1D9BF0"),
    "instagram.com":    ("instagram", "#C13584"),
    "vimeo.com":        ("vimeo",     "#1AB7EA"),
    "deezer.com":       ("deezer",    "#A238FF"),
    "twitch.tv":        ("twitch",    "#9146FF"),
}

def detect_platform(url: str) -> tuple[str, str]:
    for domain, info in PLATFORM_MAP.items():
        if domain in url:
            return info
    return ("other", "#888888")


# ── Cerca executables ───────────────────────────────────────────────────────

def find_tool(name: str) -> list:
    if shutil.which(name):
        return [name]
    scripts_win = Path(sys.executable).parent / "Scripts" / f"{name}.exe"
    if scripts_win.exists():
        return [str(scripts_win)]
    scripts_unix = Path(sys.executable).parent / name
    if scripts_unix.exists():
        return [str(scripts_unix)]
    return [sys.executable, "-m", name]


# ── Descàrrega ──────────────────────────────────────────────────────────────

def run_download(job_id: str, url: str, output_dir: Path):
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 5
    jobs[job_id]["log"] = []

    platform, _ = detect_platform(url)

    try:
        if platform == "spotify":
            _run_spotdl(job_id, url, output_dir)
        else:
            _run_ytdlp(job_id, url, output_dir)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def _run_spotdl(job_id, url, output_dir):
    cmd = find_tool("spotdl") + [
        url,
        "--output", str(output_dir),
        "--format", "mp3",
        "--bitrate", "320k",
    ]
    _execute(job_id, cmd, output_dir, "*.mp3")


def _run_ytdlp(job_id, url, output_dir):
    cmd = find_tool("yt-dlp") + [
        url,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--add-metadata",
        "-o", str(output_dir / "%(title)s.%(ext)s"),
        "--no-playlist",   # per defecte no baixa playlists senceres
    ]
    # Per a playlists/canals, eliminar --no-playlist si la URL ho indica
    if any(x in url for x in ["/playlist", "/sets/", "/album/", "/channel/", "list="]):
        cmd = [c for c in cmd if c != "--no-playlist"]

    _execute(job_id, cmd, output_dir, "*.mp3")


def _execute(job_id, cmd, output_dir, glob_pattern):
    jobs[job_id]["log"].append(f"$ {' '.join(cmd)}")

    files_before = set(output_dir.glob(glob_pattern))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )

    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        jobs[job_id]["log"].append(line)
        low = line.lower()
        if any(w in low for w in ["fetching", "gathering", "searching"]):
            jobs[job_id]["progress"] = 15
            jobs[job_id]["status_text"] = "Obtenint informació..."
        elif "downloading" in low:
            jobs[job_id]["progress"] = 45
            jobs[job_id]["status_text"] = "Descarregant àudio..."
        elif any(w in low for w in ["converting", "processing", "ffmpeg", "merging"]):
            jobs[job_id]["progress"] = 75
            jobs[job_id]["status_text"] = "Convertint a MP3..."
        elif any(w in low for w in ["downloaded", "finished", "100%"]):
            jobs[job_id]["progress"] = 90
            jobs[job_id]["status_text"] = "Finalitzant..."

    process.wait()

    if process.returncode not in (0, 1):  # yt-dlp pot retornar 1 en warnings
        jobs[job_id]["status"] = "error"
        last_lines = "\n".join(jobs[job_id]["log"][-5:])
        jobs[job_id]["error"] = f"Error en la descàrrega.\n\n{last_lines}"
        return

    files_after = set(output_dir.glob(glob_pattern))
    # Also check other audio formats yt-dlp might produce
    for ext in ["*.m4a", "*.ogg", "*.opus", "*.flac", "*.wav"]:
        files_after |= set(output_dir.glob(ext))

    new_files = list(files_after - files_before)
    if not new_files:
        new_files = list(output_dir.glob("*.*"))
        new_files = [f for f in new_files if f.suffix.lower() in
                     {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"}]

    if not new_files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No s'ha trobat cap fitxer descarregat. Comprova el log."
        return

    filenames = [f.name for f in sorted(new_files)]
    jobs[job_id]["status"] = "done"
    jobs[job_id]["progress"] = 100
    jobs[job_id]["filenames"] = filenames
    jobs[job_id]["status_text"] = f"{len(filenames)} fitxer(s) descarregat(s)"


# ── API ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL buida"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Introdueix una URL completa (https://...)"}), 400

    platform, color = detect_platform(url)
    job_id = str(uuid.uuid4())
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "filenames": [],
        "error": None,
        "status_text": "En cua...",
        "platform": platform,
        "color": color,
        "log": [],
    }

    threading.Thread(target=run_download, args=(job_id, url, job_dir), daemon=True).start()
    return jsonify({"job_id": job_id, "platform": platform, "color": color})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Feina no trobada"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>/<path:filename>")
def get_file(job_id, filename):
    job_dir = DOWNLOAD_DIR / job_id
    filepath = job_dir / filename
    if not filepath.exists():
        return jsonify({"error": "Fitxer no trobat"}), 404
    return send_file(str(filepath), as_attachment=True, download_name=filename)


@app.route("/api/debug")
def debug_info():
    return jsonify({
        "python": sys.executable,
        "spotdl": find_tool("spotdl"),
        "yt-dlp": find_tool("yt-dlp"),
        "ffmpeg": shutil.which("ffmpeg"),
    })
