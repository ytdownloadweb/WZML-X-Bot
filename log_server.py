#!/usr/bin/env python3
"""Lightweight log server for the downloader website.

Receives HTTP POST requests from the website frontend and forwards
them as messages to a Telegram group using the Bot API.

Listens on port 8081.

Endpoints:
  GET  /           - simple HTML status page (so root URL doesn't show error)
  GET  /ping       - health check (returns {"ok": true})
  POST /log        - forward a log message to TG group
"""
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOG_CHAT_ID = os.environ.get("LOG_CHAT_ID", "")
PORT = int(os.environ.get("LOG_PORT", "8081"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

LANDING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Log Server</title></head>
<body style="font-family:monospace;background:#0f0f0f;color:#22c55e;padding:40px;text-align:center;">
<h1>Log Server Running</h1>
<p>Endpoints:</p>
<p>POST /log - send a log event</p>
<p>GET /ping - health check</p>
</body></html>"""


def send_tg(text, parse_mode="HTML"):
    if not BOT_TOKEN or not LOG_CHAT_ID:
        print(f"[LOG] (no TG configured) {text}")
        return False
    try:
        data = json.dumps({
            "chat_id": LOG_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(TG_API, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 200
    except Exception as e:
        print(f"[ERROR] TG send failed: {e}")
        return False


class LogHandler(BaseHTTPRequestHandler):
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
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/log":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return

        event = data.get("event", "unknown")
        url = data.get("url", "")
        extra = data.get("extra", "")
        platform = data.get("platform", "")
        ip = self.client_address[0] if self.client_address else "unknown"
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

        if event == "visit":
            msg = f"\U0001F441\ufe0f <b>New Visit</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"
            if extra:
                msg += f"\n{extra}"
        elif event == "download":
            msg = f"\u2b07\ufe0f <b>Download Request</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"
            if platform:
                msg += f"\nPlatform: {platform}"
            msg += f"\nURL: <code>{url}</code>"
        elif event == "open_manager":
            msg = f"\U0001F517 <b>Opened Download Manager</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"
            if url:
                msg += f"\nURL: <code>{url}</code>"
        else:
            msg = f"\U0001F4CB <b>{event}</b>\n\nTime: {ts} UTC\nIP: <code>{ip}</code>"
            if url:
                msg += f"\nURL: <code>{url}</code>"
            if extra:
                msg += f"\n{extra}"

        ok = send_tg(msg)
        print(f"[{ts}] event={event} ip={ip} platform={platform} url={url[:60]} tg={'ok' if ok else 'fail'}")
        self._send_json(200, {"ok": True, "sent": ok})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), LogHandler)
    print(f"Log server listening on :{PORT}")
    print(f"  BOT_TOKEN: {'set' if BOT_TOKEN else 'MISSING'}")
    print(f"  LOG_CHAT_ID: {'set' if LOG_CHAT_ID else 'MISSING'}")
    send_tg(f"\U0001F7E2 <b>Log Server Started</b>\n\nListening on port {PORT}\nReady to receive website logs.")
    server.serve_forever()
