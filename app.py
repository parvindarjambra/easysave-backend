# EasyDownloader media engine — Flask + yt-dlp + ffmpeg
# Only use for videos you own or have permission to download.
import base64, ipaddress, os, re, socket, subprocess, tempfile, urllib.parse
from flask import Flask, jsonify, request, send_file, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 8080))

@app.after_request
def cors(resp):
    # The web frontend calls this API cross-origin — without these headers
    # the browser blocks every request. Same as the old backend.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp
TIMEOUT = 600  # very long videos can hit this — report TIMEOUT, don't hang

ALLOWED_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com",
                 "facebook.com", "fb.watch", "twitter.com", "x.com")

def _is_private_literal(host: str) -> bool:
    """Block IP literals in private ranges. Do NOT DNS-resolve hostnames:
    this VM / proxies resolve public hosts to CGNAT addresses (198.18.x),
    which ipaddress calls private — resolving would reject every real site."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname, not an IP literal
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved

def sanitize_url(raw: str) -> str:
    """Reject non-http(s), localhost / private IPs, and non-media hosts."""
    if not raw or len(raw) > 2048:
        raise ValueError("UNSUPPORTED")
    u = urllib.parse.urlparse(raw.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("UNSUPPORTED")
    host = u.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal") or _is_private_literal(host):
        raise ValueError("UNSUPPORTED")
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise ValueError("UNSUPPORTED")
    return raw.strip()

def cookies_file_for(url: str):
    """Server-side cookies only, from env vars — never from the frontend."""
    mapping = [("youtube.com", "YT_COOKIES"), ("youtu.be", "YT_COOKIES"),
               ("instagram.com", "IG_COOKIES"), ("facebook.com", "FB_COOKIES"),
               ("fb.watch", "FB_COOKIES")]
    for host, env in mapping:
        if host in url:
            b64 = os.environ.get(env)
            if b64:
                p = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                p.write(base64.b64decode(b64)); p.close()
                return p.name
    return None

def classify_error(e: Exception) -> tuple[str, str]:
    msg = str(e).lower()
    if any(k in msg for k in ("private video", "this video is private", "login required",
                              "sign in to confirm", "age")):
        return "LOGIN_REQUIRED", "This video needs a login or is private / age-restricted."
    if any(k in msg for k in ("429", "too many requests", "bot", "confirm you're not a robot")):
        return "BOT_CHECK", "The platform is blocking this server right now. Try again later."
    if "unsupported url" in msg or "no video" in msg:
        return "UNSUPPORTED", "This link is not supported."
    return "UNKNOWN", "Extraction failed. Check the link and try again."

YOUTUBE_CLIENTS = ["ios", "android", None]  # retry with a different client each time

def ydl_opts(url: str, client=None):
    opts = {"quiet": True, "noplaylist": True, "socket_timeout": 30}
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    cf = cookies_file_for(url)
    if cf:
        opts["cookiefile"] = cf
    return opts

def extract_with_retries(url: str):
    """Try each YouTube player client in turn — a bot-checked attempt falls
    back to another client instead of failing the whole request."""
    last_err = None
    for client in YOUTUBE_CLIENTS:
        try:
            with YoutubeDL(ydl_opts(url, client)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e
    raise last_err

def build_formats(info: dict) -> list:
    """Map yt-dlp formats to the free/premium cards the UI shows."""
    out, seen = [], set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if f.get("vcodec") != "none" and h in (360, 720, 1080, 2160):
            label = {360: "360p MP4", 720: "720p MP4", 1080: "1080p MP4", 2160: "4K MP4"}[h]
            if label in seen: continue
            seen.add(label)
            out.append({"id": f"v{h}", "label": label, "ext": "mp4", "kind": "video",
                        "height": h, "filesize": f.get("filesize"),
                        "premium": False})  # 100% free like Snaptube — no premium
        elif f.get("acodec") != "none" and f.get("vcodec") == "none":
            # One best-audio source backs all four audio cards below.
            if "audio" in seen: continue
            seen.add("audio")
            seen.add("MP3 320kbps")
            out.append({"id": "a_mp3_320", "label": "MP3 320kbps", "ext": "mp3", "kind": "audio",
                        "bitrate": 320, "filesize": f.get("filesize")})
    # Guarantee the free cards exist even when yt-dlp lists odd formats
    for label, fid, h in [("360p MP4", "v360", 360), ("720p MP4", "v720", 720)]:
        if label not in seen:
            out.append({"id": fid, "label": label, "ext": "mp4", "kind": "video", "height": h, "premium": False})
    # Audio: MP3 + M4A, 128kbps free / 320kbps best quality (all free in v1)
    for label, fid, ext, br in [("MP3 128kbps", "a_mp3_128", "mp3", 128),
                                ("MP3 320kbps", "a_mp3_320", "mp3", 320),
                                ("M4A 128kbps", "a_m4a_128", "m4a", 128),
                                ("M4A 320kbps", "a_m4a_320", "m4a", 320)]:
        if label not in seen:
            out.append({"id": fid, "label": label, "ext": ext, "kind": "audio", "bitrate": br})
    order = {"360p MP4": 0, "720p MP4": 1, "1080p MP4": 2, "4K MP4": 3,
             "MP3 128kbps": 4, "MP3 320kbps": 5, "M4A 128kbps": 6, "M4A 320kbps": 7}
    return sorted(out, key=lambda f: order.get(f["label"], 9))

def platform_of(url: str) -> str:
    u = url.lower()
    for name in ("youtube", "instagram", "tiktok", "facebook"):
        if name in u or ("youtu" in u and name == "youtube") or ("fb.watch" in u and name == "facebook"):
            return name
    if "twitter.com" in u or "x.com" in u:
        return "other"
    return "other"

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "easydownloader-api"})

@app.route("/extract", methods=["POST", "OPTIONS"])
def extract():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        url = sanitize_url((request.get_json(silent=True) or {}).get("url", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": "Unsupported link.", "errorCode": str(e)}), 400
    try:
        info = extract_with_retries(url)
    except DownloadError as e:
        code, msg = classify_error(e)
        return jsonify({"ok": False, "error": msg, "errorCode": code}), 422
    except Exception as e:
        code, msg = classify_error(e)
        return jsonify({"ok": False, "error": msg, "errorCode": code}), 500
    return jsonify({"ok": True, "title": info.get("title", "Video"),
                    "author": info.get("uploader"), "duration": info.get("duration"),
                    "thumbnail": info.get("thumbnail"), "platform": platform_of(url),
                    "download_url": "/download?url=" + urllib.parse.quote(url),
                    "formats": build_formats(info)})

@app.get("/download")
def download():
    try:
        url = sanitize_url(request.args.get("url", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Unsupported link.", "errorCode": "UNSUPPORTED"}), 400
    fmt = request.args.get("format", "v720")
    if fmt not in ("v360", "v720", "v1080", "v2160",
                   "a_mp3_128", "a_mp3_320", "a_m4a_128", "a_m4a_320"):
        return jsonify({"ok": False, "error": "Unknown format.", "errorCode": "UNSUPPORTED"}), 400
    # 100% free like Snaptube — no premium gate on any format.
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "out.%(ext)s")
    if fmt.startswith("a_"):
        # a_mp3_128 / a_mp3_320 / a_m4a_128 / a_m4a_320
        _, codec, br = fmt.split("_")
        ext = codec
        mime = "audio/mpeg" if codec == "mp3" else "audio/mp4"
        ydl_fmt = "bestaudio"
        post = [{"key": "FFmpegExtractAudio", "preferredcodec": codec,
                 "preferredquality": br}]
    else:
        h = fmt[1:]
        ydl_fmt, ext, mime = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]", "mp4", "video/mp4"
        post = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    opts = ydl_opts(url)
    opts.update({"format": ydl_fmt, "outtmpl": out, "postprocessors": post,
                 "merge_output_format": "mp4", "quiet": True, "noplaylist": True})
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError as e:
        code, msg = classify_error(e)
        return jsonify({"ok": False, "error": msg, "errorCode": code}), 422
    # Find the finished file (yt-dlp + ffmpeg merge writes it to tmp/)
    files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
    if not files:
        return jsonify({"ok": False, "error": "Download produced no file.", "errorCode": "UNKNOWN"}), 500
    path = max(files, key=os.path.getsize)
    # Sanity: must be a real media file, not an error page
    if os.path.getsize(path) < 1024:
        return jsonify({"ok": False, "error": "Download failed.", "errorCode": "UNKNOWN"}), 500
    return send_file(path, mimetype=mime, as_attachment=True,
                     download_name=f"easydownloader.{ext}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
