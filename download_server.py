#!/usr/bin/env python3
"""
Combined download + log server for the downloader website.

- Downloads videos via yt-dlp
- Uploads to Google Drive via service account
- Logs activity to Telegram group
- Tracks progress via job IDs (polled by website)

Port: 8081
Endpoints:
  GET  /                    - landing page
  GET  /ping                - health check
  POST /log                 - log event to TG (visit tracking)
  POST /api/download         - start a download job, returns job_id
  GET  /api/status/<job_id>  - poll job progress/result
"""
import json
import os
import time
import uuid
import subprocess
import threading
import tempfile
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOG_CHAT_ID = os.environ.get("LOG_CHAT_ID", "")
GDRIVE_ID = os.environ.get("GDRIVE_ID", "")
GDRIVE_SA = os.environ.get("GDRIVE_SA", "")
PORT = int(os.environ.get("DOWNLOAD_PORT", "8081"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# In-memory job storage
jobs = {}
jobs_lock = threading.Lock()

# Clean up old jobs (older than 1 hour)
def cleanup_jobs():
    while True:
        time.sleep(300)
        now = time.time()
        with jobs_lock:
            old = [jid for jid, j in jobs.items() if now - j.get("created_at", 0) > 3600]
            for jid in old:
                del jobs[jid]
                print(f"[CLEANUP] Removed old job {jid}")

LANDING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Download Server</title></head>
<body style="font-family:monospace;background:#0f0f0f;color:#22c55e;padding:40px;text-align:center;">
<h1>Download Server Running</h1>
<p>POST /api/download - start a download</p>
<p>GET /api/status/:id - check progress</p>
<p>GET /ping - health check</p>
</body></html>"""


def detect_platform(url):
    u = url.lower()
    platforms = [
        (["youtube.com", "youtu.be", "m.youtube.com"], "YouTube"),
        (["instagram.com", "instagr.am"], "Instagram"),
        (["tiktok.com"], "TikTok"),
        (["facebook.com", "fb.watch", "m.facebook.com"], "Facebook"),
        (["twitter.com", "x.com", "t.co"], "Twitter / X"),
        (["reddit.com", "redd.it"], "Reddit"),
        (["vimeo.com"], "Vimeo"),
        (["dailymotion.com", "dai.ly"], "Dailymotion"),
        (["soundcloud.com"], "SoundCloud"),
        (["pinterest.com", "pin.it"], "Pinterest"),
        (["streamable.com"], "Streamable"),
        (["twitch.tv"], "Twitch"),
    ]
    for domains, name in platforms:
        for d in domains:
            if d in u:
                return name
    return "Unknown"


def send_tg(text):
    if not BOT_TOKEN or not LOG_CHAT_ID:
        print(f"[TG] (not configured) {text[:80]}")
        return False
    try:
        data = json.dumps({
            "chat_id": LOG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(TG_API, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 200
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return False


def upload_to_gdrive(filepath, filename, job_id):
    """Upload file to Google Drive folder using service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    sa_info = json.loads(GDRIVE_SA)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds, static_discovery=False)

    file_metadata = {"name": filename, "parents": [GDRIVE_ID]}
    media = MediaFileUpload(filepath, resumable=True, chunksize=10 * 1024 * 1024)
    request = service.files().create(
        body=file_metadata, media_body=media, fields="id,webViewLink"
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["message"] = f"Uploading to Google Drive... {pct}%"

    # Make file publicly viewable
    service.permissions().create(
        fileId=response["id"],
        body={"role": "reader", "type": "anyone"},
    ).execute()

    link = response.get("webViewLink", f"https://drive.google.com/file/d/{response['id']}/view")
    return link


def process_download(job_id, url):
    """Background thread: download with yt-dlp, upload to GDrive."""
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["message"] = "Starting download..."
        jobs[job_id]["progress"] = 0

    tmpdir = tempfile.mkdtemp(prefix=f"dl_{job_id}_")
    try:
        output_template = os.path.join(tmpdir, "%(title).80s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--format", "best[ext=mp4][height<=1080]/best[ext=webm][height<=1080]/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--newline",
            "--no-warnings",
            "--output", output_template,
            url,
        ]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=tmpdir
        )

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if "[download]" in line and "%" in line:
                try:
                    pct_str = line.split("%")[0].split()[-1]
                    pct = float(pct_str)
                    with jobs_lock:
                        if job_id in jobs:
                            jobs[job_id]["progress"] = min(pct, 95)
                            jobs[job_id]["message"] = f"Downloading... {pct:.0f}%"
                except (ValueError, IndexError):
                    pass
            elif "Merging" in line or "[Merger]" in line:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["message"] = "Merging audio and video..."
            elif "[ExtractAudio]" in line:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["message"] = "Extracting audio..."

        proc.wait(timeout=600)

        if proc.returncode != 0:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["message"] = "Download failed. The video may be private, region-locked, or unsupported."
            send_tg(f"\u274c <b>Download Failed</b>\n\nURL: <code>{url[:100]}</code>\nJob: <code>{job_id}</code>")
            return

        files = [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl"))]
        if not files:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["message"] = "No file was downloaded."
            return

        filepath = os.path.join(tmpdir, files[0])
        filesize = os.path.getsize(filepath)
        size_mb = filesize / (1024 * 1024)

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "uploading"
                jobs[job_id]["message"] = f"Uploading to Google Drive ({size_mb:.1f} MB)..."
                jobs[job_id]["progress"] = 95

        gdrive_link = ""
        if GDRIVE_SA and GDRIVE_ID:
            gdrive_link = upload_to_gdrive(filepath, files[0], job_id)

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["message"] = "Download complete!"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["gdrive_link"] = gdrive_link
                jobs[job_id]["filename"] = files[0]
                jobs[job_id]["filesize"] = filesize

        platform = detect_platform(url)
        send_tg(
            f"\u2705 <b>Download Complete</b>\n\n"
            f"File: {files[0]}\n"
            f"Size: {size_mb:.1f} MB\n"
            f"Platform: {platform}\n"
            f"URL: <code>{url[:100]}</code>\n"
            f"GDrive: {gdrive_link if gdrive_link else 'N/A (not configured)'}"
        )

    except subprocess.TimeoutExpired:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = "Download timed out (10 min limit)."
    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = f"Error: {str(e)[:200]}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class DownloadHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ping":
            self._send_json(200, {"ok": True, "ts": int(time.time())})
        elif path == "/" or path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self._cors()
            self.end_headers()
            self.wfile.write(LANDING_HTML.encode())
        elif path.startswith("/api/status/"):
            job_id = path.replace("/api/status/", "")
            with jobs_lock:
                job = jobs.get(job_id)
            if job:
                safe_job = {k: v for k, v in job.items() if k != "url"}
                self._send_json(200, safe_job)
            else:
                self._send_json(404, {"status": "error", "message": "job not found"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/download":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                data = json.loads(body)
                url = data.get("url", "").strip()
            except Exception as e:
                self._send_json(400, {"error": f"bad request: {e}"})
                return

            if not url or len(url) < 10:
                self._send_json(400, {"error": "valid URL required"})
                return
            if not url.startswith(("http://", "https://")):
                self._send_json(400, {"error": "URL must start with http:// or https://"})
                return

            job_id = str(uuid.uuid4())[:8]
            platform = detect_platform(url)

            with jobs_lock:
                jobs[job_id] = {
                    "status": "queued",
                    "message": "Download queued...",
                    "progress": 0,
                    "platform": platform,
                    "gdrive_link": "",
                    "filename": "",
                    "filesize": 0,
                    "created_at": time.time(),
                }

            t = threading.Thread(target=process_download, args=(job_id, url), daemon=True)
            t.start()

            ip = self.client_address[0] if self.client_address else "unknown"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            send_tg(
                f"\u2b07\ufe0f <b>Download Request</b>\n\n"
                f"Time: {ts} UTC\n"
                f"IP: <code>{ip}</code>\n"
                f"Platform: {platform}\n"
                f"URL: <code>{url[:100]}</code>\n"
                f"Job: <code>{job_id}</code>"
            )

            self._send_json(200, {"job_id": job_id, "status": "queued"})

        elif path == "/log":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                data = json.loads(body)
            except Exception as e:
                self._send_json(400, {"error": f"bad request: {e}"})
                return

            event = data.get("event", "unknown")
            extra = data.get("extra", "")
            ip = self.client_address[0] if self.client_address else "unknown"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

            if event == "visit":
                msg = f"\U0001F441\ufe0f <b>New Visit</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"
                if extra:
                    msg += f"\n{extra}"
            else:
                msg = f"\U0001F4CB <b>{event}</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"

            send_tg(msg)
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ct = threading.Thread(target=cleanup_jobs, daemon=True)
    ct.start()

    server = HTTPServer(("0.0.0.0", PORT), DownloadHandler)
    print(f"Download server listening on :{PORT}")
    print(f"  BOT_TOKEN: {'set' if BOT_TOKEN else 'MISSING'}")
    print(f"  LOG_CHAT_ID: {'set' if LOG_CHAT_ID else 'MISSING'}")
    print(f"  GDRIVE_ID: {'set' if GDRIVE_ID else 'MISSING'}")
    print(f"  GDRIVE_SA: {'set' if GDRIVE_SA else 'MISSING'}")
    send_tg(
        f"\U0001F7E2 <b>Download Server Started</b>\n\n"
        f"Listening on port {PORT}\n"
        f"GDrive upload: {'enabled' if GDRIVE_SA else 'disabled'}\n"
        f"Ready to process downloads."
    )
    server.serve_forever()
