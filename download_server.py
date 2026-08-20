#!/usr/bin/env python3
"""
download_server.py v4 — Full-featured download server with auth, tiers, admin, GDrive upload.
Changes from v3:
  - Guest downloads (no login required) with IP-based rate limiting (3 per 24h)
  - Rolling 24h window (not midnight reset) for all download quotas
  - Masked IP addresses (SHA-256 hash with salt) — privacy-preserving
  - Fixed CORS: echoes specific Origin header instead of * (critical for credentials)
  - Fixed cookies: SameSite=None instead of Lax (cross-origin auth works now)
  - Free tier daily_limit changed from 3 to 5
  - New endpoint: /api/guest-quota — returns remaining guest downloads
Env: BOT_TOKEN, LOG_CHAT_ID, GDRIVE_ID, GDRIVE_SA, DATABASE_URL,
     ADMIN_USERNAME, ADMIN_PASSWORD,
     BREVO_API_KEY, BREVO_SENDER_EMAIL, OWNER_EMAIL, IP_SALT (optional)
Port: 8081
"""
import os, json, time, uuid, hashlib, secrets, threading, subprocess, tempfile, shutil
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
import pymongo
from pymongo import MongoClient

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOG_CHAT_ID = os.environ.get("LOG_CHAT_ID", "")
GDRIVE_ID = os.environ.get("GDRIVE_ID", "")
GDRIVE_SA = os.environ.get("GDRIVE_SA", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")
IP_SALT = os.environ.get("IP_SALT", "ytdl_salt_2024_secure")
PORT = 8081
GUEST_DAILY_LIMIT = 3

TIERS = {
    "free":    {"max_bytes": 1073741824,    "daily_limit": 5,    "label": "Free"},
    "bronze":  {"max_bytes": 2147483648,    "daily_limit": 10,   "label": "Bronze"},
    "silver":  {"max_bytes": 5368709120,    "daily_limit": 25,   "label": "Silver"},
    "gold":    {"max_bytes": 10737418240,   "daily_limit": 50,   "label": "Gold"},
    "supreme": {"max_bytes": None,          "daily_limit": None, "label": "Supreme"},
}

GUEST_TIER = {"max_bytes": 1073741824, "daily_limit": GUEST_DAILY_LIMIT, "label": "Guest"}

# Platform access per tier — controls which sites each tier can download from
TIER_PLATFORMS = {
    "guest":   ["YouTube"],
    "free":    ["YouTube"],
    "bronze":  ["YouTube", "Instagram", "TikTok", "Facebook"],
    "silver":  ["YouTube", "Instagram", "TikTok", "Facebook", "Twitter/X", "Reddit", "Vimeo"],
    "gold":    ["YouTube", "Instagram", "TikTok", "Facebook", "Twitter/X", "Reddit", "Vimeo", "Dailymotion", "SoundCloud", "Pinterest"],
    "supreme": None,  # None = all platforms allowed
}

jobs = {}
jobs_lock = threading.Lock()
_db = None
_db_lock = threading.Lock()

def get_db():
    global _db
    if _db is not None: return _db
    with _db_lock:
        if _db is not None: return _db
        if not DATABASE_URL: print("[WARN] DATABASE_URL not set"); return None
        try:
            c = MongoClient(DATABASE_URL, serverSelectionTimeoutMS=10000)
            c.admin.command("ping")
            _db = c["ytdownload"]
            _db.sessions.create_index("expires_at", expireAfterSeconds=0)
            _db.sessions.create_index("token", unique=True)
            _db.users.create_index("username", unique=True)
            _db.downloads.create_index("username")
            _db.downloads.create_index("ip_hash")
            _db.downloads.create_index("created_at")
            _db.logs.create_index("created_at")
            print("[INFO] MongoDB connected")
        except Exception as e:
            print(f"[ERROR] MongoDB: {e}"); _db = None
        return _db

def mask_ip(ip):
    """Hash an IP address with salt for privacy-preserving storage."""
    return hashlib.sha256((ip + IP_SALT).encode()).hexdigest()[:32]

def hash_pw(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000, 32)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"

def verify_pw(pw, stored):
    try:
        _, iters, sh, hh = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(sh), int(iters), 32)
        return secrets.compare_digest(dk.hex(), hh)
    except: return False

def parse_cookies(hdr):
    c = {}
    if not hdr: return c
    for p in hdr.split(";"):
        if "=" in p:
            k, _, v = p.strip().partition("=")
            c[k.strip()] = v.strip()
    return c

def get_session_user(db, headers):
    cookies = parse_cookies(headers.get("Cookie", ""))
    tok = cookies.get("__Host-user_session") or cookies.get("user_session")
    if not tok: return None, False
    doc = db.sessions.find_one({"token": tok})
    if not doc or doc.get("expires_at", datetime.min.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc):
        return None, False
    u = db.users.find_one({"username": doc["username"]})
    return u, False

def get_session_admin(db, headers):
    cookies = parse_cookies(headers.get("Cookie", ""))
    tok = cookies.get("__Host-admin_session") or cookies.get("admin_session")
    if not tok: return None
    doc = db.sessions.find_one({"token": tok})
    if not doc or not doc.get("is_admin"): return None
    return doc

def create_session(db, username, is_admin=False):
    tok = str(uuid.uuid4())
    exp = datetime.now(timezone.utc) + timedelta(hours=24)
    db.sessions.insert_one({"token": tok, "username": username, "is_admin": is_admin, "expires_at": exp})
    return tok, exp

def count_downloads_24h(db, username):
    """Rolling 24h window: count downloads in the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return db.downloads.count_documents({"username": username, "created_at": {"$gte": cutoff}})

def count_guest_downloads_24h(db, ip_hash):
    """Rolling 24h window for guest (IP-based) downloads."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return db.downloads.count_documents({"ip_hash": ip_hash, "created_at": {"$gte": cutoff}})

def send_tg(text):
    if not BOT_TOKEN or not LOG_CHAT_ID: return
    try:
        data = urllib.parse.urlencode({"chat_id": LOG_CHAT_ID, "text": text, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[TG] {e}")

def send_email(subject, body_text):
    """Send notification email to owner via Brevo (sendinblue) API."""
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL or not OWNER_EMAIL:
        return False
    try:
        payload = json.dumps({
            "sender": {"email": BREVO_SENDER_EMAIL, "name": "YTDownload Server"},
            "to": [{"email": OWNER_EMAIL}],
            "subject": subject,
            "textContent": body_text
        }).encode()
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            method="POST"
        )
        req.add_header("api-key", BREVO_API_KEY)
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=15)
        print(f"[BREVO] Email sent: {subject} (status {resp.status})")
        return True
    except Exception as e:
        print(f"[BREVO] Email failed: {e}")
        return False

def notify(subject, tg_text=None, email_body=None):
    """Send both TG message and Brevo email notification. Falls back to either."""
    if tg_text:
        send_tg(tg_text)
    if email_body is None:
        email_body = tg_text or subject
    send_email(subject, email_body)

def detect_platform(url):
    u = url.lower()
    for domains, name in [(["youtube.com","youtu.be","m.youtube.com"],"YouTube"),(["instagram.com","instagr.am"],"Instagram"),(["tiktok.com"],"TikTok"),(["facebook.com","fb.watch","m.facebook.com"],"Facebook"),(["twitter.com","x.com","t.co"],"Twitter/X"),(["reddit.com","redd.it"],"Reddit"),(["vimeo.com"],"Vimeo"),(["dailymotion.com","dai.ly"],"Dailymotion"),(["soundcloud.com"],"SoundCloud"),(["pinterest.com","pin.it"],"Pinterest"),(["streamable.com"],"Streamable"),(["twitch.tv"],"Twitch")]:
        for d in domains:
            if d in u: return name
    return "Unknown"

def probe_video(url):
    try:
        p = subprocess.run(["yt-dlp","--dump-json","--no-warnings","--no-playlist",url], capture_output=True, text=True, timeout=120)
        if p.returncode != 0: return None, None
        d = json.loads(p.stdout)
        size = d.get("filesize") or d.get("filesize_approx")
        if not size:
            for f in (d.get("formats") or []):
                if f.get("vcodec") != "none" and f.get("acodec") != "none":
                    size = f.get("filesize") or f.get("filesize_approx")
                    if size: break
        return d.get("title","video"), int(size) if size else None
    except Exception as e:
        print(f"[probe] {e}")
        return None, None

def upload_to_gdrive(filepath, filename):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        info = json.loads(GDRIVE_SA)
        creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
        svc = build("drive","v3",credentials=creds,cache_discovery=False)
        media = MediaFileUpload(filepath, resumable=True, chunksize=8*1024*1024)
        body = {"name": filename}
        if GDRIVE_ID: body["parents"] = [GDRIVE_ID]
        req = svc.files().create(body=body, media_body=media, fields="id")
        resp = None
        while resp is None: _, resp = req.next_chunk()
        svc.permissions().create(fileId=resp["id"], body={"role":"reader","type":"anyone"}).execute()
        return f"https://drive.google.com/file/d/{resp['id']}/view"
    except Exception as e:
        print(f"[GDrive] {e}")
        return None

def process_download(job_id, url, username, tier, ip_hash=None):
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["message"] = "Checking video info..."
        jobs[job_id]["progress"] = 0
    tmpdir = tempfile.mkdtemp(prefix=f"dl_{job_id}_")
    try:
        # Probe video for size (now in background thread, won't block HTTP response)
        title_probe, vsize_probe = probe_video(url)
        if vsize_probe:
            tcfg_check = TIERS.get(tier, TIERS["free"]) if tier != "guest" else GUEST_TIER
            if tcfg_check["max_bytes"] is not None and vsize_probe > tcfg_check["max_bytes"]:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["status"] = "error"
                        jobs[job_id]["message"] = f"File too large ({fmt_size(vsize_probe)}). Your {tcfg_check['label']} tier limit is {fmt_size(tcfg_check['max_bytes'])}."
                return
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["message"] = f"Downloading ({fmt_size(vsize_probe)})..."
        else:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["message"] = "Downloading..."
        outtmpl = os.path.join(tmpdir, "%(title).80s.%(ext)s")
        cmd = ["yt-dlp","-f","best[ext=mp4][height<=1080]/best[ext=webm][height<=1080]/best","--merge-output-format","mp4","--no-playlist","--newline","--no-warnings","-o",outtmpl,url]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.strip()
            if "[download]" in line and "%" in line:
                try:
                    pct = float(line.split("%")[0].split()[-1])
                    with jobs_lock:
                        if job_id in jobs:
                            jobs[job_id]["progress"] = min(pct, 90)
                            jobs[job_id]["message"] = f"Downloading... {pct:.0f}%"
                except: pass
            elif "Merging" in line:
                with jobs_lock:
                    if job_id in jobs: jobs[job_id]["message"] = "Merging audio+video..."
        proc.wait(timeout=600)
        if proc.returncode != 0:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["message"] = "Download failed. Video may be private or unsupported."
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            notify("Download Failed",
                   tg_text=f"❌ Download Failed\nUser: {username}\nURL: {url[:100]}\nTime: {ts} UTC",
                   email_body=f"A download has failed.\n\nUser: {username}\nURL: {url[:100]}\nTime: {ts} UTC")
            return
        files = [f for f in os.listdir(tmpdir) if not f.endswith((".part",".ytdl"))]
        if not files:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["message"] = "No file downloaded."
            return
        filepath = os.path.join(tmpdir, files[0])
        fsize = os.path.getsize(filepath)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "uploading"
                jobs[job_id]["message"] = f"Uploading to Google Drive ({fsize/1048576:.1f} MB)..."
                jobs[job_id]["progress"] = 92
        link = upload_to_gdrive(filepath, files[0]) if GDRIVE_SA and GDRIVE_ID else ""
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["message"] = "Complete!"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["gdrive_link"] = link
                jobs[job_id]["filename"] = files[0]
                jobs[job_id]["filesize"] = fsize
        db = get_db()
        if db:
            dl_record = {
                "username": username, "url": url, "title": files[0],
                "platform": detect_platform(url), "size_bytes": fsize,
                "drive_link": link, "status": "completed",
                "created_at": datetime.now(timezone.utc)
            }
            if ip_hash: dl_record["ip_hash"] = ip_hash
            db.downloads.insert_one(dl_record)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        user_display = f"{username} ({tier})" if username != "guest" else f"Guest (IP: {ip_hash[:8] if ip_hash else 'unknown'}...)"
        notify("Download Complete",
               tg_text=f"✅ Download Complete\nUser: {user_display}\nFile: {files[0]}\nSize: {fsize/1048576:.1f} MB\nURL: {url[:80]}\nGDrive: {link or 'N/A'}\nTime: {ts} UTC",
               email_body=f"A download has completed.\n\nUser: {user_display}\nFile: {files[0]}\nSize: {fsize/1048576:.1f} MB\nURL: {url[:80]}\nGDrive: {link or 'N/A'}\nTime: {ts} UTC")
    except subprocess.TimeoutExpired:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = "Download timed out."
    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = f"Error: {str(e)[:200]}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def fmt_size(b):
    if not b: return "—"
    if b > 1073741824: return f"{b/1073741824:.1f} GB"
    return f"{b/1048576:.1f} MB"

# Cookie strings — SameSite=None for cross-origin (GitHub Pages -> Cloudflare tunnel)
USER_COOKIE = "__Host-user_session={}; Path=/; Secure; HttpOnly; SameSite=None; Max-Age=86400"
ADMIN_COOKIE = "__Host-admin_session={}; Path=/; Secure; HttpOnly; SameSite=None; Max-Age=86400"
CLEAR_COOKIE = "__Host-user_session=; Path=/; Max-Age=0; SameSite=None; Secure|||__Host-admin_session=; Path=/; Max-Age=0; SameSite=None; Secure"

class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        # CRITICAL FIX: Must echo specific Origin, not *, when credentials are included
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cookie")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Max-Age", "86400")
    def _json(self, code, data, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        if cookie:
            # Support multiple Set-Cookie headers separated by "|||"
            for c in cookie.split("|||"):
                c = c.strip()
                if c:
                    self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        db = get_db()
        path = urllib.parse.urlparse(self.path).path
        if path == "/ping":
            self._json(200, {"ok": True, "ts": int(time.time())})
        elif path == "/" or path == "":
            self.send_response(200); self.send_header("Content-Type","text/html"); self._cors(); self.end_headers()
            self.wfile.write(b"<html><body style='background:#0f0f0f;color:#22c55e;font-family:monospace;padding:40px;text-align:center'><h1>YTDownload Server</h1><p>Online</p></body></html>")
        elif path.startswith("/api/status/"):
            jid = path.split("/api/status/")[-1]
            with jobs_lock:
                j = jobs.get(jid)
            if j: self._json(200, {k:v for k,v in j.items()})
            else: self._json(404, {"status":"error","message":"job not found"})
        elif path == "/api/auth/me":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            u, _ = get_session_user(db, self.headers)
            if not u: self._json(401, {"error":"not authenticated"}); return
            tier = u.get("tier", "free")
            today_dl = count_downloads_24h(db, u["username"])
            self._json(200, {"username": u["username"], "tier": tier, "isAdmin": u.get("is_admin", False), "downloadsToday": today_dl, "maxSize": TIERS[tier]["max_bytes"], "dailyLimit": TIERS[tier]["daily_limit"], "botAccess": tier == "supreme", "banned": u.get("banned", False)})
        elif path == "/api/guest-quota":
            if not db: self._json(200, {"remaining": GUEST_DAILY_LIMIT, "limit": GUEST_DAILY_LIMIT, "tier": "guest"}); return
            ip = self.client_address[0] if self.client_address else "unknown"
            iph = mask_ip(ip)
            used = count_guest_downloads_24h(db, iph)
            remaining = max(0, GUEST_DAILY_LIMIT - used)
            self._json(200, {"remaining": remaining, "limit": GUEST_DAILY_LIMIT, "used": used, "tier": "guest"})
        elif path == "/api/history":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            u, _ = get_session_user(db, self.headers)
            if not u: self._json(401, {"error":"not authenticated"}); return
            items = list(db.downloads.find({"username": u["username"]}).sort("created_at", -1).limit(50))
            self._json(200, [{"title": d.get("title",""), "size": d.get("size_bytes",0), "link": d.get("drive_link",""), "date": d.get("created_at","").isoformat() if isinstance(d.get("created_at"), datetime) else str(d.get("created_at","")), "platform": d.get("platform","")} for d in items])
        elif path == "/api/admin/users":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            users = list(db.users.find({}, {"_id":0,"password":0}).limit(500))
            self._json(200, users)
        elif path == "/api/admin/stats":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            cutoff24 = datetime.now(timezone.utc) - timedelta(hours=24)
            self._json(200, {"totalUsers": db.users.count_documents({}), "totalDownloads": db.downloads.count_documents({}), "todayDownloads": db.downloads.count_documents({"created_at": {"$gte": cutoff24}}), "activeSessions": db.sessions.count_documents({})})
        elif path == "/api/admin/logs":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            logs = list(db.logs.find({}, {"_id":0}).sort("created_at", -1).limit(100))
            self._json(200, logs)
        else:
            self._json(404, {"error":"not found"})
    def do_POST(self):
        db = get_db()
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode()) if length else {}
        except Exception as e:
            print(f"[POST] body parse error: {e}")
            body = {}
        if path == "/api/auth":
            action = body.get("action", "")
            if action == "register":
                uname = body.get("username","").strip().lower()
                pw = body.get("password","")
                if not uname or len(uname) < 3 or len(uname) > 32: self._json(400, {"error":"Username must be 3-32 chars"}); return
                if len(pw) < 6: self._json(400, {"error":"Password must be 6+ chars"}); return
                if not db: self._json(500, {"error":"DB unavailable"}); return
                if db.users.find_one({"username": uname}): self._json(409, {"error":"Username taken"}); return
                db.users.insert_one({"username": uname, "password": hash_pw(pw), "tier": "free", "banned": False, "is_admin": False, "created_at": datetime.now(timezone.utc)})
                tok, _ = create_session(db, uname)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                notify("New User Registration",
                       tg_text=f"🆕 New Registration\nUser: {uname}\nTier: Free\nTime: {ts} UTC",
                       email_body=f"A new user has registered.\n\nUsername: {uname}\nTier: Free\nTime: {ts} UTC")
                self._json(200, {"success": True, "username": uname, "tier": "free"}, cookie=USER_COOKIE.format(tok))
            elif action == "signin":
                uname = body.get("username","").strip().lower()
                pw = body.get("password","")
                if not db: self._json(500, {"error":"DB unavailable"}); return
                u = db.users.find_one({"username": uname})
                if not u or not verify_pw(pw, u.get("password","")): self._json(401, {"error":"Invalid credentials"}); return
                if u.get("banned"): self._json(403, {"error":"Account banned"}); return
                tok, _ = create_session(db, uname)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                notify("User Login",
                       tg_text=f"🔑 Login\nUser: {uname}\nTier: {u.get('tier','free')}\nTime: {ts} UTC",
                       email_body=f"User logged in.\n\nUsername: {uname}\nTier: {u.get('tier','free')}\nTime: {ts} UTC")
                self._json(200, {"success": True, "username": uname, "tier": u.get("tier","free"), "isAdmin": u.get("is_admin", False)}, cookie=USER_COOKIE.format(tok))
            elif action == "admin_login":
                au = body.get("username","").strip().lower()
                pw = body.get("password","")
                if au != ADMIN_USERNAME.lower() or pw != ADMIN_PASSWORD:
                    self._json(401, {"error":"Wrong admin credentials"}); return
                tok, _ = create_session(db, "admin", is_admin=True)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                notify("Admin Login Alert",
                       tg_text=f"🛡️ Admin Login\nTime: {ts} UTC",
                       email_body=f"Admin login detected.\n\nTime: {ts} UTC\nUsername: {au}")
                self._json(200, {"success": True}, cookie=ADMIN_COOKIE.format(tok))
            elif action == "logout":
                cookies = parse_cookies(self.headers.get("Cookie",""))
                for ck in ["__Host-user_session","__Host-admin_session","user_session","admin_session"]:
                    if cookies.get(ck) and db: db.sessions.delete_one({"token": cookies[ck]})
                self._json(200, {"success": True}, cookie=CLEAR_COOKIE)
            else:
                self._json(400, {"error":"invalid action"})
        elif path == "/api/download":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            url = body.get("url","").strip()
            if not url or len(url) < 10: self._json(400, {"error":"valid URL required"}); return

            u, _ = get_session_user(db, self.headers)
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if not ip: ip = self.client_address[0] if self.client_address else "unknown"
            iph = mask_ip(ip)

            if u:
                # Authenticated user
                if u.get("banned"): self._json(403, {"error":"Account banned"}); return
                tier = u.get("tier", "free")
                tcfg = TIERS.get(tier, TIERS["free"])
                dl_count = count_downloads_24h(db, u["username"])
                if tcfg["daily_limit"] is not None and dl_count >= tcfg["daily_limit"]:
                    self._json(429, {"error":f"Daily limit reached ({tcfg['daily_limit']} per 24h). Your oldest download will free up a slot after 24 hours. Upgrade at t.me/DJ_Hackrr for more."}); return
                username = u["username"]
                ip_hash_for_record = None  # Don't track IP for logged-in users
            else:
                # Guest user — IP-based rate limiting
                tier = "guest"
                tcfg = GUEST_TIER
                guest_count = count_guest_downloads_24h(db, iph)
                if guest_count >= GUEST_DAILY_LIMIT:
                    self._json(429, {"error":f"Guest limit reached ({GUEST_DAILY_LIMIT} per 24h). Sign up for free to get 5 downloads/day, or upgrade at t.me/DJ_Hackrr on Telegram!"}); return
                username = "guest"
                ip_hash_for_record = iph

            # Platform enforcement — check tier allows this platform
            platform = detect_platform(url)
            allowed_platforms = TIER_PLATFORMS.get(tier)
            if allowed_platforms is not None and platform not in allowed_platforms:
                self._json(403, {"error":f"Your {tcfg['label']} tier does not support {platform}. Allowed platforms: {', '.join(allowed_platforms)}. Upgrade at t.me/DJ_Hackrr for more platforms."}); return

            # NOTE: probe_video moved to background thread (was root cause of "Network error" timeout)
            jid = str(uuid.uuid4())[:8]
            with jobs_lock:
                jobs[jid] = {"status":"queued","message":"Queued...","progress":0,"platform":detect_platform(url),"gdrive_link":"","filename":"","filesize":0,"created_at":time.time()}
            t = threading.Thread(target=process_download, args=(jid, url, username, tier, ip_hash_for_record), daemon=True)
            t.start()

            user_label = f"{username} ({tier})" if username != "guest" else f"Guest (IP: {iph[:8]}...)"
            send_tg(f"⬇️ Download Request\nUser: {user_label}\nPlatform: {detect_platform(url)}\nURL: {url[:100]}")
            self._json(200, {"job_id": jid, "status": "queued", "tier": tier})
        elif path == "/api/admin/set-tier":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            uname = body.get("username","")
            new_tier = body.get("tier","")
            if new_tier not in TIERS: self._json(400, {"error":"invalid tier"}); return
            db.users.update_one({"username": uname}, {"$set": {"tier": new_tier}})
            notify("Tier Changed",
                   tg_text=f"⬆️ Tier Changed\nUser: {uname}\nNew Tier: {TIERS[new_tier]['label']}\nBy: admin",
                   email_body=f"User tier has been changed.\n\nUser: {uname}\nNew Tier: {TIERS[new_tier]['label']}\nChanged by: admin")
            self._json(200, {"success": True})
        elif path == "/api/admin/ban-user":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            uname = body.get("username","")
            db.users.update_one({"username": uname}, {"$set": {"banned": True}})
            notify("User Banned",
                   tg_text=f"🚫 User Banned\nUser: {uname}",
                   email_body=f"A user has been banned.\n\nUsername: {uname}")
            self._json(200, {"success": True})
        elif path == "/api/admin/unban-user":
            if not db: self._json(500, {"error":"DB unavailable"}); return
            admin = get_session_admin(db, self.headers)
            if not admin: self._json(403, {"error":"admin only"}); return
            uname = body.get("username","")
            db.users.update_one({"username": uname}, {"$set": {"banned": False}})
            notify("User Unbanned",
                   tg_text=f"✅ User Unbanned\nUser: {uname}",
                   email_body=f"A user has been unbanned.\n\nUsername: {uname}")
            self._json(200, {"success": True})
        elif path == "/log":
            event = body.get("event","unknown")
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if not ip: ip = self.client_address[0] if self.client_address else "unknown"
            iph = mask_ip(ip)
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            if event == "visit":
                notify("New Website Visit",
                       tg_text=f"👁️ New Visit\nTime: {ts} UTC\nIP: {iph[:12]}...(masked)",
                       email_body=f"New website visit detected.\n\nTime: {ts} UTC\nIP (masked): {iph[:12]}...")
            if db:
                db.logs.insert_one({"event": event, "ip": iph[:16], "created_at": datetime.now(timezone.utc)})
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error":"not found"})
    def log_message(self, *a): pass

if __name__ == "__main__":
    print(f"Download server v4 on :{PORT}")
    print(f"  BOT_TOKEN: {'set' if BOT_TOKEN else 'MISSING'}")
    print(f"  GDRIVE: {'set' if GDRIVE_SA else 'MISSING'}")
    print(f"  DATABASE: {'set' if DATABASE_URL else 'MISSING'}")
    print(f"  BREVO: {'set' if BREVO_API_KEY else 'MISSING'}")
    print(f"  ADMIN_USERNAME: {ADMIN_USERNAME}")
    print(f"  Guest limit: {GUEST_DAILY_LIMIT}/24h")
    print(f"  Free limit: {TIERS['free']['daily_limit']}/24h")
    notify("Download Server Started",
           tg_text="🟢 Download Server v4 Started\nGuest downloads enabled (3/24h)\nRolling 24h rate limiting\nMasked IPs\nReady for downloads.",
           email_body="The download server v4 has started.\n\nFeatures:\n- Guest downloads enabled (3 per 24h)\n- Rolling 24h rate limiting\n- Masked IPs (SHA-256)\n- Fixed CORS for cross-origin auth\n\nReady for downloads.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
