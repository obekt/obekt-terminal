#!/usr/bin/env python3
"""
OBEKT TERMINAL — local dev server + static-file builder for public hosting.

Serves ./ (static) and regenerates data.js in the background every REFRESH_S
seconds from the live trading system (read-only).

    python3 serve.py [--port 8770]

PUBLIC HOSTING (recommended): the page is 100% static. Run `generate.py`
from cron on the machine that has the trading log, then serve this directory
with nginx/Caddy or rsync it to any static host. Set PUBLIC_BASE so the
Open Graph share image resolves to an absolute URL for social crawlers:

    PUBLIC_BASE=https://terminal.yourdomain.com python3 serve.py

The same var, exported before `python3 generate.py --og`, bakes absolute
og:image URLs into a `index.public.html` you can deploy as `index.html`.
"""
import http.server
import json
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Generated artifacts live in STATE_DIR (a persistent volume when containerized)
# so LLM-built dossiers survive restarts. Static assets stay next to the code.
STATE_DIR = os.path.expanduser(os.environ.get("TERMINAL_STATE_DIR", HERE))
REFRESH_S = 45  # data rebuild cadence (engine cycle is 5m; near-real-time feel)
PORT = 8770
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
# paths served from STATE_DIR rather than HERE
GENERATED_PREFIXES = ("/dossiers/",)
GENERATED_FILES = ("data.js", "data.json", "chain.jsonl", "head.json")

# Themed 404 — keep the terminal aesthetic instead of the stock Python error page.
NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>404 — SIGNAL LOST · Obekt Terminal</title></head>
<body style="margin:0;background:#050505;color:#e8e6df;font-family:'IBM Plex Mono','JetBrains Mono','SF Mono',Menlo,Consolas,'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh">
<div style="text-align:center;padding:24px;border:1px solid #3a2f14;background:#0a0a0a;max-width:560px">
<div style="color:#ffb000;font-size:28px;font-weight:700;letter-spacing:3px">▚ 404 — SIGNAL LOST</div>
<div style="color:#8a867a;font-size:13px;margin-top:10px;letter-spacing:1px">this frequency is dead air</div>
<div style="margin-top:22px"><a href="/" style="color:#29c9eb;text-decoration:none;border:1px solid #3a2f14;padding:6px 14px;font-size:13px;letter-spacing:1px">← back to the terminal</a></div>
</div></body></html>
"""

# Inline SVG favicon (▚ glyph, amber on black) so /favicon.ico stops 404ing.
FAVICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
               "<rect width='16' height='16' fill='#050505'/>"
               "<text x='1' y='13' font-size='13' font-family='monospace' fill='#ffb000'>▚</text>"
               "</svg>")


def absolutize_og(html):
    """Rewrite relative og:image/twitter:image to absolute under PUBLIC_BASE."""
    if not PUBLIC_BASE:
        return html
    def fix(m):
        return m.group(1) + PUBLIC_BASE + "/" + m.group(2)
    html = re.sub(r'((?:og:image|twitter:image)" content=")(og\.png)', fix, html)
    if "<!--OGABS-->" in html:
        extra = ('<meta property="og:url" content="%s/">\n'
                 '<meta name="twitter:title" content="OBEKT TERMINAL — autonomous US-stock news trading, live">\n'
                 % PUBLIC_BASE)
        html = html.replace("<!--OGABS-->", extra)
    return html


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def translate_path(self, path):
        """Serve generated artifacts (data.js/json, dossiers/, og.png) from
        STATE_DIR — the persistent volume — and everything else from HERE."""
        clean = path.split("?")[0].split("#")[0]
        name = clean.lstrip("/")
        if (clean.startswith(GENERATED_PREFIXES)
                or name in GENERATED_FILES or name == "og.png"):
            # strip leading slash and resolve against STATE_DIR, safely
            rel = name
            target = os.path.normpath(os.path.join(STATE_DIR, rel))
            if target.startswith(os.path.abspath(STATE_DIR)):
                return target
        return super().translate_path(path)

    def end_headers(self):
        # data.js/dossiers must never be cached (live); data.json is revalidated
        # every poll (no-cache + Last-Modified → 304 when unchanged, so the
        # browser fetch loop skips re-downloading ~71KB each time).
        p = self.path.split("?")[0]
        if p.endswith("data.json"):
            self.send_header("Cache-Control", "no-cache")
        elif (p.endswith(("data.js", "chain.jsonl", "head.json"))
                or p.startswith("/dossiers/")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        elif p == "/og.png":
            self.send_header("Cache-Control", "public, max-age=300")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # inline waitlist script + inline styles exist → 'unsafe-inline' required
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            body = NOT_FOUND_HTML.encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        return super().send_error(code, message, explain)

    def do_GET(self):
        # /verify is the canonical proof page URL (verify.html on disk)
        if self.path.split("?")[0] in ("/verify", "/verify/"):
            self.path = "/verify.html"
        # inline SVG favicon — no binary asset to ship, no 404
        if self.path.split("?")[0] == "/favicon.ico":
            data = FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # inject absolute OG URLs for index when PUBLIC_BASE is set
        if PUBLIC_BASE and self.path.split("?")[0] in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read().decode("utf-8")
                body = absolutize_og(body)
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception:
                pass  # fall through to normal static handling
        return super().do_GET()

    _wl_last = {}

    def do_POST(self):
        # Waitlist only: emails for the weekly track-record report. Stored in
        # STATE_DIR/waitlist.jsonl. No third-party services, no tokens, nothing
        # else on this box accepts POST.
        if self.path.split("?")[0] != "/api/waitlist":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1024:
                return self.send_error(400)
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            email = str(body.get("email", "")).strip().lower()[:254]
            if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
                return self.send_error(400)
            wl = os.path.join(STATE_DIR, "waitlist.jsonl")
            try:
                count = sum(1 for _ in open(wl))
            except FileNotFoundError:
                count = 0
            if count > 50000:
                return self.send_error(429)
            ip = self.client_address[0]
            nowt = time.time()
            if nowt - self._wl_last.get(ip, 0) < 60:
                return self.send_error(429)
            self._wl_last[ip] = nowt
            with open(wl, "a") as f:
                f.write(json.dumps({"email": email, "ts": int(nowt), "ip": ip}) + "\n")
            data = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(400)

    def log_message(self, *a):
        pass


def rebuild_loop():
    sys.path.insert(0, HERE)
    import generate
    while True:
        try:
            generate.build()
        except Exception as ex:
            print("rebuild failed:", ex, file=sys.stderr)
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    if "--build-public" in sys.argv:
        # one-shot: bake index.public.html with absolute OG urls, then exit
        with open(os.path.join(HERE, "index.html")) as f:
            html = f.read()
        out = absolutize_og(html)
        with open(os.path.join(HERE, "index.public.html"), "w") as f:
            f.write(out)
        print("wrote index.public.html with PUBLIC_BASE=%s" % (PUBLIC_BASE or "(none set)"))
        sys.exit(0)
    t = threading.Thread(target=rebuild_loop, daemon=True)
    t.start()
    # ThreadingHTTPServer: concurrent polls (fetch revalidation, dossiers, OG
    # crawlers) no longer queue behind each other on a single-threaded server.
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("OBEKT TERMINAL on http://localhost:%d  (rebuild every %ds, PUBLIC_BASE=%s)"
              % (PORT, REFRESH_S, PUBLIC_BASE or "(none)"))
        httpd.serve_forever()
