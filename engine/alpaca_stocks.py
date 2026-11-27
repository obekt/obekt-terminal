#!/usr/bin/env python3
"""alpaca_stocks.py — v13.1-volbrackets: INTRADAY NEWS-MOMENTUM US EQUITY engine on Alpaca.

v13.1 (2026-11-27, AUDIT BUG-FIX RELEASE on top of v13.0-volbrackets):
Fixes verified mechanical & algorithmic bugs found in live production logs:
  - DYNAMIC CLOSE CLOCK: uses Alpaca /v2/clock next_close (FLAT <=10m before
    close, no entries <=30m before close) instead of hardcoded 15:50/15:30 ET,
    fixing half-day early closes (Black Friday 13:00 ET stranded 3 positions
    over Thanksgiving weekend).
  - OPENING-BELL PROTECTION: OPEN_SETTLE_MIN = 15 (no new entries and no forced
    time-exit before 09:45 ET while TP/SL stay active at 09:30 ET) — eliminates
    the 09:31 ET opening-imbalance wipeout and protects overnight carries from
    09:30 auction-spread dumps.
  - ATR SORT/SPLIT FIX: atr_pct() now requests sort=desc, adjustment=split,
    end=yesterday 23:59Z, and reverses bars before TR computation (Alpaca
    defaulted to sort=asc from now-30d, dropping the latest 9 days of bars).
  - MULTI-SYMBOL BUG FIX: halogen_decide() receives the candidate's actual `sym`
    instead of item["symbols"][0]; scan() dedupes on c["sym"]; seen keys track
    f"{id}:{sym}"; multi-ticker roundups (>3 symbols) filtered pre-LLM.
  - 18H OVERNIGHT WINDOW & RSS CACHE FIX: rss_cached() invalidates when a wider
    window is requested; overnight 18h window triggers when entries actually
    open (09:45 ET); candidates sorted (company-specific, wire-first, freshest)
    before MAX_LLM_CALLS; skips 60-ticker Yahoo RSS polling while market closed.
  - RE-ENTRY, CHASE & PROMPT FIXES: SL_COOLDOWN_MIN = 240 (4h lockout after SL,
    preventing same-story reprint re-entries); CHASE_CAP_PCT 7.0 -> 3.5; prompt
    consistency rule bans buying when rationale argues vague/priced-in/roundup
    and removes midpoint confidence anchors.
  - EXECUTION & EXIT HARDENING: close_one() keeps meta intact if sell fails;
    manage() uses broker /v2/positions current_price (consolidated NBBO) first,
    tracks & persists hwm_pct, and enables breakeven/trailing protection once
    hwm >= 0.65*TP; cycle() decrements cash across multi-entry fills and saves
    seen state incrementally; status() tails perp_log.jsonl (O(1)) and writes
    market_open for watchdog stranded-position alarms.

v13.0 (2026-11-25, KILL-GATE REPLACEMENT of v12 — pre-registered gate fired:
34 closes >= 30 target, live realized net -$0.09 <= 0).
KILL-GATE v13 (pre-registered 11-25): 30 closed trades OR 7 days (~12-02),
whichever FIRST, live realized net <= 0 -> replace (operator standing rule).

Modes: cycle | status | scan   (+ --dry-run: full decision path, zero orders)
"""
import json, os, sys, time, datetime, urllib.request, urllib.error, uuid, hashlib, re
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_VERSION = "v13.1-volbrackets"
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
NY_TZ = ZoneInfo("America/New_York")

def api(method, path, body=None, base=API, timeout=25):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": CREDS["key"], "APCA-API-SECRET-KEY": CREDS["secret"],
        "Content-Type": "application/json", "User-Agent": "obekt-stocks/1.1"})
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

def parse_iso(ts_str):
    """Parse RFC3339 timestamps including Alpaca nanosecond fractions."""
    q = (ts_str or "").strip().replace("Z", "+00:00")
    if not q:
        raise ValueError("empty timestamp")
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
    dt = datetime.datetime.fromisoformat(q)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt

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
            "SOFI", "HOOD", "RIVN", "LCID", "NIO", "MARA", "RIOT", "CLSK",
            "DKNG", "SNAP", "PINS", "ETSY", "CHWY", "CVNA", "UPST", "AFRM",
            "DASH", "NET", "DDOG", "ZS", "OKTA", "MDB", "SNOW", "MSTR",
            "IREN", "IONQ", "RGTI", "RKLB", "ASTS", "HIMS"]
MAX_OPEN = 3                 # 3 slots (~equity*0.96/3 per leg, clamped to cash)
MAX_NOTIONAL_FRAC = 0.96     # 4% headroom prevents market-buy 40310000 slippage rejects
MAX_NOTIONAL_USD = 500.0     # bug guard only
CONF_FLOOR = 0.55            # post-gate floor
NEWS_WINDOW_MIN = 90         # headline freshness window
MAX_LLM_CALLS = 8            # cap LLM evaluations per cycle
MAX_HEADLINE_SYMS = 3        # skip >3-symbol roundup spam before LLM
MAX_SPREAD_PCT = 10.0        # disaster guard only (IEX BBO is not NBBO)
ASK_VS_LAST_PCT = 2.5        # ghost-quote guard: IEX ask >2.5% off last trade
QUOTE_STALE_S = 90
CHASE_CAP_PCT = 3.5          # v13.1: >+3.5% chase entries lost -$0.94; +2..+3.5% was +$2.03
KNIFE_FLOOR_PCT = -9.0       # don't catch a -9% day even on "good" news
DEEP_DIP_PCT = -4.0          # day_chg <= -4% needs impact=high + conf>=0.70
DEEP_DIP_CONF = 0.70
# Volatility geometry: code-computed brackets from per-symbol 14-session daily ATR
ATR_SESSIONS = 14
ATR_SL_FRAC = 0.35
SL_MIN, SL_MAX = 0.6, 2.0
TP_MIN, TP_MAX = 1.0, 3.0
TP_SL_RATIO = 1.5
ATR_FALLBACK_SL = 1.2
# Trailing/breakeven protection: once HWM reaches 65% of TP, protect profits
TRAIL_ARM_FRAC = 0.65
TRAIL_GIVEBACK_SL_FRAC = 0.50
TRAIL_FLOOR_PCT = 0.15
VOL_CACHE = os.path.join(HERE, "vol_cache.json")
MAX_HOLD_H = 6.0             # intraday hold; time exit
COOLDOWN_MIN = 60            # post-TP / post-time-exit per-symbol cooldown
SL_COOLDOWN_MIN = 240        # v13.1: 4h lockout after SL (blocks same-story reprint chases)
OPEN_SETTLE_MIN = 15         # v13.1: no entries or time-exits in 09:30-09:45 ET auction chaos
ENTRY_CUTOFF_ET = (15, 30)   # fallback regular-day cutoff (dynamic clock overrides)
FLAT_TIME_ET = (15, 50)      # fallback regular-day flat (dynamic clock overrides)
ENTRY_CUTOFF_BEFORE_CLOSE_MIN = 30  # dynamic: stop entries <=30m before clock.next_close
FLAT_BEFORE_CLOSE_MIN = 10          # dynamic: flatten <=10m before clock.next_close
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
    with open(tmp, "w") as f:
        json.dump(d, f, default=str)
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

def clock_info(clk=None, now_utc=None):
    """Derive dynamic intraday boundaries from Alpaca /v2/clock (handles early
    closes like Black Friday 13:00 ET, Christmas Eve, July 3). Falls back to
    ENTRY_CUTOFF_ET / FLAT_TIME_ET only if next_close cannot be parsed."""
    if clk is None:
        clk = clock()
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    is_open = bool(clk.get("is_open"))
    now_et = now_utc.astimezone(NY_TZ)
    et_hm = (now_et.hour, now_et.minute)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_since_open = (now_et - open_et).total_seconds() / 60.0
    mins_to_close = None
    next_close_str = clk.get("next_close")
    if is_open and next_close_str:
        try:
            nc = parse_iso(next_close_str)
            mins_to_close = (nc.astimezone(datetime.timezone.utc) - now_utc).total_seconds() / 60.0
        except Exception:
            mins_to_close = None
    if is_open:
        if mins_to_close is not None:
            should_flat = mins_to_close <= FLAT_BEFORE_CLOSE_MIN
            before_entry_cutoff = mins_to_close > ENTRY_CUTOFF_BEFORE_CLOSE_MIN
        else:
            should_flat = et_hm >= FLAT_TIME_ET
            before_entry_cutoff = et_hm < ENTRY_CUTOFF_ET
        settled_open = mins_since_open >= OPEN_SETTLE_MIN
        entries_allowed = settled_open and before_entry_cutoff and not should_flat
        time_exit_allowed = settled_open
    else:
        should_flat = False
        entries_allowed = False
        time_exit_allowed = False
    return {
        "is_open": is_open,
        "now_et": now_et,
        "et_hm": et_hm,
        "mins_since_open": round(mins_since_open, 1),
        "mins_to_close": round(mins_to_close, 1) if mins_to_close is not None else None,
        "next_close": next_close_str,
        "should_flat": should_flat,
        "entries_allowed": entries_allowed,
        "time_exit_allowed": time_exit_allowed,
    }

# ---------------- news ----------------
def _exit_ts(val):
    if isinstance(val, dict):
        return float(val.get("ts") or 0)
    try:
        return float(val or 0)
    except Exception:
        return 0.0

def _exit_is_sl(val):
    return isinstance(val, dict) and bool(val.get("sl"))

def load_news_state():
    st = load_json(NEWS_STATE, {"seen": {}, "acted": {}, "exits": {}})
    now_t = time.time()
    cutoff = now_t - 48 * 3600
    st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v > cutoff}
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    st["acted"] = {k: v for k, v in st.get("acted", {}).items() if v == today}
    st["exits"] = {k: v for k, v in st.get("exits", {}).items() if now_t - _exit_ts(v) < 86400}
    return st

def fetch_news(start_iso):
    """Headlines within the window. Server-side `start` filter PLUS client-side
    created_at re-filter; paginate to token end (20-page cap) without early-stop
    because the wire sorts by updated_at, not created_at."""
    cutoff = parse_iso(start_iso)
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
                t = parse_iso(n.get("created_at") or "")
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
        all_syms = n.get("symbols") or []
        syms = [s for s in all_syms if s in uni]
        if syms:
            rows.append({"id": str(n.get("id")), "headline": n.get("headline") or "",
                         "summary": (n.get("summary") or "")[:400],
                         "source": n.get("source") or "", "created_at": n.get("created_at"),
                         "symbols": syms, "n_raw_symbols": len(all_syms),
                         "url": n.get("url") or ""})
    save_json(os.path.join(HERE, "stocks_news.json"),
              [{"ts": r["created_at"], "headline": r["headline"], "symbols": r["symbols"],
                "source": r["source"], "url": r["url"]} for r in rows[:12]])
    return rows

RSS_CACHE = os.path.join(HERE, "rss_cache.json")
RSS_TTL_S = 900  # refresh Yahoo feeds at most every 15m (rate-limit guard)

def _title_tokens(t):
    stop = {"the", "a", "an", "for", "to", "of", "in", "on", "at", "and",
            "or", "its", "with", "from", "by", "is", "are", "vs", "as"}
    return set(re.findall(r"[a-z0-9]+", t.lower())) - stop

def fetch_rss(cutoff):
    """Second news source: Yahoo per-ticker headline RSS (keyless). Stable ids
    (md5 of sym+title) so the seen-ledger dedupes across cycles."""
    from email.utils import parsedate_to_datetime
    import html as _html
    rows = []
    for sym in UNIVERSE:
        try:
            req = urllib.request.Request(
                f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}",
                headers={"User-Agent": "Mozilla/5.0 (obekt-stocks/1.1)"})
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
                title = _html.unescape(re.sub(r"\s+", " ", tm.group(1)).strip())
                rows.append({"id": "yahoo-" + hashlib.md5((sym + title).encode()).hexdigest()[:16],
                             "headline": title, "summary": "", "source": "yahoo",
                             "created_at": t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "symbols": [sym], "n_raw_symbols": 1,
                             "url": (lm.group(1).strip() if lm else "")})
        except Exception:
            continue
        time.sleep(0.1)
    return rows

def rss_cached(cutoff):
    """Cached Yahoo RSS fetch. v13.1: invalidates cache when caller requests a
    wider lookback window (e.g. 90m -> 1080m overnight sweep at market open)."""
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    req_window_min = max(0.0, (now_dt - cutoff).total_seconds() / 60.0)
    c = load_json(RSS_CACHE, {})
    cached_win = float(c.get("window_min") or 0.0)
    if (time.time() - c.get("ts", 0) < RSS_TTL_S
            and c.get("rows") is not None
            and cached_win >= req_window_min - 5.0):
        # filter cached rows to requested cutoff in case cache held a wider window
        filtered = []
        for r in c["rows"]:
            try:
                if parse_iso(r.get("created_at") or "") >= cutoff:
                    filtered.append(r)
            except Exception:
                continue
        return filtered
    rows = fetch_rss(cutoff)
    save_json(RSS_CACHE, {"ts": time.time(), "window_min": round(req_window_min, 1), "rows": rows})
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
        qt = parse_iso(qts)
        stale_s = (datetime.datetime.now(datetime.timezone.utc) - qt).total_seconds()
    except Exception:
        pass
    return {"px": px, "bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3),
            "day_chg_pct": round(day_chg, 2), "day_open": day_open,
            "day_vol": float(db.get("v") or 0), "quote_stale_s": round(stale_s),
            "prev_close": prev}

# ---------------- volatility geometry (code owns brackets) ----------------
def atr_pct(sym):
    """14-session completed daily ATR as % of last close, IEX feed (free-plan safe).
    v13.1 fixes: sort=desc so limit=15 gets the LATEST 15 completed sessions
    (default sort=asc from now-30d dropped the most recent ~9 days), adjustment=split
    so stock splits don't spike TR, end=yesterday 23:59:59Z so partial live bars
    are excluded, and bars reversed oldest->newest before computing True Ranges."""
    today_dt = datetime.datetime.now(datetime.timezone.utc)
    today = today_dt.strftime("%Y-%m-%d")
    cache = load_json(VOL_CACHE, {})
    hit = cache.get(sym)
    if hit and hit.get("day") == today and hit.get("atr") is not None:
        return float(hit["atr"])
    try:
        start = (today_dt - datetime.timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
        yest_end = (today_dt - datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
        d = api("GET", f"/v2/stocks/bars?symbols={sym}&timeframe=1Day&limit={ATR_SESSIONS + 1}"
                       f"&start={start}&end={yest_end}&sort=desc&feed=iex&adjustment=split", base=DATA)
        bars = (d.get("bars") or {}).get(sym) or []
        if len(bars) < 5:
            return None
        bars = sorted(bars, key=lambda b: b.get("t") or "")
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i - 1]["c"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        last_c = float(bars[-1]["c"])
        if last_c <= 0:
            return None
        atr = sum(trs[-ATR_SESSIONS:]) / min(len(trs), ATR_SESSIONS) / last_c * 100
        cache[sym] = {"day": today, "atr": round(atr, 4),
                      "last_bar": (bars[-1].get("t") or "")[:10]}
        save_json(VOL_CACHE, cache)
        return atr
    except Exception as e:
        logrec({"event": "stocks_atr_err", "inst": sym, "err": str(e)[:140]})
        return None

def vol_brackets(sym):
    """Code-computed TP/SL from measured volatility."""
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

def make_bracket(tp, sl):
    tp = max(TP_MIN, min(TP_MAX, tp))
    sl = max(SL_MIN, min(SL_MAX, sl))
    if tp < TP_SL_RATIO * sl:
        tp = round(TP_SL_RATIO * sl, 2)
    return {
        "cost_pct": 0.01,
        "tp_pct": round(tp, 2),
        "sl_pct": round(sl, 2),
        "trail_arm_pct": round(tp * TRAIL_ARM_FRAC, 2),
        "trail_giveback_pct": round(sl * TRAIL_GIVEBACK_SL_FRAC, 2),
        "trail_floor_pct": TRAIL_FLOOR_PCT,
    }

# ---------------- halogen decision ----------------
SYS_PROMPT = (
    "You are the entry catalyst classifier for a small LIVE INTRADAY US-equity news book "
    "(~$120, long-only, fractional shares, commission-free, FLAT BEFORE MARKET CLOSE). "
    "You receive ONE news headline about ONE target stock (`symbol`) plus its price context. "
    "Decide whether this news is a FRESH, SPECIFIC, BULLISH catalyst for THAT target stock "
    "likely to drive further intraday LONG upside TODAY — not an already-priced-in move. "
    "TRADABLE catalysts: earnings/guidance beats, FDA/clinical approvals, confirmed M&A or "
    "major signed contracts, concrete product/revenue announcements, substantive analyst "
    "upgrades or price-target raises WITH a clear thesis, or direct positive company read-throughs "
    "where the move is NOT yet exhausted. "
    "REJECT (output action=\"no_trade\"): listicles, multi-stock roundups/recaps, vague or "
    "unconfirmed rumors/speculation, headlines primarily about a DIFFERENT company, macro/industry "
    "market-research reports, stale reprints (>2h old whose move already happened), negative "
    "catalysts (long-only — downgrades/PT cuts/breaches/export curbs are no_trade), and stocks "
    "whose reaction is already mostly priced in. "
    "A stock DOWN 4%+ today is falling for a reason: only a rare HIGH-impact bullish override "
    "is tradable there. "
    "CONSISTENCY RULE (STRICT): If your rationale says the headline is vague, conditional, "
    "speculative, indirect, a roundup, stale, or already priced in / extended, you MUST output "
    "action=\"no_trade\". Never argue against a setup in your rationale and still output buy. "
    "Take-profit and stop levels are computed by code — do NOT output target_pct or stop_pct. "
    "Score confidence honestly from 0.00 to 1.00 as your estimated probability the catalyst is "
    "genuinely actionable (avoid repeating the same habitual number on every call). "
    "Reply STRICT JSON only: {\"action\":\"buy\"|\"no_trade\",\"confidence\":0.0-1.0,"
    "\"impact\":\"high\"|\"medium\"|\"low\",\"horizon_hours\":number,"
    "\"rationale\":\"<=200 chars\"}. "
    "No prose outside the JSON. Never use double-quote characters inside rationale."
)

def parse_decision(txt):
    """Tolerant JSON extraction: direct parse -> outermost balanced-brace block
    -> field-level regex. Truly garbage output still raises -> fail closed."""
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

def halogen_decide(item, ctx, equity_usd, budget, sym=None):
    target_sym = sym or (item.get("symbols") or [""])[0]
    try:
        age_min = round((time.time() - parse_iso(item["created_at"]).timestamp()) / 60, 1)
    except Exception:
        age_min = None
    user = json.dumps({
        "symbol": target_sym, "headline": item["headline"],
        "summary": item["summary"], "source": item["source"],
        "news_age_min": age_min,
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
    """Submit entry market order. Returns spent notional (>0, truthy) on success,
    or 0.0 (falsy) on skip/unfilled so cycle() can decrement live cash."""
    if budget is None:
        budget = min(eq * MAX_NOTIONAL_FRAC / MAX_OPEN, MAX_NOTIONAL_USD)
    # Use last trade price (NBBO-proximate) rather than noisy IEX BBO ask
    ref_px = ctx["px"] or ctx["ask"]
    if ref_px <= 0:
        logrec({"event": "perp_gate_reject", "inst": sym, "why": "no reference price"})
        return 0.0
    qty = round(budget / ref_px, 4)
    notional = qty * ref_px
    if notional < 1.0:
        logrec({"event": "perp_gate_reject", "inst": sym, "why": f"notional {notional:.2f} < $1 min"})
        return 0.0
    bk = make_bracket(ans["target_pct"], ans["stop_pct"])
    if DRY:
        logrec({"event": "perp_dry_open", "inst": sym, "side": "long", "contracts": qty,
                "px": ref_px, "notional_usd": round(notional, 2), "bracket": bk,
                "headline": item["headline"], "conf": ans["conf"]})
        return round(notional, 2)
    od, err = submit_order(sym, qty, "buy")
    if od is None:
        logrec({"event": "perp_entry_unfilled", "inst": sym, "side": "long", "err": str(err)})
        return 0.0
    fill_px = float(od.get("filled_avg_price") or ref_px)
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
    return round(notional, 2)

def close_one(sym, m, reason):
    """Submit exit market order. v13.1: if the sell fails/rejects (od is None),
    keep meta[sym] intact and log perp_close_err so the next cycle retries with
    the real bracket instead of adopting a fake orphan."""
    if DRY:
        logrec({"event": "perp_dry_close", "inst": sym, "reason": reason})
        return True
    try:
        od, err = submit_order(sym, m["contracts"], "sell")
    except Exception as e:
        logrec({"event": "perp_close_err", "inst": sym, "reason": reason, "err": str(e)[:200]})
        return False
    if od is None:
        logrec({"event": "perp_close_err", "inst": sym, "reason": reason, "err": str(err)})
        return False
    exit_px = float(od.get("filled_avg_price") or 0) or None
    filled_qty = float(od.get("filled_qty") or m["contracts"])
    fee = round(filled_qty * (exit_px or 0) * SELL_REG_FEE_RATE, 4)
    entry = float(m["entry_ref"])
    if exit_px is None:
        try:
            snap = snapshots([sym])
            exit_px = price_context(sym, snap)["px"] or entry
        except Exception:
            exit_px = entry
    upc = (exit_px - entry) / entry * 100 if entry else 0.0
    opened = parse_iso(m["opened_ts"])
    age_min = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 60
    logrec({"event": "perp_close", "inst": sym, "side": "long", "reason": reason,
            "upc": round(upc, 2), "age_min": round(age_min, 1), "fill_px": exit_px,
            "realized_fee": fee, "notional_usd": m.get("notional_usd"),
            "ordId": od.get("id"), "err": err})
    meta = load_meta()
    # If only partial fill, keep remaining qty; otherwise remove closed symbol
    rem_qty = round(float(m["contracts"]) - filled_qty, 4)
    if err == "partial" and rem_qty > 0.0001:
        meta[sym]["contracts"] = rem_qty
        meta[sym]["notional_usd"] = round(rem_qty * entry, 2)
    else:
        meta.pop(sym, None)
    save_meta(meta)
    nst = load_json(NEWS_STATE, {})
    is_sl = str(reason).startswith("SL")
    nst.setdefault("exits", {})[sym] = {"ts": time.time(), "sl": is_sl}
    save_json(NEWS_STATE, nst)
    return True

def reconcile(meta, live_px=None):
    """Adopt exchange positions without meta; detect vanished ones. Optionally
    populates live_px[sym] from Alpaca's broker-authoritative current_price."""
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
            if live_px is not None:
                cp = float(p.get("current_price") or 0)
                if cp > 0:
                    live_px[sym] = cp
    for sym, p in held.items():
        if sym not in meta:
            entry = float(p.get("avg_entry_price") or 0)
            qty = float(p.get("qty") or 0)
            tp, sl, atr = vol_brackets(sym)
            meta[sym] = {"opened_ts": now_iso(), "side": "long", "contracts": qty,
                         "entry_ref": entry, "notional_usd": round(qty * entry, 2),
                         "strategy": "news", "bracket": make_bracket(tp, sl),
                         "hwm_pct": 0.0, "algo_ok": False, "adopted_orphan": True,
                         "max_hold_h": MAX_HOLD_H, "atr_pct": atr}
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

def manage(eq, clk=None):
    live_px = {}
    meta = reconcile(load_meta(), live_px=live_px)
    if not meta:
        return meta
    if clk is None:
        try:
            clk = clock_info()
        except Exception:
            clk = {"is_open": False, "time_exit_allowed": False}
    is_open = bool(clk.get("is_open"))
    time_exit_allowed = bool(clk.get("time_exit_allowed", is_open))
    missing_px = [s for s in meta.keys() if s not in live_px]
    snaps = {}
    if missing_px:
        try:
            snaps = snapshots(missing_px)
        except Exception as e:
            logrec({"event": "stocks_snap_err", "err": str(e)[:140]})
    meta_dirty = False
    for sym, m in list(meta.items()):
        px = live_px.get(sym) or price_context(sym, snaps)["px"]
        entry = float(m["entry_ref"])
        if not px or not entry:
            continue
        upc = (px - entry) / entry * 100
        old_hwm = float(m.get("hwm_pct") or 0.0)
        hwm = max(old_hwm, round(upc, 3))
        if hwm > old_hwm:
            m["hwm_pct"] = hwm
            meta[sym]["hwm_pct"] = hwm
            meta_dirty = True
        bk = m.get("bracket") or {}
        tp = float(bk.get("tp_pct") or 2.5)
        sl = float(bk.get("sl_pct") or 1.5)
        trail_arm = float(bk.get("trail_arm_pct") or 999.0)
        trail_gb = float(bk.get("trail_giveback_pct") or 999.0)
        trail_fl = float(bk.get("trail_floor_pct") or TRAIL_FLOOR_PCT)
        opened = parse_iso(m["opened_ts"])
        age_h = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 3600
        reason = None
        if upc >= tp:
            reason = f"TP {upc:.2f}% >= {tp}%"
        elif trail_arm < 900 and hwm >= trail_arm and upc <= max(trail_fl, hwm - trail_gb):
            stop_lvl = max(trail_fl, hwm - trail_gb)
            reason = f"TRAIL {upc:.2f}% <= {stop_lvl:.2f}% (hwm {hwm:.2f}%)"
        elif upc <= -sl:
            reason = f"SL {upc:.2f}% <= -{sl}%"
        elif age_h >= float(m.get("max_hold_h") or MAX_HOLD_H):
            if time_exit_allowed:
                reason = f"time exit {age_h:.1f}h"
            elif is_open:
                logrec({"event": "stocks_exit_deferred", "inst": sym, "upc": round(upc, 2),
                        "reason": f"time exit {age_h:.1f}h",
                        "why": f"waiting for open settle ({OPEN_SETTLE_MIN}m after 09:30 ET)"})
        if reason:
            if is_open:
                logrec({"event": "stocks_exit_signal", "inst": sym, "upc": round(upc, 2),
                        "reason": reason})
                if close_one(sym, m, reason):
                    meta_dirty = False  # close_one reloaded & saved meta
            else:
                logrec({"event": "stocks_exit_deferred", "inst": sym, "upc": round(upc, 2),
                        "reason": reason, "why": "market closed — executes at next open"})
    if meta_dirty:
        # Persist updated HWM for still-open positions
        cur = load_meta()
        for sym, m in meta.items():
            if sym in cur and m.get("hwm_pct", 0) > cur[sym].get("hwm_pct", 0):
                cur[sym]["hwm_pct"] = m["hwm_pct"]
        save_meta(cur)
    return load_meta()

# ---------------- scan + cycle ----------------
def scan(eq, force=False, window_min=NEWS_WINDOW_MIN, is_open=True):
    """News scan + universe day-mover table (mom_scan shape for the deck)."""
    st = load_news_state()
    start = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=window_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        news = fetch_news(start)
    except Exception as e:
        logrec({"event": "news_fetch_err", "err": str(e)[:160]})
        news = []
    cutoff = parse_iso(start)
    rss = []
    if is_open or force:
        try:
            rss = rss_cached(cutoff)
        except Exception as e:
            logrec({"event": "rss_fetch_err", "err": str(e)[:160]})
            rss = []
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
    held = set(load_meta().keys())
    fresh_count = 0
    for n in news:
        # Filter noisy multi-stock roundup spam before LLM evaluation
        if int(n.get("n_raw_symbols") or len(n.get("symbols") or [])) > MAX_HEADLINE_SYMS:
            continue
        if n["id"] in st["seen"]:
            continue
        item_had_fresh = False
        for sym in n["symbols"]:
            seen_key = f"{n['id']}:{sym}"
            if seen_key in st["seen"]:
                continue
            item_had_fresh = True
            if sym in held:
                continue
            ex_rec = exits.get(sym)
            if ex_rec:
                cd_min = SL_COOLDOWN_MIN if _exit_is_sl(ex_rec) else COOLDOWN_MIN
                if now_ts - _exit_ts(ex_rec) < cd_min * 60:
                    continue
            if any(c["sym"] == sym for c in cands):
                continue
            ctx = price_context(sym, snaps)
            if ctx["px"] <= 0:
                continue
            cands.append({"item": n, "sym": sym, "ctx": ctx})
        if item_had_fresh:
            fresh_count += 1

    # Prioritize: fewer tagged symbols first, wire (with summary) before bare RSS, freshest first
    def _cand_key(c):
        it = c["item"]
        try:
            cts = parse_iso(it.get("created_at") or "").timestamp()
        except Exception:
            cts = 0.0
        n_syms = int(it.get("n_raw_symbols") or len(it.get("symbols") or [1]))
        is_rss = 1 if it.get("source") == "yahoo" else 0
        return (n_syms, is_rss, -cts)
    cands.sort(key=_cand_key)

    logrec({"event": "mom_scan", "n_universe": len(UNIVERSE), "n_ranked": len(cands),
            "signal": "news", "thr_pct": None, "n_fresh_headlines": fresh_count,
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

    try:
        clk = clock_info()
    except Exception as e:
        logrec({"event": "clock_err", "err": str(e)[:120]})
        clk = {"is_open": False, "should_flat": False, "entries_allowed": False,
               "time_exit_allowed": False, "now_et": datetime.datetime.now(NY_TZ),
               "mins_to_close": None, "mins_since_open": 0.0, "next_close": None}

    is_open = clk["is_open"]
    meta = manage(eq, clk=clk)
    out = {"opened": [], "skipped": [], "equity": round(eq, 2)}

    # Dynamic flat-by-close using clock.next_close (handles half-days like Black Friday 13:00 ET)
    if clk["should_flat"] and meta:
        n_closed = 0
        mtc = clk.get("mins_to_close")
        flat_label = (f"flat-by-close (<={FLAT_BEFORE_CLOSE_MIN}m to close, {mtc:.0f}m left)"
                      if mtc is not None else "flat-by-close (15:50 ET rule)")
        for sym, m in list(meta.items()):
            if close_one(sym, m, flat_label):
                n_closed += 1
        meta = load_meta()
        logrec({"event": "flat_by_close", "n_closed": n_closed,
                "et": clk["now_et"].strftime("%H:%M"), "next_close": clk.get("next_close")})

    entries_allowed = clk["entries_allowed"]
    # Widen to 18h ONCE when the session actually opens for entries (09:45 ET),
    # not at 09:31 ET while opening settle still suppresses entries.
    prev_entries_open = load_news_state().get("last_open_state", False)
    window = 1080 if (entries_allowed and not prev_entries_open) else NEWS_WINDOW_MIN
    try:
        st, cands, snaps = scan(eq, window_min=window, is_open=is_open)
    except Exception as e:
        logrec({"event": "news_scan_err", "err": str(e)[:200]})
        st, cands = load_news_state(), []

    if not is_open:
        out["skipped"].append("market closed — entries at next open")
        logrec({"event": "perp_no_trade", "why": "market closed"})
    elif not entries_allowed:
        if clk["mins_since_open"] < OPEN_SETTLE_MIN:
            why = f"opening settle wait ({clk['mins_since_open']:.0f}m < {OPEN_SETTLE_MIN}m)"
        else:
            why = f"entry cutoff passed ({clk.get('mins_to_close')}m to close)"
        out["skipped"].append(why)
        logrec({"event": "perp_no_trade", "why": why})
    elif len(meta) >= MAX_OPEN:
        out["skipped"].append(f"MAX_OPEN {MAX_OPEN} reached")
        logrec({"event": "perp_no_trade", "why": f"MAX_OPEN {MAX_OPEN} reached"})
    else:
        if DRY:
            cash = eq
        else:
            try:
                cash = float(api("GET", "/v2/account").get("cash") or 0)
            except Exception:
                cash = eq
        calls = 0
        for c in cands:
            if calls >= MAX_LLM_CALLS or len(load_meta()) >= MAX_OPEN:
                break
            budget = min(eq * MAX_NOTIONAL_FRAC / MAX_OPEN, cash * MAX_NOTIONAL_FRAC, MAX_NOTIONAL_USD)
            if budget < 1.0:
                out["skipped"].append(f"cash ${cash:.2f} — no $1-min leg possible")
                logrec({"event": "perp_no_trade", "why": f"insufficient cash {cash:.2f}"})
                break
            sym, item, ctx = c["sym"], c["item"], c["ctx"]
            seen_key = f"{item['id']}:{sym}"
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
                st["seen"][seen_key] = time.time()
                save_json(NEWS_STATE, st)
                logrec({"event": "perp_gate_reject", "inst": sym, "why": "; ".join(fails),
                        "headline": item["headline"]})
                continue
            calls += 1
            try:
                ans = halogen_decide(item, ctx, eq, budget, sym=sym)
            except Exception as e:
                logrec({"event": "news_llm_err", "inst": sym, "err": str(e)[:200],
                        "headline": item["headline"]})
                logrec({"event": "perp_no_trade", "inst": sym, "why": "LLM fail-closed"})
                st["seen"][seen_key] = time.time()
                save_json(NEWS_STATE, st)
                continue
            if ans["action"] == "buy":
                tp, sl, atr = vol_brackets(sym)
                ans["target_pct"], ans["stop_pct"], ans["atr_pct"] = tp, sl, atr
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
            st["seen"][seen_key] = time.time()
            save_json(NEWS_STATE, st)
            if ans["action"] != "buy":
                logrec({"event": "perp_no_trade", "inst": sym, "why": ans["rationale"][:160]})
                continue
            gate_fails = []
            if ans["conf"] < CONF_FLOOR:
                gate_fails.append(f"conf {ans['conf']:.2f} < {CONF_FLOOR}")
            if ans["impact"] == "low":
                gate_fails.append("impact low")
            if ctx["day_chg_pct"] <= DEEP_DIP_PCT and (
                    ans["impact"] != "high" or ans["conf"] < DEEP_DIP_CONF):
                gate_fails.append(f"deep dip {ctx['day_chg_pct']}% needs impact=high + conf>={DEEP_DIP_CONF}")
            if gate_fails:
                logrec({"event": "perp_gate_reject", "inst": sym, "why": "; ".join(gate_fails)})
                continue
            try:
                spent = open_one(sym, item, ctx, ans, eq, budget)
                if spent:
                    cash = max(0.0, cash - float(spent))
                    out["opened"].append(("long", sym))
            except Exception as e:
                logrec({"event": "perp_entry_err", "inst": sym, "err": str(e)[:200],
                        "headline": item["headline"]})

    # Track whether today's 18h overnight sweep has fired (resets when market closes)
    if not is_open:
        st["last_open_state"] = False
    elif entries_allowed:
        st["last_open_state"] = True
    save_json(NEWS_STATE, st)

    # heartbeat (deck liveness strip)
    nst = load_json(NEWS_STATE, {})
    last_hb = nst.get("last_heartbeat_ts")
    hb_age = 1e9 if not last_hb else (
        datetime.datetime.now(datetime.timezone.utc) - parse_iso(last_hb)).total_seconds()
    if hb_age >= HEARTBEAT_S:
        logrec({"event": "mom_heartbeat", "why": "news book alive",
                "n_positions": len(load_meta()), "market_open": is_open})
        nst["last_heartbeat_ts"] = now_iso()
        save_json(NEWS_STATE, nst)
    try:
        status(market_open=is_open)
    except Exception as e:
        logrec({"event": "stocks_status_err", "err": str(e)[:160]})
    return out

# ---------------- status ----------------
def _tail_last_jev(path, max_bytes=131072):
    """Read last perp_jev from the tail of perp_log.jsonl in O(1) I/O."""
    last_jev = None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", "replace")
        for line in reversed(chunk.splitlines()):
            if '"perp_jev"' in line:
                try:
                    e = json.loads(line)
                    if e.get("event") == "perp_jev":
                        return e
                except Exception:
                    continue
    except Exception:
        pass
    return last_jev

def status(market_open=None):
    meta = load_meta()
    eq = equity()
    if market_open is None:
        try:
            market_open = bool(clock().get("is_open"))
        except Exception:
            market_open = None
    out = {"engine": ENGINE_VERSION, "usdc_equity": round(eq, 2),
           "eur_float": 0.0, "account_total_usd": round(eq, 2),
           "collateral_ccy": "USD", "halted": halted(), "dry": DRY,
           "market_open": market_open,
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
    out["last_jev"] = _tail_last_jev(LOG)
    out["breaked"] = os.path.exists(STOP) and open(STOP).read().startswith("breaker")
    snap = {"engine": out["engine"], "usdc_equity": out["usdc_equity"],
            "eur_float": 0.0, "account_total_usd": out["account_total_usd"],
            "collateral_ccy": "USD", "halted": out["halted"], "dry": DRY,
            "market_open": out["market_open"],
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
