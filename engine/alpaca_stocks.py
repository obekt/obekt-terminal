#!/usr/bin/env python3
"""alpaca_stocks.py — v11.0-news: NEWS-DRIVEN US EQUITY engine on Alpaca.

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

HONEST EV: no demonstrated edge yet. The OKX fade edge was venue-specific
and does NOT transfer. This is a fresh hypothesis (fresh high-impact news +
contained price context -> short-horizon drift), pre-registered kill-gate:
30 closed trades OR 14 days, whichever FIRST, live realized net <= 0 ->
replace (operator standing rule: failed algorithms get replaced, not
stopped). Mechanics review day 3.

Modes: cycle | status | scan   (+ --dry-run: full decision path, zero orders)
"""
import json, os, sys, time, datetime, urllib.request, urllib.error, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_VERSION = "v11.0-news"
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
            "SHOP", "MRVL", "SMCI", "ARM", "TSM", "SQ"]
MAX_OPEN = 2                 # slots; ~equity*0.98/2 per leg (all-in directive)
MAX_NOTIONAL_FRAC = 0.98
MAX_NOTIONAL_USD = 500.0     # bug guard only
CONF_FLOOR = 0.60            # burn-in bar; recalibrate after ~30 calls
NEWS_WINDOW_MIN = 90         # headline freshness window
MAX_LLM_CALLS = 4            # per cycle cap (halogen is $0 but keep it sane)
MAX_SPREAD_PCT = 10.0        # disaster guard only — IEX BBO spread is NOT the
                             # NBBO (routinely 2-4% on liquid names); Alpaca
                             # routes to the inside market, so this never binds
ASK_VS_LAST_PCT = 2.5        # ghost-quote guard: IEX ask >2.5% off the last
                             # trade means a broken/stale quote, not a market
QUOTE_STALE_S = 90
CHASE_CAP_PCT = 7.0          # don't buy a name already +7% on the day
KNIFE_FLOOR_PCT = -9.0       # don't catch a -9% day even on "good" news
TP_MIN, TP_MAX = 1.0, 5.0
SL_MIN, SL_MAX = 0.8, 3.0
TP_SL_RATIO = 1.5            # enforce TP >= 1.5 x SL
MAX_HOLD_H = 48.0            # news drift decays; time exit
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

# ---------------- halogen decision ----------------
SYS_PROMPT = (
    "You are the entry decision model for a small LIVE US-equity news book "
    "(~$120, long-only, fractional shares, commission-free, regular hours only). "
    "You receive ONE fresh news headline about a stock plus its price context. "
    "Decide whether the news is a tradable catalyst for a 1-48h LONG drift. "
    "Rules: routine/irrelevant news (software updates, awards, listicles, rehashed "
    "analyst notes, macro without company impact) -> no_trade. Strong genuine "
    "catalysts (earnings beats/misses with guidance, FDA decisions, major contracts, "
    "M&A, guidance raises, product launches with revenue impact, regulatory wins) "
    "may justify buy. Beware: good news on a stock already up big today is often "
    "priced in (chase risk); bad news is NOT tradable here (long-only). "
    "target_pct/stop_pct are 1-48h price-move expectations, not certainties. "
    "Reply STRICT JSON only: {\"action\":\"buy\"|\"no_trade\",\"confidence\":0.0-1.0,"
    "\"impact\":\"high\"|\"medium\"|\"low\",\"target_pct\":number,\"stop_pct\":number,"
    "\"horizon_hours\":number,\"rationale\":\"<=200 chars\"}. "
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
    # schema check — fail closed on anything malformed. Brackets are only
    # required for buy: a no_trade answer legitimately carries 0/none.
    assert ans.get("action") in ("buy", "no_trade"), f"bad action: {str(ans)[:120]}"
    conf = float(ans.get("confidence"))
    assert 0.0 <= conf <= 1.0, f"bad confidence: {str(ans)[:120]}"
    assert ans.get("impact") in ("high", "medium", "low"), f"bad impact: {str(ans)[:120]}"
    if ans["action"] == "buy":
        tp = float(ans.get("target_pct")); sl = float(ans.get("stop_pct"))
        assert 0 < tp < 50 and 0 < sl < 50, f"bad bracket: {str(ans)[:120]}"
    else:
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

def open_one(sym, item, ctx, ans, eq):
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
                 "entry_ordId": od.get("id"), "max_hold_h": MAX_HOLD_H}
    save_meta(meta)
    logrec({"event": "perp_open", "inst": sym, "side": "long", "contracts": filled_qty,
            "notional_usd": round(notional, 2), "fill_px": fill_px, "conf": ans["conf"],
            "margin": None, "noul": None, "algo_ok": False, "realized_fee": 0.0,
            "entry_type": "market", "headline": item["headline"], "bracket": bk})
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
                         "tp_pct": 2.5, "sl_pct": 1.5, "trail_arm_pct": 999.0,
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
    fresh = [n for n in news if n["id"] not in st["seen"]]
    snaps = snapshots(UNIVERSE)
    movers = []
    for sym in UNIVERSE:
        ctx = price_context(sym, snaps)
        if ctx["px"]:
            movers.append((sym, ctx["day_chg_pct"]))
    movers.sort(key=lambda r: -r[1])
    cands = []
    for n in fresh:
        for sym in n["symbols"]:
            if sym in st["acted"]:
                continue
            if any(c["item"]["symbols"][0] == sym for c in cands):
                continue
            cands.append({"item": n, "sym": sym, "ctx": price_context(sym, snaps)})
    logrec({"event": "mom_scan", "n_universe": len(UNIVERSE), "n_ranked": len(cands),
            "signal": "news", "thr_pct": None, "n_fresh_headlines": len(fresh),
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
    elif len(meta) >= MAX_OPEN:
        out["skipped"].append(f"MAX_OPEN {MAX_OPEN} reached")
        logrec({"event": "perp_no_trade", "why": f"MAX_OPEN {MAX_OPEN} reached"})
    else:
        budget = min(eq * MAX_NOTIONAL_FRAC / MAX_OPEN, MAX_NOTIONAL_USD)
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
            if gate_fails:
                logrec({"event": "perp_gate_reject", "inst": sym, "why": "; ".join(gate_fails)})
                continue
            if open_one(sym, item, ctx, ans, eq):
                st["acted"][sym] = today
                out["opened"].append(("long", sym))
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
