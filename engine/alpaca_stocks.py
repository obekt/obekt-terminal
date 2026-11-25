#!/usr/bin/env python3
"""alpaca_stocks.py — v13.0-volbrackets: INTRADAY NEWS-MOMENTUM US EQUITY engine on Alpaca.

v13.0 (2026-11-25, KILL-GATE REPLACEMENT of v12 — pre-registered gate fired:
34 closes >= 30 target, live realized net -$0.09 <= 0). Evidence from the v12
book (11-20..11-25, 39 entries / 34 closes):
  - LLM bracket templating: 32/39 entries carried EXACTLY tp 1.2 / sl 0.8 and
    30/39 confidence 0.62 — the model anchored, it did not differentiate;
  - fixed 0.8% stops sat inside the noise band: 18/34 closes were stops (53%),
    5 of them within 10 minutes of entry; win rate 40%, avg win ~1.5% vs avg
    loss ~0.95% -> a breakeven machine;
  - same-symbol re-entry while still held (cooldown measured from last EXIT)
    overwrote symbol-keyed meta and orphaned earlier lots (UBER x4 on 11-24).
v13 changes (code owns risk, harder):
  - BRACKETS ARE CODE-COMPUTED from measured per-symbol volatility (14-session
    daily ATR, IEX): SL = clamp(0.35 x ATR%, 0.6, 2.0), TP = clamp(1.5 x SL,
    1.0, 3.0). The LLM no longer outputs target/stop at all — it classifies the
    catalyst (action/confidence/impact), which is its actual job;
  - prompt asks for CALIBRATED confidence across the full range (anti-anchor);
  - deep-dip rule: day_chg <= -4% requires impact=high AND conf >= 0.70;
  - no pyramiding: a symbol already held is never re-entered (meta overwrite
    bug fixed);
  - cash-clamped leg budget + fail-soft entries: v12 sized off equity alone and
    an unhandled Alpaca 40310000 (cash spent) killed the cycle before status();
    v13 clamps the budget to actual cash and logs entry errors instead of
    crashing the loop;
  - everything else kept: intraday clock (no entries >15:30 ET, FLAT 15:50 ET),
    MAX_OPEN 3, 45m post-exit cooldown, -8% daily breaker, fail-closed LLM,
    same telemetry filenames/events (deck + signed chain unchanged).
HONEST EV: volatility-scaled geometry removes the measured noise-stop failure
mode; the catalyst edge itself is still unproven. KILL-GATE v13 (pre-registered
11-25): 30 closed trades OR 7 days (~12-02), whichever FIRST, live realized
net <= 0 -> replace (operator standing rule).

v12.0 (2026-11-20, OPERATOR DIRECTIVE: "we want more trades. multiple trades a
day. we are not aiming long term stocks with that pocket money"): recalibrated
from the v11 catalyst-only book for intraday turnover —
  - prompt admits substantive MEDIUM-quality catalysts (analyst actions with a
    thesis, product news, stakeholder moves, sector read-throughs), still
    rejects listicles/rehash/misattribution/negative catalysts;
  - CONF_FLOOR 0.60->0.55, MAX_LLM_CALLS 4->8, MAX_OPEN 2->3;
  - intraday geometry: TP 0.8-2.5%, SL 0.5-1.5%, hold <=6h (was 48h);
  - re-entry COOLDOWN 45m per symbol replaces the one-entry-per-day lockout;
  - deterministic clock: no entries after 15:30 ET, FLAT EVERYTHING 15:50 ET —
    overnight gap risk is designed out, the book is flat every night;
  - costs: commission-free, round trip ~spread+0.003% regulatory — the fee wall
    that killed the OKX scalp does not exist here; the risk is adverse selection
    on noise entries, which the prompt+gates still filter.
HONEST EV: intraday news momentum is a known pattern but unproven HERE; higher
turnover = faster learning + higher variance. KILL-GATE v12 (pre-registered
11-20): 30 closed trades OR 7 days (~11-27), whichever FIRST, live realized
net <= 0 -> replace (operator standing rule). Mechanics review day 2.

v11.0-news (2026-11-19): NEWS-DRIVEN US EQUITY engine on Alpaca.

VENUE CHANGE (2026-11-19, operator directive): the whole platform moved from
OKX EEA crypto X-Perps to Alpaca US STOCKS. "we will trade real stocks. and
will make decisions with stock news. just moving the crypto value of money
to alpaca". The OKX era is archived in *_okx_retired files; this engine
reuses the SAME telemetry filenames + event vocabulary (perp_log.jsonl,
perp_positions.json, perp_status.json, perp_* events) so the public deck,
its normalize_perp_events adapter and the transparency chain keep working.
Key names are internal identifiers; all money is USD.

Architecture (manage/decide split, proven on OKX v7-v10):
  - NEWS is the signal: Alpaca News API (Benzinga-derived, free) polled each
    cycle; fresh headlines on universe names become candidates.
  - HALOGEN decides: self-hosted halogen-qwen3.8-flash-next via
    chat.obekt.com/v1 ($0 marginal) reads headline+summary+price context and
    returns TYPED JSON {action, confidence, impact, target_pct, stop_pct,
    horizon_hours, rationale}. ANY transport/parse/schema failure FAILS
    CLOSED to no_trade (news_llm_err event). The LLM never sizes or overrides
    a gate.
  - CODE owns risk: market-hours clock, spread/staleness gates, chase guard,
    one-entry-per-symbol-per-day, MAX_OPEN slots, all-in fractional sizing,
    TP/SL brackets (engine-managed: fractional orders cannot carry server
    brackets), 48h time exit, -8% daily equity breaker, STOP/STOP_PERP kill
    files, orphan reconciliation.

Venue facts (verified live 11-19): stocks commission-free (sell-side SEC/TAF
~0.003% estimated on close), fractional trading enabled, T+1 settlement but
margin account trades unsettled funds, PDT rule retired by FINRA 06-2026 (no
day-trade count limit), LONG-ONLY at this equity (shorting needs margin
approval; leverage needs $2k equity under Alpaca's intraday framework).
Regular hours only in v1 (09:30-16:00 ET): overnight/weekend gap risk is
REAL and accepted at experimental scale — exits cannot execute while the
market is closed. 5-min poll granularity on TP/SL: slippage beyond the stop
is possible on fast moves; position size (~$60/leg) bounds it.

(v11 historical: launched with no demonstrated edge, kill-gate 30 closes OR
14 days; superseded by v12 above at operator directive.)

Modes: cycle | status | scan   (+ --dry-run: full decision path, zero orders)
"""
import json, os, sys, time, datetime, urllib.request, urllib.error, uuid, hashlib, re
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_VERSION = "v13.0-volbrackets"
DRY = "--dry-run" in sys.argv

LOG = os.path.join(HERE, "perp_log.jsonl")
META = os.path.join(HERE, "perp_positions.json")
RISK = os.path.join(HERE, "perp_risk_state.json")
NEWS_STATE = os.path.join(HERE, "news_state.json")
STOP = os.path.join(HERE, "STOP")
STOP_PERP = os.path.join(HERE, "STOP_PERP")
STATUS_FILE = os.path.join(HERE, "perp_status.json")

# ---------------- credentials ----------------
def load_creds():
    env_k, env_s = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if env_k and env_s:
        return {"key": env_k, "secret": env_s}
    p = os.path.expanduser("~/.config/alpaca/credentials.json")
    try:
        d = json.load(open(p))
        if d.get("key") and d.get("secret"):
            return d
    except Exception:
        pass
    sys.stderr.write(
        "\n[alpaca-stocks] missing credentials.\n"
        "Set ALPACA_KEY + ALPACA_SECRET env vars, or write\n"
        "  ~/.config/alpaca/credentials.json  {\"key\": ..., \"secret\": ...}\n"
        "Keys: https://app.alpaca.markets/ (Trading API keys, live).\n\n")
    sys.exit(2)

CREDS = load_creds()
API = "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"

def api(method, path, body=None, base=API, timeout=25):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": CREDS["key"], "APCA-API-SECRET-KEY": CREDS["secret"],
        "Content-Type": "application/json", "User-Agent": "obekt-stocks/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            d = json.loads(raw)
        except Exception:
            d = {"message": raw[:200]}
        raise RuntimeError(f"Alpaca {e.code}: {json.dumps(d)[:250]}")

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def logrec(rec):
    rec = dict(rec)
    rec["ts"] = now_iso()
    rec["engine"] = "stocks"
    rec.setdefault("strategy", "news")
    rec.setdefault("dry", bool(DRY))
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")

# ---------------- strategy parameters ----------------
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
            "MU", "AVGO", "NFLX", "ORCL", "CRM", "PLTR", "COIN", "INTC",
            "QCOM", "TXN", "AMAT", "LRCX", "PANW", "CRWD", "UBER", "ABNB",
            "SHOP", "MRVL", "SMCI", "ARM", "TSM", "SQ",
            # 11-20 widen (operator-default after unanswered clarify): news-heavy
            # liquid mid/high-beta names — more catalyst surface, same gates.
            # All verified tradable+fractionable on Alpaca 11-20 (ROKU dropped:
            # not in the active asset list).
            "SOFI", "HOOD", "RIVN", "LCID", "NIO", "MARA", "RIOT", "CLSK",
            "DKNG", "SNAP", "PINS", "ETSY", "CHWY", "CVNA", "UPST", "AFRM",
            "DASH", "NET", "DDOG", "ZS", "OKTA", "MDB", "SNOW", "MSTR",
            "IREN", "IONQ", "RGTI", "RKLB", "ASTS", "HIMS"]
MAX_OPEN = 3                 # v12 intraday: 3 slots (~equity*0.98/3 per leg)
MAX_NOTIONAL_FRAC = 0.98
MAX_NOTIONAL_USD = 500.0     # bug guard only
CONF_FLOOR = 0.55            # v12: medium-quality catalysts admitted; recalibrate after ~30 calls
NEWS_WINDOW_MIN = 90         # headline freshness window
MAX_LLM_CALLS = 8            # v12: evaluate more of the candidate set (halogen is $0)
MAX_SPREAD_PCT = 10.0        # disaster guard only — IEX BBO spread is NOT the
                             # NBBO (routinely 2-4% on liquid names); Alpaca
                             # routes to the inside market, so this never binds
ASK_VS_LAST_PCT = 2.5        # ghost-quote guard: IEX ask >2.5% off the last
                             # trade means a broken/stale quote, not a market
QUOTE_STALE_S = 90
CHASE_CAP_PCT = 7.0          # don't buy a name already +7% on the day
KNIFE_FLOOR_PCT = -9.0       # don't catch a -9% day even on "good" news
DEEP_DIP_PCT = -4.0          # v13: day_chg <= -4% needs impact=high + conf>=0.70
DEEP_DIP_CONF = 0.70
# v13 volatility geometry: brackets are CODE-computed from per-symbol 14-session
# daily ATR (IEX). v12's LLM-set fixed 0.8% stops sat inside the noise band
# (18/34 stops, 5 within 10 min). SL = 0.35 x ATR% gives each name room to breathe.
ATR_SESSIONS = 14
ATR_SL_FRAC = 0.35
SL_MIN, SL_MAX = 0.6, 2.0    # v13 clamp on the volatility-derived stop
TP_MIN, TP_MAX = 1.0, 3.0    # v13 clamp on the target
TP_SL_RATIO = 1.5            # enforce TP >= 1.5 x SL
ATR_FALLBACK_SL = 1.2        # wider-than-v12 default if ATR fetch fails (logged)
VOL_CACHE = os.path.join(HERE, "vol_cache.json")
MAX_HOLD_H = 6.0             # v12: intraday hold; time exit
COOLDOWN_MIN = 45            # v12: re-entry lockout per symbol after an exit
ENTRY_CUTOFF_ET = (15, 30)   # no new entries after 15:30 ET (let them play out)
FLAT_TIME_ET = (15, 50)      # deterministic flat-by-close: exit everything 15:50 ET
DAILY_LOSS_PCT = 8.0         # software backstop breaker (auto-clears UTC roll)
SELL_REG_FEE_RATE = 0.00003  # SEC+TAF estimate on sells (commission-free venue)
HEARTBEAT_S = 1800

LLM_URL = os.environ.get("LLM_URL", "https://chat.obekt.com/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "halogen-qwen3.8-flash-next")

def llm_key():
    k = os.environ.get("LLM_KEY") or os.environ.get("LITELLM_MASTER_KEY")
    if k:
        return k
    # fallback: a generic per-user gateway credential file (env vars win above)
    try:
        d = json.load(open(os.path.expanduser("~/.config/litellm/credentials.json")))
        return d.get("master_key") or d.get("LITELLM_MASTER_KEY") or ""
    except Exception:
        pass
    return ""

# ---------------- state helpers ----------------
def load_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default

def save_json(p, d):
    if DRY:
        return
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w"), default=str)
    os.replace(tmp, p)

def load_meta():
    return load_json(META, {})

def save_meta(m):
    save_json(META, m)

def halted():
    return os.path.exists(STOP) or os.path.exists(STOP_PERP)

def check_breaker():
    """UTC-rollover auto-clear: a breaker STOP only lasts the day it fired.
    Manual STOP/STOP_PERP (any other content) persists until removed."""
    if not os.path.exists(STOP):
        return
    try:
        content = open(STOP).read().strip()
    except Exception:
        return
    if content.startswith("breaker "):
        day = content.split(" ", 1)[1][:10]
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if day < today:
            os.remove(STOP)
            logrec({"event": "breaker_clear", "why": "UTC rollover", "fired_day": day})

def equity():
    if DRY:
        return float(os.environ.get("PAPER_EQUITY_USD", "120.0"))
    a = api("GET", "/v2/account")
    return float(a.get("equity") or 0)

def clock():
    return api("GET", "/v2/clock")

# ---------------- news ----------------
def load_news_state():
    st = load_json(NEWS_STATE, {"seen": {}, "acted": {}})
    cutoff = time.time() - 48 * 3600
    st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v > cutoff}
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    st["acted"] = {k: v for k, v in st.get("acted", {}).items() if v == today}
    st["exits"] = {k: v for k, v in st.get("exits", {}).items() if time.time() - v < 86400}
    return st

def fetch_news(start_iso):
    """Headlines within the window. Server-side `start` filter (verified
    working 11-20; a transient 0-row failure on 11-19 caused the empty-news
    incident) PLUS a client-side created_at re-filter as belt-and-braces.
    The wire's sort order is by updated_at, NOT created_at — never early-stop
    on the first old item; paginate to the token end or the page cap."""
    cutoff = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    out, token = [], None
    for _ in range(20):
        path = f"/v1beta1/news?limit=50&sort=desc&start={start_iso}"
        if token:
            path += f"&page_token={token}"
        d = api("GET", path, base=DATA)
        items = d.get("news") or []
        if not items:
            break
        for n in items:
            try:
                t = datetime.datetime.fromisoformat((n.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if t >= cutoff:
                out.append(n)
        token = d.get("next_page_token")
        if not token:
            break
    uni = set(UNIVERSE)
    rows = []
    for n in out:
        syms = [s for s in (n.get("symbols") or []) if s in uni]
        if syms:
            rows.append({"id": str(n.get("id")), "headline": n.get("headline") or "",
                         "summary": (n.get("summary") or "")[:400],
                         "source": n.get("source") or "", "created_at": n.get("created_at"),
                         "symbols": syms, "url": n.get("url") or ""})
    # deck news panel cache (read-only consumer: site/generate.py)
    save_json(os.path.join(HERE, "stocks_news.json"),
              [{"ts": r["created_at"], "headline": r["headline"], "symbols": r["symbols"],
                "source": r["source"], "url": r["url"]} for r in rows[:12]])
    return rows

RSS_CACHE = os.path.join(HERE, "rss_cache.json")
RSS_TTL_S = 900  # refresh the Yahoo feeds at most every 15m (rate-limit guard)

def _title_tokens(t):
    stop = {"the", "a", "an", "for", "to", "of", "in", "on", "at", "and",
            "or", "its", "with", "from", "by", "is", "are", "vs", "as"}
    return set(re.findall(r"[a-z0-9]+", t.lower())) - stop

def fetch_rss(cutoff):
    """Second news source: Yahoo per-ticker headline RSS (keyless). Tolerates
    per-feed failure/rate-limit; same row shape as fetch_news. Stable ids
    (md5 of sym+title) so the seen-ledger dedupes across cycles."""
    from email.utils import parsedate_to_datetime
    rows = []
    for sym in UNIVERSE:
        try:
            req = urllib.request.Request(
                f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}",
                headers={"User-Agent": "Mozilla/5.0 (obekt-stocks/1.0)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                xml = r.read().decode("utf-8", "replace")
            for it in re.findall(r"<item>(.*?)</item>", xml, re.S)[:5]:
                tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
                lm = re.search(r"<link>(.*?)</link>", it, re.S)
                pm = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
                if not tm or not pm:
                    continue
                try:
                    t = parsedate_to_datetime(pm.group(1).strip())
                except Exception:
                    continue
                if t < cutoff:
                    continue
                title = re.sub(r"\s+", " ", tm.group(1)).strip()
                import html as _html
                title = _html.unescape(title)
                rows.append({"id": "yahoo-" + hashlib.md5((sym + title).encode()).hexdigest()[:16],
                             "headline": title, "summary": "", "source": "yahoo",
                             "created_at": t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "symbols": [sym], "url": (lm.group(1).strip() if lm else "")})
        except Exception:
            continue
        time.sleep(0.1)
    return rows

def rss_cached(cutoff):
    c = load_json(RSS_CACHE, {})
    if time.time() - c.get("ts", 0) < RSS_TTL_S and c.get("rows") is not None:
        return c["rows"]
    rows = fetch_rss(cutoff)
    save_json(RSS_CACHE, {"ts": time.time(), "rows": rows})
    return rows

def snapshots(symbols):
    if not symbols:
        return {}
    out = {}
    for i in range(0, len(symbols), 20):
        chunk = symbols[i:i + 20]
        d = api("GET", "/v2/stocks/snapshots?symbols=" + ",".join(chunk) + "&feed=iex", base=DATA)
        out.update(d or {})
    return out

def price_context(sym, snap):
    s = snap.get(sym) or {}
    lt = s.get("latestTrade") or {}
    lq = s.get("latestQuote") or {}
    db = s.get("dailyBar") or {}
    pdb = s.get("prevDailyBar") or {}
    px = float(lt.get("p") or db.get("c") or 0)
    prev = float(pdb.get("c") or 0)
    day_open = float(db.get("o") or 0)
    bid, ask = float(lq.get("bp") or 0), float(lq.get("ap") or 0)
    spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100) if bid and ask else 99.0
    day_chg = ((px / prev - 1) * 100) if prev else 0.0
    qts = lq.get("t") or ""
    stale_s = 9999
    try:
        # Alpaca timestamps carry nanoseconds; truncate to microseconds so
        # fromisoformat works on every Python >= 3.7 (else every quote reads
        # stale and the gate rejects the whole universe).
        q = qts.replace("Z", "+00:00")
        if "." in q:
            head, tail = q.split(".", 1)
            frac = ""
            for ch in tail:
                if ch.isdigit():
                    frac += ch
                else:
                    break
            rest = tail[len(frac):]
            q = head + "." + (frac[:6] or "0") + rest
        qt = datetime.datetime.fromisoformat(q)
        stale_s = (datetime.datetime.now(datetime.timezone.utc) - qt).total_seconds()
    except Exception:
        pass
    return {"px": px, "bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3),
            "day_chg_pct": round(day_chg, 2), "day_open": day_open,
            "day_vol": float(db.get("v") or 0), "quote_stale_s": round(stale_s),
            "prev_close": prev}

# ---------------- v13 volatility geometry (code owns brackets) ----------------
def atr_pct(sym):
    """14-session daily ATR as % of last close, IEX feed (free-plan safe).
    Cached per UTC day in vol_cache.json — one bars call per symbol per day.
    Returns None on any failure (caller falls back to ATR_FALLBACK_SL)."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    cache = load_json(VOL_CACHE, {})
    hit = cache.get(sym)
    if hit and hit.get("day") == today and hit.get("atr") is not None:
        return float(hit["atr"])
    try:
        start = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = api("GET", f"/v2/stocks/bars?symbols={sym}&timeframe=1Day&limit={ATR_SESSIONS + 1}"
                       f"&start={start}&feed=iex&adjustment=raw", base=DATA)
        bars = (d.get("bars") or {}).get(sym) or []
        if len(bars) < 5:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i - 1]["c"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        last_c = float(bars[-1]["c"])
        if last_c <= 0:
            return None
        atr = sum(trs[-ATR_SESSIONS:]) / min(len(trs), ATR_SESSIONS) / last_c * 100
        cache[sym] = {"day": today, "atr": round(atr, 4)}
        save_json(VOL_CACHE, cache)
        return atr
    except Exception as e:
        logrec({"event": "stocks_atr_err", "inst": sym, "err": str(e)[:140]})
        return None

def vol_brackets(sym):
    """Code-computed TP/SL from measured volatility. v12 lesson: the LLM
    templated 1.2/0.8 on 32/39 trades; fixed 0.8% stops were inside the noise
    band. Here the stop scales with the name's actual daily range."""
    a = atr_pct(sym)
    if a is None:
        sl = ATR_FALLBACK_SL
        logrec({"event": "stocks_atr_fallback", "inst": sym, "sl_pct": sl})
    else:
        sl = max(SL_MIN, min(SL_MAX, ATR_SL_FRAC * a))
    tp = max(TP_MIN, min(TP_MAX, TP_SL_RATIO * sl))
    if tp < TP_SL_RATIO * sl:
        tp = round(TP_SL_RATIO * sl, 2)
    return round(tp, 2), round(sl, 2), (round(a, 3) if a is not None else None)

# ---------------- halogen decision ----------------
SYS_PROMPT = (
    "You are the entry decision model for a small LIVE INTRADAY US-equity news book "
    "(~$120, long-only, fractional shares, commission-free, FLAT BY THE CLOSE). "
    "You receive ONE fresh news headline about a stock plus its price context. "
    "Decide whether the news is likely to produce a tradable 1-6 hour LONG drift TODAY. "
    "This is an intraday news-momentum desk, not an investment committee: substantive "
    "company-specific news with a plausible same-day price reaction IS tradable — "
    "earnings/guidance surprises, FDA/clinical outcomes, M&A and major contracts, "
    "product launches with a revenue angle, substantive analyst upgrades/downgrades or "
    "price-target changes WITH a thesis, major stakeholder moves, index inclusion, "
    "squeeze/flow signals, and sector news with a clear positive company read-through. "
    "REJECT: listicles and comparisons, rehashed roundups with no new information, "
    "news actually about a DIFFERENT company, macro stories with no company angle, "
    "stale reprints, and negative catalysts (long-only — bad news is not tradable here). "
    "Good news on a stock already up big today may be priced in (chase risk) — weigh it. "
    "A stock already DOWN 4% or more today is falling for a reason: only a HIGH-impact "
    "catalyst that clearly overrides the selloff is tradable there; medium news on a "
    "deep dip is usually noise. "
    "Take-profit and stop levels are set by code from measured volatility — do NOT "
    "output target_pct or stop_pct. Your job is catalyst classification only. "
    "Calibrate confidence across the FULL 0-1 range: 0.9+ only for rare unambiguous "
    "market-moving catalysts; 0.7-0.85 strong substantive catalysts; 0.55-0.7 plausible "
    "but mixed evidence; below 0.55 weak. Do not anchor on one habitual value. "
    "Reply STRICT JSON only: {\"action\":\"buy\"|\"no_trade\",\"confidence\":0.0-1.0,"
    "\"impact\":\"high\"|\"medium\"|\"low\",\"horizon_hours\":number,"
    "\"rationale\":\"<=200 chars\"}. "
    "No prose outside the JSON. Never use double-quote characters inside rationale."
)

def parse_decision(txt):
    """Tolerant JSON extraction: halogen sometimes breaks the rationale string
    (unescaped quotes) or wraps the object in prose. Try direct parse, then the
    outermost brace block, then field-level regex for the numeric/enum fields
    (rationale is optional). Truly garbage output still raises -> fail closed."""
    import re
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
    try:
        return json.loads(txt)
    except Exception:
        pass
    s = txt.find("{")
    if s >= 0:
        depth, instr, esc = 0, False, False
        for i in range(s, len(txt)):
            c = txt[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                instr = not instr
                continue
            if instr:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(txt[s:i + 1])
                    except Exception:
                        break
    def grab(pat):
        m = re.search(pat, txt)
        return m.group(1) if m else None
    action = grab(r'"action"\s*:\s*"(buy|no_trade)"')
    conf = grab(r'"confidence"\s*:\s*([0-9.]+)')
    impact = grab(r'"impact"\s*:\s*"(high|medium|low)"')
    if action and conf and impact:
        return {"action": action, "confidence": float(conf), "impact": impact,
                "target_pct": float(grab(r'"target_pct"\s*:\s*([0-9.]+)') or 0),
                "stop_pct": float(grab(r'"stop_pct"\s*:\s*([0-9.]+)') or 0),
                "horizon_hours": float(grab(r'"horizon_hours"\s*:\s*([0-9.]+)') or 24),
                "rationale": "(field-parsed from malformed JSON)"}
    raise ValueError(f"unparseable: {txt[:150]}")

def halogen_decide(item, ctx, equity_usd, budget):
    user = json.dumps({
        "symbol": item["symbols"][0], "headline": item["headline"],
        "summary": item["summary"], "source": item["source"],
        "news_age_min": round((time.time() - datetime.datetime.fromisoformat(
            item["created_at"].replace("Z", "+00:00")).timestamp()) / 60, 1),
        "price_context": ctx, "account_equity_usd": round(equity_usd, 2),
        "leg_budget_usd": round(budget, 2),
    }, separators=(",", ":"))
    body = {"model": LLM_MODEL, "temperature": 0.2, "max_tokens": 300,
            "messages": [{"role": "system", "content": SYS_PROMPT},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {llm_key()}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    txt = d["choices"][0]["message"]["content"].strip()
    ans = parse_decision(txt)
    # schema check — fail closed on anything malformed. v13: brackets are
    # code-computed from volatility, so the model is NOT asked for them;
    # legacy outputs that still carry them parse fine and are ignored.
    assert ans.get("action") in ("buy", "no_trade"), f"bad action: {str(ans)[:120]}"
    conf = float(ans.get("confidence"))
    assert 0.0 <= conf <= 1.0, f"bad confidence: {str(ans)[:120]}"
    assert ans.get("impact") in ("high", "medium", "low"), f"bad impact: {str(ans)[:120]}"
    tp = float(ans.get("target_pct") or 0); sl = float(ans.get("stop_pct") or 0)
    usage = d.get("usage") or {}
    return {"action": ans["action"], "conf": conf, "impact": ans["impact"],
            "target_pct": tp, "stop_pct": sl,
            "horizon_hours": float(ans.get("horizon_hours") or 24),
            "rationale": str(ans.get("rationale") or "")[:200],
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens")}

# ---------------- execution ----------------
def submit_order(sym, qty, side):
    body = {"symbol": sym, "qty": str(qty), "side": side, "type": "market",
            "time_in_force": "day",
            "client_order_id": f"obekt-news-{sym}-{side}-{uuid.uuid4().hex[:12]}"}
    od = api("POST", "/v2/orders", body)
    oid = od.get("id")
    # poll for fill (market orders on liquid names fill in seconds)
    for _ in range(10):
        time.sleep(2)
        o = api("GET", f"/v2/orders/{oid}")
        st = o.get("status")
        if st == "filled":
            return o, None
        if st in ("canceled", "expired", "rejected"):
            return None, st
    o = api("GET", f"/v2/orders/{oid}")
    if o.get("status") == "filled":
        return o, None
    # partially filled or pending: cancel the rest, book what filled
    try:
        api("DELETE", f"/v2/orders/{oid}")
    except Exception:
        pass
    time.sleep(2)
    o = api("GET", f"/v2/orders/{oid}")
    if float(o.get("filled_qty") or 0) > 0:
        return o, "partial"
    return None, o.get("status") or "unfilled"

def open_one(sym, item, ctx, ans, eq, budget=None):
    if budget is None:
        budget = min(eq * MAX_NOTIONAL_FRAC / MAX_OPEN, MAX_NOTIONAL_USD)
    ask = ctx["ask"] or ctx["px"]
    if ask <= 0:
        logrec({"event": "perp_gate_reject", "inst": sym, "why": "no ask price"})
        return False
    qty = round(budget / ask, 4)
    notional = qty * ask
    if notional < 1.0:
        logrec({"event": "perp_gate_reject", "inst": sym, "why": f"notional {notional:.2f} < $1 min"})
        return False
    # v13: brackets arrive code-computed from volatility (cycle fills them in
    # before this call); the clamp here is a bug guard, not the LLM leash.
    tp = max(TP_MIN, min(TP_MAX, ans["target_pct"]))
    sl = max(SL_MIN, min(SL_MAX, ans["stop_pct"]))
    if tp < TP_SL_RATIO * sl:
        tp = round(TP_SL_RATIO * sl, 2)
    bk = {"cost_pct": 0.01, "tp_pct": tp, "sl_pct": sl,
          "trail_arm_pct": 999.0, "trail_giveback_pct": 999.0, "trail_floor_pct": 999.0}
    if DRY:
        logrec({"event": "perp_dry_open", "inst": sym, "side": "long", "contracts": qty,
                "px": ask, "notional_usd": round(notional, 2), "bracket": bk,
                "headline": item["headline"], "conf": ans["conf"]})
        return True
    od, err = submit_order(sym, qty, "buy")
    if od is None:
        logrec({"event": "perp_entry_unfilled", "inst": sym, "side": "long", "err": str(err)})
        return False
    fill_px = float(od.get("filled_avg_price") or ask)
    filled_qty = float(od.get("filled_qty") or qty)
    notional = filled_qty * fill_px
    meta = load_meta()
    meta[sym] = {"opened_ts": now_iso(), "side": "long", "contracts": filled_qty,
                 "entry_ref": fill_px, "notional_usd": round(notional, 2),
                 "strategy": "news", "news_headline": item["headline"],
                 "news_rationale": ans["rationale"], "jev_conf": ans["conf"],
                 "jev_margin": None, "jev_noul": None, "bracket": bk,
                 "hwm_pct": 0.0, "algo_ok": False, "entry_fee": 0.0,
                 "entry_ordId": od.get("id"), "max_hold_h": MAX_HOLD_H,
                 "atr_pct": ans.get("atr_pct")}
    save_meta(meta)
    logrec({"event": "perp_open", "inst": sym, "side": "long", "contracts": filled_qty,
            "notional_usd": round(notional, 2), "fill_px": fill_px, "conf": ans["conf"],
            "margin": None, "noul": None, "algo_ok": False, "realized_fee": 0.0,
            "entry_type": "market", "headline": item["headline"], "bracket": bk,
            "atr_pct": ans.get("atr_pct")})
    return True

def close_one(sym, m, reason):
    if DRY:
        logrec({"event": "perp_dry_close", "inst": sym, "reason": reason})
        return
    od, err = submit_order(sym, m["contracts"], "sell")
    exit_px, fee = None, 0.0
    if od is not None:
        exit_px = float(od.get("filled_avg_price") or 0) or None
        fee = round(float(od.get("filled_qty") or 0) * (exit_px or 0) * SELL_REG_FEE_RATE, 4)
    entry = float(m["entry_ref"])
    if exit_px is None:
        try:
            snap = snapshots([sym])
            exit_px = price_context(sym, snap)["px"] or entry
        except Exception:
            exit_px = entry
    upc = (exit_px - entry) / entry * 100 if entry else 0.0
    opened = datetime.datetime.fromisoformat(m["opened_ts"])
    age_min = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 60
    logrec({"event": "perp_close", "inst": sym, "side": "long", "reason": reason,
            "upc": round(upc, 2), "age_min": round(age_min, 1), "fill_px": exit_px,
            "realized_fee": fee, "notional_usd": m.get("notional_usd"),
            "ordId": (od or {}).get("id"), "err": err})
    meta = load_meta()
    meta.pop(sym, None)
    save_meta(meta)
    nst = load_json(NEWS_STATE, {})
    nst.setdefault("exits", {})[sym] = time.time()
    save_json(NEWS_STATE, nst)

def reconcile(meta):
    """Adopt exchange positions without meta; detect vanished ones. Same
    safety net as the OKX book: never fly blind."""
    try:
        positions = api("GET", "/v2/positions") or []
    except Exception as e:
        logrec({"event": "stocks_reconcile_err", "err": str(e)[:140]})
        return meta
    held = {}
    for p in positions:
        sym = p.get("symbol")
        if sym in UNIVERSE and p.get("asset_class") == "us_equity":
            held[sym] = p
    for sym, p in held.items():
        if sym not in meta:
            entry = float(p.get("avg_entry_price") or 0)
            qty = float(p.get("qty") or 0)
            meta[sym] = {"opened_ts": now_iso(), "side": "long", "contracts": qty,
                         "entry_ref": entry, "notional_usd": round(qty * entry, 2),
                         "strategy": "news", "bracket": {"cost_pct": 0.01,
                         "tp_pct": 1.5, "sl_pct": 1.0, "trail_arm_pct": 999.0,
                         "trail_giveback_pct": 999.0, "trail_floor_pct": 999.0},
                         "hwm_pct": 0.0, "algo_ok": False, "adopted_orphan": True,
                         "max_hold_h": MAX_HOLD_H}
            save_meta(meta)
            logrec({"event": "stocks_orphan_adopted", "inst": sym, "contracts": qty,
                    "entry_ref": entry})
    for sym in list(meta.keys()):
        if sym not in held:
            m = meta.pop(sym)
            save_meta(meta)
            logrec({"event": "perp_close_detected", "inst": sym, "side": "long",
                    "upc": None, "notional_usd": m.get("notional_usd"),
                    "note": "position gone externally — reconcile"})
    return meta

def manage(eq):
    meta = reconcile(load_meta())
    if not meta:
        return meta
    try:
        is_open = bool(clock().get("is_open"))
    except Exception:
        is_open = False
    try:
        snaps = snapshots(list(meta.keys()))
    except Exception as e:
        logrec({"event": "stocks_snap_err", "err": str(e)[:140]})
        snaps = {}
    for sym, m in list(meta.items()):
        ctx = price_context(sym, snaps)
        px = ctx["px"]
        entry = float(m["entry_ref"])
        if not px or not entry:
            continue
        upc = (px - entry) / entry * 100
        bk = m.get("bracket") or {}
        tp = float(bk.get("tp_pct") or 2.5)
        sl = float(bk.get("sl_pct") or 1.5)
        opened = datetime.datetime.fromisoformat(m["opened_ts"])
        age_h = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 3600
        reason = None
        if upc >= tp:
            reason = f"TP {upc:.2f}% >= {tp}%"
        elif upc <= -sl:
            reason = f"SL {upc:.2f}% <= -{sl}%"
        elif age_h >= float(m.get("max_hold_h") or MAX_HOLD_H):
            reason = f"time exit {age_h:.1f}h"
        if reason:
            if is_open:
                logrec({"event": "stocks_exit_signal", "inst": sym, "upc": round(upc, 2),
                        "reason": reason})
                close_one(sym, m, reason)
            else:
                logrec({"event": "stocks_exit_deferred", "inst": sym, "upc": round(upc, 2),
                        "reason": reason, "why": "market closed — executes at next open"})
    return load_meta()

# ---------------- scan + cycle ----------------
def scan(eq, force=False, window_min=NEWS_WINDOW_MIN):
    """News scan + universe day-mover table (mom_scan shape for the deck)."""
    st = load_news_state()
    start = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=window_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        news = fetch_news(start)
    except Exception as e:
        logrec({"event": "news_fetch_err", "err": str(e)[:160]})
        news = []
    cutoff = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    try:
        rss = rss_cached(cutoff)
    except Exception as e:
        logrec({"event": "rss_fetch_err", "err": str(e)[:160]})
        rss = []
    # cross-source dedupe: drop RSS items whose title overlaps an Alpaca wire
    # item (>=0.7 token overlap) — keep the wire version (it has the summary)
    wire_toks = [_title_tokens(n["headline"]) for n in news]
    rss_uniq, rss_toks = [], []
    for n in rss:
        tk = _title_tokens(n["headline"])
        def dup(tks):
            for ot in tks:
                if tk and ot and len(tk & ot) / min(len(tk), len(ot)) >= 0.7:
                    return True
            return False
        if dup(wire_toks) or dup(rss_toks):
            continue
        rss_uniq.append(n)
        rss_toks.append(tk)
    news = news + rss_uniq
    fresh = [n for n in news if n["id"] not in st["seen"]]
    snaps = snapshots(UNIVERSE)
    movers = []
    for sym in UNIVERSE:
        ctx = price_context(sym, snaps)
        if ctx["px"]:
            movers.append((sym, ctx["day_chg_pct"]))
    movers.sort(key=lambda r: -r[1])
    cands = []
    now_ts = time.time()
    exits = st.get("exits", {})
    held = set(load_meta().keys())  # v13: no pyramiding — meta is symbol-keyed,
    # a second entry while held overwrote the first lot's exit management (v12 bug)
    for n in fresh:
        for sym in n["symbols"]:
            if sym in held:
                continue
            last_exit = exits.get(sym)
            if last_exit and now_ts - last_exit < COOLDOWN_MIN * 60:
                continue  # v12 re-entry cooldown (replaces the daily lockout)
            if any(c["item"]["symbols"][0] == sym for c in cands):
                continue
            ctx = price_context(sym, snaps)
            if ctx["px"] <= 0:
                continue  # no live price (halted/unlisted) — never trade blind
            cands.append({"item": n, "sym": sym, "ctx": ctx})
    logrec({"event": "mom_scan", "n_universe": len(UNIVERSE), "n_ranked": len(cands),
            "signal": "news", "thr_pct": None, "n_fresh_headlines": len(fresh),
            "n_rss": len(rss_uniq), "n_wire": len(news) - len(rss_uniq),
            "legs": [(c["sym"], c["ctx"]["day_chg_pct"], "long") for c in cands[:MAX_OPEN]],
            "longs": [(c["sym"], c["ctx"]["day_chg_pct"]) for c in cands[:MAX_OPEN]],
            "shorts": [],
            "top": movers[:5], "bottom": movers[-5:]})
    logrec({"event": "perp_universe", "n_perps": len(UNIVERSE), "n_cands": len(cands),
            "selected": [c["sym"] for c in cands[:MAX_LLM_CALLS]], "top": movers[:5]})
    return st, cands, snaps

def cycle():
    check_breaker()
    if halted():
        logrec({"event": "perp_skip", "why": "halted (STOP or STOP_PERP)"})
        return {"halted": True}
    eq = equity()
    # daily breaker
    risk = load_json(RISK, {})
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if risk.get("day") != today:
        risk = {"day": today, "day_equity_start": eq}
        save_json(RISK, risk)
    start_eq = float(risk.get("day_equity_start") or eq)
    if start_eq > 0 and eq <= start_eq * (1 - DAILY_LOSS_PCT / 100):
        if not os.path.exists(STOP):
            open(STOP, "w").write(f"breaker {today}")
            logrec({"event": "breaker_fire", "equity": eq, "day_start": start_eq,
                    "loss_pct": round((eq / start_eq - 1) * 100, 2)})
        return {"halted": True, "why": "breaker"}
    meta = manage(eq)
    out = {"opened": [], "skipped": [], "equity": round(eq, 2)}
    try:
        is_open = bool(clock().get("is_open"))
    except Exception as e:
        logrec({"event": "clock_err", "err": str(e)[:120]})
        is_open = False
    now_et = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    et_hm = (now_et.hour, now_et.minute)
    # v12 deterministic intraday clock: flat everything 10 min before the close,
    # no new entries in the last 30 min. Overnight gap risk is designed OUT.
    if is_open and et_hm >= FLAT_TIME_ET and meta:
        n_flat = len(meta)
        for sym, m in list(meta.items()):
            close_one(sym, m, "flat-by-close (15:50 ET rule)")
        meta = load_meta()
        logrec({"event": "flat_by_close", "n_closed": n_flat, "et": now_et.strftime("%H:%M")})
    entries_allowed = is_open and et_hm < ENTRY_CUTOFF_ET
    # On the closed->open transition widen the window to 18h so overnight /
    # pre-market catalysts are visible at the open (the 90m window alone would
    # drop everything published before ~08:00 ET).
    prev_open = load_news_state().get("last_open_state", False)
    window = 1080 if (is_open and not prev_open) else NEWS_WINDOW_MIN
    try:
        st, cands, snaps = scan(eq, window_min=window)
    except Exception as e:
        logrec({"event": "news_scan_err", "err": str(e)[:200]})
        st, cands = load_news_state(), []
    if not is_open:
        out["skipped"].append("market closed — news accumulated, entries at next open")
        logrec({"event": "perp_no_trade", "why": "market closed"})
    elif not entries_allowed:
        out["skipped"].append("entry cutoff 15:30 ET passed — flat-by-close regime")
        logrec({"event": "perp_no_trade", "why": "entry cutoff 15:30 ET passed"})
    elif len(meta) >= MAX_OPEN:
        out["skipped"].append(f"MAX_OPEN {MAX_OPEN} reached")
        logrec({"event": "perp_no_trade", "why": f"MAX_OPEN {MAX_OPEN} reached"})
    else:
        # v13: clamp the leg budget to actual cash — v12 sized off equity alone,
        # so once cash was spent every buy raised an unhandled Alpaca 40310000
        # and killed the cycle before status() (stale-snapshot IDLE alarms).
        if DRY:
            cash = eq
        else:
            try:
                cash = float(api("GET", "/v2/account").get("cash") or 0)
            except Exception:
                cash = eq
        budget = min(eq * MAX_NOTIONAL_FRAC / MAX_OPEN, cash * 0.98, MAX_NOTIONAL_USD)
        if budget < 1.0:
            out["skipped"].append(f"cash ${cash:.2f} — no $1-min leg possible")
            logrec({"event": "perp_no_trade", "why": f"insufficient cash {cash:.2f}"})
            save_json(NEWS_STATE, st)
            cands = []
        calls = 0
        for c in cands:
            if calls >= MAX_LLM_CALLS or len(load_meta()) >= MAX_OPEN:
                break
            sym, item, ctx = c["sym"], c["item"], c["ctx"]
            # deterministic pre-gates (before spending an LLM call)
            fails = []
            if ctx["spread_pct"] > MAX_SPREAD_PCT:
                fails.append(f"spread {ctx['spread_pct']}% > {MAX_SPREAD_PCT}%")
            if ctx["px"] and ctx["ask"] and abs(ctx["ask"] / ctx["px"] - 1) * 100 > ASK_VS_LAST_PCT:
                fails.append(f"ask deviates {abs(ctx['ask'] / ctx['px'] - 1) * 100:.2f}% from last")
            if ctx["quote_stale_s"] > QUOTE_STALE_S:
                fails.append(f"quote stale {ctx['quote_stale_s']}s")
            if ctx["day_chg_pct"] > CHASE_CAP_PCT:
                fails.append(f"chase +{ctx['day_chg_pct']}% > {CHASE_CAP_PCT}%")
            if ctx["day_chg_pct"] < KNIFE_FLOOR_PCT:
                fails.append(f"knife {ctx['day_chg_pct']}%")
            if fails:
                st["seen"][item["id"]] = time.time()
                logrec({"event": "perp_gate_reject", "inst": sym, "why": "; ".join(fails),
                        "headline": item["headline"]})
                continue
            calls += 1
            try:
                ans = halogen_decide(item, ctx, eq, budget)
            except Exception as e:
                logrec({"event": "news_llm_err", "inst": sym, "err": str(e)[:200],
                        "headline": item["headline"]})
                logrec({"event": "perp_no_trade", "inst": sym, "why": "LLM fail-closed"})
                st["seen"][item["id"]] = time.time()
                continue
            # v13: brackets are code-computed from measured volatility BEFORE
            # the decision event, so the deck shows the real exit geometry.
            if ans["action"] == "buy":
                tp, sl, atr = vol_brackets(sym)
                ans["target_pct"], ans["stop_pct"], ans["atr_pct"] = tp, sl, atr
            # perp_jev-shaped event: feeds the deck's decision panels unchanged
            logrec({"event": "perp_jev", "choice": f"buy_{sym}" if ans["action"] == "buy" else "no_trade",
                    "chosen_inst": sym if ans["action"] == "buy" else None,
                    "chosen_side": "long" if ans["action"] == "buy" else None,
                    "conf": ans["conf"], "noul": ans["conf"],
                    "probabilities": {f"buy_{sym}": ans["conf"]},
                    "pairs": [{"inst": sym, "base": sym.lower(),
                               "headline": item["headline"], "impact": ans["impact"],
                               "day_chg_pct": ctx["day_chg_pct"],
                               "target_pct": ans["target_pct"], "stop_pct": ans["stop_pct"]}],
                    "collateral_usdc": round(eq, 2), "budget_usd": round(budget, 2),
                    "input_tokens": ans["input_tokens"], "output_tokens": ans["output_tokens"],
                    "n_pairs": 1, "rationale": ans["rationale"], "headline": item["headline"]})
            st["seen"][item["id"]] = time.time()
            if ans["action"] != "buy":
                logrec({"event": "perp_no_trade", "inst": sym, "why": ans["rationale"][:160]})
                continue
            gate_fails = []
            if ans["conf"] < CONF_FLOOR:
                gate_fails.append(f"conf {ans['conf']:.2f} < {CONF_FLOOR}")
            if ans["impact"] == "low":
                gate_fails.append("impact low")
            # v13 deep-dip rule: medium news on a -4%+ day was a coin flip with
            # instant stops in v12; require a high-impact override + conviction.
            if ctx["day_chg_pct"] <= DEEP_DIP_PCT and (
                    ans["impact"] != "high" or ans["conf"] < DEEP_DIP_CONF):
                gate_fails.append(f"deep dip {ctx['day_chg_pct']}% needs impact=high + conf>={DEEP_DIP_CONF}")
            if gate_fails:
                logrec({"event": "perp_gate_reject", "inst": sym, "why": "; ".join(gate_fails)})
                continue
            # v13 fail-soft: an entry error (e.g. Alpaca 40310000 insufficient
            # buying power) must not kill the cycle before status() — log it,
            # mark the headline seen, keep managing the book.
            try:
                if open_one(sym, item, ctx, ans, eq, budget):
                    out["opened"].append(("long", sym))
            except Exception as e:
                logrec({"event": "perp_entry_err", "inst": sym, "err": str(e)[:200],
                        "headline": item["headline"]})
        save_json(NEWS_STATE, st)
    # persist market-state transition flag + seen/acted ids every cycle
    st["last_open_state"] = is_open
    save_json(NEWS_STATE, st)
    # heartbeat (deck liveness strip)
    nst = load_json(NEWS_STATE, {})
    last_hb = nst.get("last_heartbeat_ts")
    hb_age = 1e9 if not last_hb else (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.datetime.fromisoformat(last_hb)).total_seconds()
    if hb_age >= HEARTBEAT_S:
        logrec({"event": "mom_heartbeat", "why": "news book alive",
                "n_positions": len(load_meta()), "market_open": is_open})
        nst["last_heartbeat_ts"] = now_iso()
        save_json(NEWS_STATE, nst)
    try:
        status()
    except Exception as e:
        logrec({"event": "stocks_status_err", "err": str(e)[:160]})
    return out

# ---------------- status ----------------
def status():
    meta = load_meta()
    eq = equity()
    out = {"engine": ENGINE_VERSION, "usdc_equity": round(eq, 2),
           "eur_float": 0.0, "account_total_usd": round(eq, 2),
           "collateral_ccy": "USD", "halted": halted(), "dry": DRY,
           "positions": [], "algos_pending": 0, "strategy": "news",
           "venue": "Alpaca US equities"}
    try:
        snaps = snapshots(list(meta.keys())) if meta else {}
    except Exception:
        snaps = {}
    for sym, m in meta.items():
        row = dict(m)
        row["inst"] = sym
        ctx = price_context(sym, snaps)
        if ctx["px"]:
            row["last"] = ctx["px"]
            entry = float(m["entry_ref"])
            row["upc"] = round((ctx["px"] - entry) / entry * 100, 2) if entry else None
        out["positions"].append(row)
    # last decision for the deck
    last_jev = None
    try:
        with open(LOG, errors="replace") as f:
            for line in f:
                if '"perp_jev"' in line:
                    try:
                        e = json.loads(line)
                        if e.get("event") == "perp_jev":
                            last_jev = e
                    except Exception:
                        pass
    except Exception:
        pass
    out["last_jev"] = last_jev
    out["breaked"] = os.path.exists(STOP) and open(STOP).read().startswith("breaker")
    snap = {"engine": out["engine"], "usdc_equity": out["usdc_equity"],
            "eur_float": 0.0, "account_total_usd": out["account_total_usd"],
            "collateral_ccy": "USD", "halted": out["halted"], "dry": DRY,
            "n_positions": len(out["positions"]), "algos_pending": 0,
            "ts": now_iso(), "positions": out["positions"],
            "strategy": "news", "venue": "Alpaca US equities"}
    save_json(STATUS_FILE, snap)
    return out

def main():
    mode = next((a for a in sys.argv[1:] if not a.startswith("--")), "cycle")
    if mode == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif mode == "scan":
        eq = equity()
        st, cands, snaps = scan(eq, force=True)
        print(json.dumps({"equity": round(eq, 2),
                          "candidates": [{"sym": c["sym"], "headline": c["item"]["headline"],
                                          "ctx": c["ctx"]} for c in cands]}, indent=1))
    else:
        print(json.dumps(cycle(), indent=1, default=str))

if __name__ == "__main__":
    main()
