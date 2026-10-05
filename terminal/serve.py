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
import os
import re
import socketserver
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Generated artifacts live in STATE_DIR (a persistent volume when containerized)
# so LLM-built dossiers survive restarts. Static assets stay next to the code.
STATE_DIR = os.path.expanduser(os.environ.get("TERMINAL_STATE_DIR", HERE))
REFRESH_S = 45  # data rebuild cadence (engine cycle is 5m; near-real-time feel)
PORT = 8770
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
# paths served from STATE_DIR rather than HERE
GENERATED_PREFIXES = ("/dossiers/",)
GENERATED_FILES = ("data.js", "data.json")


def absolutize_og(html):
    """Rewrite relative og:image/twitter:image to absolute under PUBLIC_BASE."""
    if not PUBLIC_BASE:
        return html
    def fix(m):
        return m.group(1) + PUBLIC_BASE + "/" + m.group(2)
    html = re.sub(r'((?:og:image|twitter:image)" content=")(og\.png)', fix, html)
    if "<!--OGABS-->" in html:
        extra = ('<meta property="og:url" content="%s/">\n'
                 '<meta name="twitter:title" content="OBEKT TERMINAL — autonomous crypto scalping, live">\n'
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
        # data.js/dossiers must never be cached (live); static assets can be
        p = self.path.split("?")[0]
        if p.endswith(("data.js", "data.json")) or p.startswith("/dossiers/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        elif p == "/og.png":
            self.send_header("Cache-Control", "public, max-age=300")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self):
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
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("OBEKT TERMINAL on http://localhost:%d  (rebuild every %ds, PUBLIC_BASE=%s)"
              % (PORT, REFRESH_S, PUBLIC_BASE or "(none)"))
        httpd.serve_forever()
