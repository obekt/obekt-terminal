#!/usr/bin/env python3
"""
OBEKT TERMINAL — dynamic Open Graph share card.

Renders a terminal-style PNG (1200x630, the OG standard) from the CURRENT
data.json so every shared link previews real live numbers — equity, day P&L,
the latest Jev vote, the selectivity funnel and the equity sparkline.

Called by generate.py after each build. Pure stdlib + Pillow; no network.
Writes og.png atomically. Skips silently if Pillow is missing (page still works).
"""
import json
import os
import sys

SITE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser(os.environ.get("TERMINAL_STATE_DIR", SITE))
os.makedirs(STATE_DIR, exist_ok=True)
W, H = 1200, 630
FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]

# palette (matches style.css)
BG = (5, 5, 5)
PANEL = (10, 10, 10)
AMBER = (255, 176, 0)
AMBER_DIM = (168, 118, 10)
GREEN = (0, 224, 75)
GREEN_DIM = (10, 122, 48)
RED = (255, 59, 48)
RED_DIM = (122, 35, 28)
CYAN = (41, 201, 235)
WHITE = (232, 230, 223)
GRAY = (138, 134, 122)
BORDER = (58, 47, 20)


def find_font():
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def render(data):
    from PIL import Image, ImageDraw, ImageFont
    fp = find_font()
    if not fp:
        print("og: no mono font found, skipping", file=sys.stderr)
        return False

    def F(sz):
        try:
            return ImageFont.truetype(fp, sz)
        except Exception:
            return ImageFont.load_default()

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    eq = data.get("equity") or {}
    st = data.get("stats") or {}
    fn = data.get("funnel") or {}
    j = data.get("jev_latest") or {}
    eng = data.get("engine") or {}
    dn = data.get("desk_note") or {}

    def txt(x, y, s, col, font, anchor=None):
        d.text((x, y), s, fill=col, font=font, anchor=anchor)

    # scanline texture
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill=(0, 0, 0), width=1)

    # ---- top bar ----
    f_logo = F(30)
    f_meta = F(15)
    d.rectangle([0, 0, W, 56], fill=(12, 10, 4))
    d.line([(0, 56), (W, 56)], fill=AMBER_DIM, width=1)
    txt(24, 12, "▚ JEV", AMBER, f_logo)
    txt(110, 16, "TERMINAL", WHITE, f_logo)
    mode = (eng.get("mode") or "LIVE").split("—")[0].strip()
    txt(W - 24, 20, "● LIVE · " + mode, GREEN, f_meta, anchor="ra")

    # ---- headline ----
    f_h1 = F(34)
    f_h2 = F(17)
    txt(24, 76, "AN AUTONOMOUS SYSTEM TRADING REAL CAPITAL.", AMBER, f_h1)
    txt(24, 120, "LLM desk · Jev System-One typed decisions · news/sentiment/macro injection · server-side risk exits.", GRAY, f_h2)

    # ---- KPI boxes ----
    f_kn = F(13)
    f_kv = F(38)
    f_ks = F(13)
    day_pnl = eq.get("day_pnl_eur")
    pnl_col = GREEN if (day_pnl or 0) >= 0 else RED
    boxes = [
        ("EQUITY", "€%.2f" % (eq.get("equity_eur") or 0), WHITE, "live capital"),
        ("TODAY", ("+" if (day_pnl or 0) >= 0 else "") + "%.2f€" % (day_pnl or 0), pnl_col,
         ("+" if (eq.get("day_pnl_pct") or 0) >= 0 else "") + "%.2f%%" % (eq.get("day_pnl_pct") or 0)),
        ("TRADES", str(st.get("trades_all") or 0), WHITE, "%.1f%% win" % (st.get("win_rate_pct") or 0)),
        ("JEV CALLS", str(fn.get("jev_calls") or 0), AMBER, "%d buy votes" % (fn.get("buy_picks") or 0)),
        ("UPTIME", ("%dD" % (fn.get("uptime_h", 0) / 24)) if fn.get("uptime_h") else "—", WHITE, "unattended"),
    ]
    bx, by, bw, bh, gap = 24, 160, 224, 108, 12
    for i, (k, v, col, sub) in enumerate(boxes):
        x = bx + i * (bw + gap)
        d.rectangle([x, by, x + bw, by + bh], fill=PANEL, outline=BORDER, width=1)
        txt(x + 14, by + 12, k, GRAY, f_kn)
        txt(x + 14, by + 30, v, col, f_kv)
        txt(x + 14, by + 80, sub, GRAY, f_ks)

    # ---- latest Jev vote bar ----
    f_sec = F(18)
    f_row = F(15)
    vy = 292
    d.rectangle([24, vy, W - 24, vy + 96], fill=(8, 8, 8), outline=BORDER, width=1)
    txt(40, vy + 12, "LATEST JEV SYSTEM-ONE CALL", CYAN, f_sec)
    choice = (j.get("choice") or "no_trade")
    probs = j.get("probabilities") or {}
    if choice != "no_trade":
        head = "▲ VOTE: BUY " + choice.replace("buy_", "").upper()
        hcol = GREEN
    else:
        head = "▼ VOTE: NO-TRADE — STANDING ASIDE"
        hcol = GRAY
    txt(40, vy + 40, head, hcol, f_row)
    conf = j.get("confidence")
    txt(40, vy + 62, "confidence %s · margin %s · noul %s" % (
        ("%.2f" % conf) if conf is not None else "—",
        ("%.2f" % j.get("margin")) if j.get("margin") is not None else "—",
        ("%.2f" % j.get("noul")) if j.get("noul") is not None else "—"), GRAY, f_row)
    # probability bars on the right
    barx = 560
    top = sorted(probs.items(), key=lambda kv: -kv[1])[:4]
    for i, (kk, vv) in enumerate(top):
        yy = vy + 20 + i * 18
        lbl = "NO_TRADE" if kk == "no_trade" else "BUY " + kk.replace("buy_", "").upper()
        txt(barx, yy, lbl, GRAY, f_row)
        full = 360
        d.rectangle([barx + 130, yy + 2, barx + 130 + full, yy + 13], fill=(20, 16, 5))
        hot = (kk == choice and choice != "no_trade")
        d.rectangle([barx + 130, yy + 2, barx + 130 + int(full * vv), yy + 13],
                    fill=(AMBER if hot else GREEN_DIM))
        txt(barx + 138 + full, yy, "%d%%" % round(vv * 100), AMBER if hot else WHITE, f_row)

    # ---- equity sparkline ----
    cum = st.get("cum_pnl") or []
    sy = 408
    d.rectangle([24, sy, 600, sy + 120], fill=(8, 8, 8), outline=BORDER, width=1)
    txt(40, sy + 10, "CUMULATIVE REALIZED P&L — EVERY TRADE", CYAN, f_sec)
    if len(cum) >= 2:
        px0, px1, py0, py1 = 44, 584, sy + 40, sy + 108
        mn = min(0, min(cum)); mx = max(0, max(cum)); rng = (mx - mn) or 1
        pts = []
        for i, v in enumerate(cum):
            x = px0 + i * (px1 - px0) / (len(cum) - 1)
            y = py1 - (v - mn) * (py1 - py0) / rng
            pts.append((x, y))
        zero_y = py1 - (0 - mn) * (py1 - py0) / rng
        d.line([(px0, zero_y), (px1, zero_y)], fill=BORDER, width=1)
        col = GREEN if cum[-1] >= 0 else RED
        for a, b in zip(pts, pts[1:]):
            d.line([a, b], fill=col, width=2)
        txt(40, sy + 96, ("+" if cum[-1] >= 0 else "") + "%.2f€ net · %d trades · ledger-verified" % (cum[-1], len(cum)), col, f_ks)

    # ---- funnel ----
    fx = 636
    d.rectangle([fx, sy, W - 24, sy + 120], fill=(8, 8, 8), outline=BORDER, width=1)
    txt(fx + 14, sy + 10, "SELECTIVITY FUNNEL", CYAN, f_sec)
    stages = [
        (fn.get("pair_scans_all"), "PAIR SCANS"),
        (fn.get("candidates_all"), "PASSED FILTERS"),
        (fn.get("jev_pair_evals_all"), "JEV EVALS"),
        (fn.get("opens"), "OPENED"),
    ]
    smax = max(1, *[s[0] or 0 for s in stages])
    for i, (num, lbl) in enumerate(stages):
        yy = sy + 38 + i * 20
        bw2 = int(300 * ((num or 0) / smax) ** 0.5)
        d.rectangle([fx + 14, yy, fx + 14 + max(2, bw2), yy + 12], fill=AMBER_DIM)
        txt(fx + 24 + max(2, bw2), yy - 1, "%s  %s" % ("{:,}".format(num or 0), lbl), WHITE, f_ks)

    # ---- desk note + footer ----
    fy = 544
    note = (dn.get("desk_note") or "")
    if note:
        note = note[:150] + ("…" if len(note) > 150 else "")
        txt(24, fy, "▸ " + note, AMBER_DIM, F(13))
    d.line([(0, H - 40), (W, H - 40)], fill=BORDER, width=1)
    txt(24, H - 30, "READ-ONLY SHOWCASE · NOT A BACKTEST · NOT FINANCIAL ADVICE · PRICES: OKX EUROPE PUBLIC FEED", GRAY, F(13))
    contact = data.get("contact") or ""
    txt(W - 24, H - 30, contact, GREEN, F(13), anchor="ra")

    tmp = os.path.join(STATE_DIR, "og.png.tmp")
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, os.path.join(STATE_DIR, "og.png"))
    return True


if __name__ == "__main__":
    with open(os.path.join(STATE_DIR, "data.json")) as f:
        data = json.load(f)
    ok = render(data)
    print("og.png written" if ok else "og render skipped")
