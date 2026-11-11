"""EasySave extractor backend - Fly.io / Docker.
Only use for videos you own or have permission to download.
Endpoints:
  GET  /health
  POST /extract  {"url": "..."} -> {"title":..., "formats":[...], "download_url": "..."}
  GET  /download?url=... -> streams full merged HD (<=1080p) mp4 with sound via yt-dlp + ffmpeg
"""
import base64, json, os, subprocess, time
from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)
FORMAT = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
# YouTube intermittently answers Fly's egress IP with "Sign in to confirm
# you're not a bot" on the web player client, then succeeds on retry.
# Rotate the player client across attempts so a challenged attempt falls
# back to android/ios instead of failing the whole request.
YOUTUBE_CLIENTS = [None, "android", "ios"]

def diagnose(stderr):
    """Short human-readable reason from yt-dlp's stderr (no secrets in it)."""
    s = (stderr or "").lower()
    if "instagram api is not granting access" in s or "empty media response" in s:
        return "Instagram requires login for this post — it does not serve the media to a logged-out downloader."
    if "facebook" in s and ("login" in s or "log in" in s):
        return "Facebook requires login for this video."
    if "http error 429" in s or "too many requests" in s:
        return "The platform is rate-limiting requests right now — try again later."
    if "sign in to confirm you're not a bot" in s or "confirm you're not a bot" in s:
        return "YouTube asked for a sign-in check — try again later."
    if "private video" in s:
        return "This video is private."
    if "video unavailable" in s:
        return "This video is unavailable or removed."
    if "age" in s and ("confirm" in s or "restricted" in s):
        return "This video is age-restricted."
    return "Could not extract this video. It may be private, age-restricted, or blocked by the platform."

_COOKIES_CACHE = {}
def _cookies_file(env_name, filename):
    """Session cookies from an env var (base64 of a Netscape cookies.txt).
    Written once to /tmp, never to git. Returns the path, or None."""
    if env_name in _COOKIES_CACHE:
        return _COOKIES_CACHE[env_name] or None
    data = os.environ.get(env_name, "")
    if not data:
        _COOKIES_CACHE[env_name] = False
        return None
    try:
        raw = base64.b64decode(data)
        path = "/tmp/" + filename
        with open(path, "wb") as f:
            f.write(raw)
        os.chmod(path, 0o600)
        _COOKIES_CACHE[env_name] = path
    except Exception:
        _COOKIES_CACHE[env_name] = False
        return None
    return _COOKIES_CACHE[env_name]

def cookies_for(url):
    """Instagram cookies for instagram.com, Facebook cookies for
    facebook.com / fb.watch. Each site needs its own login session."""
    if "instagram.com" in url:
        return _cookies_file("IG_COOKIES", "ig_cookies.txt")
    if "facebook.com" in url or "fb.watch" in url:
        return _cookies_file("FB_COOKIES", "fb_cookies.txt")
    return None

def run_ytdlp(base_cmd, url, attempts=3, timeout=90):
    """Run yt-dlp, retrying with a different YouTube player client each time.
    Returns the first successful CompletedProcess, else the last result (or None)."""
    last = None
    for i in range(attempts):
        cmd = list(base_cmd)
        client = YOUTUBE_CLIENTS[i % len(YOUTUBE_CLIENTS)]
        if client:
            cmd += ["--extractor-args", "youtube:player_client=%s" % client]
        c = cookies_for(url)
        if c:
            cmd += ["--cookies", c]
        cmd.append(url)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = None
        else:
            if out.returncode == 0:
                return out
            last = out
        if i < attempts - 1:
            time.sleep(2)
    return last

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "easysave-backend"})

@app.route("/extract", methods=["POST", "OPTIONS"])
def extract():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "Valid video URL required"}), 400
    cmd = ["yt-dlp", "-J", "--no-playlist", "--format", FORMAT]
    out = run_ytdlp(cmd, url, attempts=3, timeout=90)
    if out is None:
        return jsonify({"error": "Extractor timed out, try again"}), 504
    if out.returncode != 0:
        return jsonify({"error": "Could not extract this video. It may be private, age-restricted, or blocked by the platform.", "detail": diagnose(out.stderr)}), 502
    try:
        info = json.loads(out.stdout)
    except Exception:
        return jsonify({"error": "Extractor returned an unreadable result"}), 502
    formats = []
    for f in info.get("formats", []) or []:
        if f.get("url") and f.get("ext") in ("mp4", "webm"):
            q = f"{f.get('height')}p" if f.get("height") else "Original"
            formats.append({"quality": q, "ext": f["ext"], "url": f["url"]})
    seen = set(); clean = []
    for f in sorted(formats, key=lambda x: x["quality"], reverse=True):
        if f["quality"] not in seen:
            seen.add(f["quality"]); clean.append(f)
    download_url = "/download?url=" + url
    return jsonify({"title": info.get("title"), "formats": clean[:5], "direct_url": info.get("url"), "download_url": download_url})

@app.route("/download")
def download():
    url = (request.args.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "Valid video URL required"}), 400
    # Download to a temp file first, then serve it. Streaming yt-dlp to stdout
    # with a merge produces a broken TS stream, not a full MP4 with sound.
    import os, uuid
    from flask import send_file, after_this_request
    tmpdir = "/tmp/easysave"
    os.makedirs(tmpdir, exist_ok=True)
    outtmpl = os.path.join(tmpdir, str(uuid.uuid4()))
    cmd = ["yt-dlp", "-f", FORMAT, "--merge-output-format", "mp4", "-o", outtmpl + ".%(ext)s", "--no-playlist"]
    proc = run_ytdlp(cmd, url, attempts=3, timeout=600)
    if proc is None:
        return jsonify({"error": "Download timed out, try a shorter video"}), 504
    if proc.returncode != 0:
        return jsonify({"error": "Could not download this video. It may be private, age-restricted, or blocked by the platform.", "detail": diagnose(proc.stderr)}), 502
    files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.startswith(os.path.basename(outtmpl))]
    if not files:
        return jsonify({"error": "Download produced no file"}), 502
    path = files[0]
    @after_this_request
    def cleanup(response):
        try: os.remove(path)
        except Exception: pass
        return response
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name="EasySave_video.mp4")

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
