#!/usr/bin/env python3
"""OKX EEA spot crypto scalping pilot — Jev (System One) decides direction, code acts.

v3.1 (09-30): dynamic universe + cost-aware brackets + social sentiment feed.
  - v3 traded 0 times in 24h: its hardcoded 4-pair list + 0.12% spread gate
    excluded every pair that actually moves on EUR books (scan of all 273 EUR
    pairs: movers carry 0.16-0.50% spreads; majors are tight but flat).
  - select_universe(): one /market/tickers scan (10-min cache) -> filter by
    liquidity (>=€200k), spread (<=0.20%), 24h range (>=2%) -> rank by range ->
    top 10 -> quality gates -> top 5 to Jev (MAX_JEV_PAIRS bounds prompt size).
    Written to universe.json so social_feed.py tracks the same set.
  - bracket_for(spread): TP = 4x all-in cost (spread+maker+taker fees), clamped
    [1.5, 3.0]; SL/trail-arm/giveback/floor + the vol30 reachability gate + the
    Jev noul threshold all scale off TP. Stored per-position in meta at entry.
  - social_feed.py (keyless): CoinGecko per-coin sentiment votes + trending rank
    + alternative.me Fear&Greed (incl. streak) -> social.json, 15-min cache,
    injected per pair. Replaces OKX orbit news/sentiment (NOT available to EEA
    accounts — verified 09-30 against the official @okx_ai/okx-trade-mcp paths).
  - Prompt budget: MAX_STATE_CHARS 6200 (~3230 tok at measured 1.92 chars/tok);
    trim_state() cuts news->macro->secondary pair fields in priority order;
    every jev event logs state_chars/est_tokens/input_tokens + trimmed cuts,
    and prompt_budget_warn fires above MAX_JEV_INPUT_TOKENS.
  - v3 kept: trailing exits, momentum gates before Jev, CONF 0.50/NOUL 0.40,
    maker->IOC chase entries, 30m loss cooldown, -2.5%/day breaker, MAX_OPEN 2.

v3 (09-29): rebuilt after 24h live post-mortem (29 opens, ~3W/13L ledger-verified,
-€5.3 realized+fees; fee wall ate the +0.8% TP at ~20% win rate, 45% of maker
entries unfilled = adverse selection, time-exits bled fees, instant re-entries
after stops). Changes vs v2:
  - bracket TP +1.5% / SL -0.7% (breakeven win rate 48% vs 58% at 0.8/0.5)
  - TRAILING exit in manage(): arm at +0.6% HWM, exit on 0.35% giveback or
    profit < +0.1% floor — let winners run, clip round-trips on reversal
  - entry quality gates IN CODE before Jev: volatility_30min >= 0.9%,
    chg_30min > 0, bars_up >= 3/6, range30_pos 30-92 (pullback in uptrend),
    taker_buy_share >= 0.50; CONF_BAR 0.50 / NOUL_BAR 0.40 (was 0.35/0.30)
  - fill fix: post_only 25s -> if unfilled and ask within CHASE_MAX_PCT,
    IOC-take (partial fills now tracked instead of orphaned)
  - per-pair LOSS_COOLDOWN_MIN=30 after any losing exit (no re-entry churn)
  - daily loss breaker: realized day PnL <= -2.5% of start equity -> STOP file
  - universe narrowed to fee-wall clearers: NEAR, ZEC, SUI, LINK
  - close_detected matches fills AFTER opened_ts (fixes stale-fill misreports)
  - keeps: stake = ALL free EUR / slots, stables sweep, server OCO first line

Modes:
  manage   : enforce stops/targets/time-stops (deterministic; OCO is first line)
  decide   : one entry attempt: state -> Jev -> gates -> maker order
  cycle    : news refresh + sweep + manage + decide-loop (what cron runs)
  status   : account/positions/algos summary

Risk rules live in CODE, not Jev. Kill switch: touch STOP -> entries halt, manage runs.
"""
import json, os, sys, time, base64, hmac, hashlib, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "okx_jev_log.jsonl")
STOP = os.path.join(HERE, "STOP")
META = os.path.join(HERE, "positions.json")

# ---------------------------------------------------------------- credentials
# Two credential sets are needed to actually TRADE:
#   OK  — the exchange key (OKX).  Fields: api_key, secret, passphrase, base
#   TS  — the decision model key (TypeSafe Jev).  Field: api_key
# Each is read from a JSON file (~/.config/...) OR from environment variables,
# so nothing secret is ever hardcoded. If neither source exists, we exit with a
# clear message instead of crashing on a missing file.
#
# Env override (takes precedence over the file):
#   OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE, OKX_BASE (default https://eea.okx.com)
#   TYPESAFE_API_KEY
def load_creds(path, env_keys=None, required=("api_key",)):
    # 1) env vars, if the caller supplied a mapping of field->env name
    if env_keys:
        from_env = {}
        for field, envname in env_keys.items():
            v = os.environ.get(envname)
            if v:
                from_env[field] = v
        if all(f in from_env for f in required):
            return from_env
    # 2) JSON file
    full = os.path.expanduser(path)
    if os.path.exists(full):
        with open(full) as f:
            return json.load(f)
    # 3) nothing usable — fail loudly with setup guidance
    sys.stderr.write(
        "\n[obekt-terminal] missing credentials.\n"
        "  expected file: %s\n"
        "  or set env:    %s\n"
        "  see README.md -> 'Run it' for the exact JSON shape. This engine will\n"
        "  not trade real money until you provide your OWN keys.\n\n"
        % (full, ", ".join((env_keys or {}).values()) or "(none)"))
    raise SystemExit(2)

OK = load_creds("~/.config/okx/credentials.json",       # api_key, secret, passphrase, base
                env_keys={"api_key": "OKX_API_KEY", "secret": "OKX_SECRET",
                          "passphrase": "OKX_PASSPHRASE", "base": "OKX_BASE"},
                required=("api_key", "secret", "passphrase"))
OK.setdefault("base", "https://eea.okx.com")
TS = load_creds("~/.config/typesafe/credentials.json",
                env_keys={"api_key": "TYPESAFE_API_KEY"})

# ---------------- UNIVERSE (dynamic since v3.1, 09-30) ----------------
# v3 used a HARDCODED 4-pair list from a one-off 09-28 volatility study. A live
# scan of all 273 EUR-quoted spot pairs (09-30) showed why that was wrong: only
# 22 clear the liquidity floor, and 4 of the top 6 daily movers (AAVE +8%,
# AVAX +7%, PUMP +15%, NIGHT +15%) were NOT in the list at all. Worse, on EUR
# books tight spread and high volatility are mutually exclusive — BTC-EUR spread
# is 0.0001% but ranges 2%/day, while AAVE/AVAX/PUMP range 15-24%/day at 0.16-0.50%
# spread, which v3's 0.12% spread gate rejected on principle. Result: v3 could
# not trade (0 entries in 24h). v3.1 selects the universe every cycle and prices
# the bracket off the actual cost of the pair instead of a fixed TP.
UNIVERSE_FILE = os.path.join(HERE, "universe.json")
UNIVERSE_CACHE_MIN = 10     # one /market/tickers scan (all pairs) per 10 min
UNIVERSE_SCAN = 10          # liquid+moving candidates handed to the gates
MAX_JEV_PAIRS = 5           # cap on pairs sent to Jev (bounds prompt size)
MIN_DAY_RANGE_PCT = 2.0     # 24h (high-low)/open floor: must actually move
MIN_VOL24H_EUR = 200_000    # liquidity floor for the EUR book (quote volume)
MIN_TOP5_DEPTH_EUR = 1_500  # top-5 bid levels must hold >= this (CT-EUR lesson
                            # 09-30: ticker spread looked fine but the book held
                            # €662 top-5 — our €70 IOC ate it and we entered 1%
                            # off, then stopped at -4.4%). vol24h does NOT imply
                            # resting depth on thin EUR listings.
ENTRY_DEPTH_MULT = 3.0      # at entry time: top-5 bid depth >= 3x stake
FALLBACK_PAIRS = ["NEAR-EUR", "ZEC-EUR", "SUI-EUR", "LINK-EUR"]  # scan failure only

# ---------------- COST-AWARE BRACKET (v3.1) ----------------
# The question is not "is the spread tight?" but "is there edge left after cost?".
# all_in_cost = spread + MAKER_FEE_PCT + TAKER_FEE_PCT (entry maker, exit taker).
# TARGET is then EDGE_MULT x that cost, and the whole bracket (stop, trail arm,
# giveback, floor, vol gate, Jev's noul question) scales off TARGET — so a
# wide-spread mover must promise a bigger move to be worth taking.
MAKER_FEE_PCT = 0.10
TAKER_FEE_PCT = 0.20
EDGE_MULT = 7.0             # TP >= 7x all-in round-trip cost (replay 10-05: 4x->7x cut SL exits 16->7, trail 14->23, best net)
MIN_TARGET_PCT = 1.5        # never below the v3 fixed TP
MAX_TARGET_PCT = 3.0        # never above what a scalp can reach in 90min
SL_RATIO = 0.47             # SL = 0.47 x TP (v3's 0.7/1.5 ratio, preserved)
TRAIL_ARM_RATIO = 0.40      # arm trailing at 0.40 x TP
TRAIL_GIVEBACK_RATIO = 0.23 # exit after giving back 0.23 x TP from HWM
TRAIL_FLOOR_RATIO = 0.07    # trailing exit never below 0.07 x TP
MIN_VOL30_MULT = 0.6        # realized 30min range must be >= 0.6 x TP (reachable)
MAX_SPREAD_PCT = 0.20       # widened from 0.12: EUR movers legitimately sit at
                            # 0.12-0.20%, and the cost-aware TP now pays for it

# ---------------- PROMPT BUDGET (v3.1, recalibrated v3.3 10-01) ----------------
# Measured on 48 logged calls 10-01: chars/token min 1.65, median 1.97 (dense
# JSON tokenizes worse than the old 1.92 estimate — 4 calls crossed the alarm
# at ~3.4-3.7k tokens with state ~6.0-6.2k chars). Budget set off the WORST
# observed ratio: 5400 chars / 1.65 = ~3273 tokens < 3400 alarm.
CHARS_PER_TOKEN = 1.65      # worst-case measured (est_tokens uses this, so it
                            # OVER-estimates -> conservative)
MAX_STATE_CHARS = 5400      # trim ceiling: keeps real input_tokens < 3400
MAX_JEV_INPUT_TOKENS = 3400 # alarm threshold on Jev's reported input_tokens

STABLES = ["USDC", "USDT"]   # swept to EUR every cycle (all-funds directive)
MAX_OPEN = 2              # concentrated: fewer, better deals (v2's 3 diluted stake)
MAX_ENTRIES_PER_CYCLE = 1 # one quality entry per cycle max
TIME_STOP_MIN = 30        # close if losing after this
STALE_EXIT_MIN = 90       # close flat-or-worse after this (capital rotation)
FILL_WAIT_S = 25          # post_only entry patience before chase/cancel
CHASE_MAX_PCT = 0.15      # IOC-take fallback only if ask within this % of bid
COOLDOWN_MIN = 30         # per-pair cooldown after a LOSING exit
WIN_COOLDOWN_MIN = 15     # per-pair cooldown after a PROFITABLE exit (v3.5,
                          # 10-03). Measured leak: Oct 2 ran 8 CT-EUR round
                          # trips — winners interleaved with re-entry stops
                          # bought right after an exit high (net +€0.20 on
                          # €2.12 fees). A TP exit marks the local top of the
                          # move; chasing back in immediately is the worst
                          # entry in the book. Shorter than the loss cooldown:
                          # a genuine new trend can re-form fast, and we still
                          # want multiple deals/day per the user's directive.
# User directive 09-30: "we do not care about losses if there is condition to
# make profit" — entry QUALITY gates decide when to trade, not a loss counter.
# The old -2.5% daily breaker halted on ordinary variance (tripped 09-30 on two
# small stops incl. one caused by a thin-book bug, not by bad conditions).
# Kept only as a DISASTER backstop against software failure (e.g. a v2-style
# re-entry churn loop): -8%/day is unreachable by legitimate gated trading.
DAILY_LOSS_PCT = 8.0
MIN_CHG30_PCT = 0.30      # momentum gate: chg_30min_pct must exceed this
MIN_BARS_UP = 3           # of last 6 5m bars closing green
MIN_TAKER_BUY = 0.50      # net aggressive buying over last 30min
# v3.3 decision bars (10-01 replay of 38 real Jev picks over 27h): the old
# conf>=0.50 bar vetoed 35/38 picks — Jev's `confidence` is a certainty metric
# that rarely exceeds ~0.45 on 45-min calls, even when the PREFERENCE is clear
# (buy_ct p=0.74 vs no_trade 0.26 got conf 0.48 -> vetoed; same pair 18min
# later conf 0.76 -> traded, then lost on thin book — the metric was noise on
# the edge). Primary gate is now the probability MARGIN p(buy)-p(no_trade):
# margin>=0.25 & noul>=0.45 would have fired 9/38 (6 after depth gates),
# vs 3/38 for conf>=0.50. conf>=0.50 kept as an OR-path for strong-certainty
# picks. NOUL_BAR 0.40 -> 0.45: margin loosening paid for by a stricter
# directional-probability floor.
MARGIN_BAR = 0.30         # p(chosen buy) - p(no_trade) from Jev probabilities (replay 10-05: 0.25->0.30 WR 53.7->57.4%)
CONF_BAR = 0.50           # OR-path: high certainty also passes
NOUL_BAR = 0.45           # noul = P(rise >= trail_arm% within 45min)
MIN_STAKE_EUR = 10.0

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def logrec(rec):
    rec["ts"] = now_iso()
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")

# ---------------- OKX v5 REST client ----------------

def _ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def _sign(t, method, path, body=""):
    msg = t + method + path + body
    return base64.b64encode(hmac.new(OK["secret"].encode(), msg.encode(), hashlib.sha256).digest()).decode()

def api(method, path, body=None, private=True):
    """path includes query string; signing covers path+query."""
    b = json.dumps(body) if body is not None else ""
    t = _ts()
    headers = {"Content-Type": "application/json", "User-Agent": "jev-pilot/1.0"}
    if private:
        headers.update({"OK-ACCESS-KEY": OK["api_key"], "OK-ACCESS-SIGN": _sign(t, method, path, b),
                        "OK-ACCESS-TIMESTAMP": t, "OK-ACCESS-PASSPHRASE": OK["passphrase"]})
    req = urllib.request.Request(OK["base"] + path, data=b.encode() if b else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        d = json.loads(e.read().decode() or "{}")
    if d.get("code") not in ("0", None):
        raise RuntimeError(f"OKX {d.get('code')}: {d.get('msg')} data={json.dumps(d.get('data'))[:300]}")
    return d.get("data", [])

# public market data
def ticker(inst):
    return api("GET", f"/api/v5/market/ticker?instId={inst}", private=False)[0]

def candles(inst, bar="5m", limit=7):
    rows = api("GET", f"/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}", private=False)
    # rows newest-first: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    out = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
           for r in rows]
    out.reverse()
    return out

def rubik_sentiment(ccy):
    """OKX native sentiment: positioning + flow stats (public, works on EEA —
    the orbit news module is global-site-only and 404s here). Returns:
      ls_ratio      : long/short ACCOUNT ratio (latest, e.g. 1.39 = 58% long)
      ls_ratio_1h_ago: same ~1h back (trend of crowd positioning)
      taker_buy_share: buy vol / (buy+sell) over recent 5m windows (>0.5 = net buying)
      funding_bps   : current BTC-anchored perp funding in bps (market-wide risk tone)
    """
    out = {}
    try:
        d = api("GET", f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m&limit=13", private=False)
        rows = sorted(d, key=lambda r: int(r[0]))   # ascending ts: [-1]=latest, [0]=~1h ago
        if rows:
            out["ls_ratio"] = float(rows[-1][1])
            if len(rows) > 12:
                out["ls_ratio_1h_ago"] = float(rows[0][1])
    except Exception:
        pass
    try:
        d = api("GET", f"/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType=CONTRACTS&period=5m&limit=6", private=False)
        rows = sorted(d, key=lambda r: int(r[0]))
        if rows:
            buy = sum(float(r[1]) for r in rows)
            sell = sum(float(r[2]) for r in rows)
            if buy + sell > 0:
                out["taker_buy_share"] = round(buy / (buy + sell), 3)
    except Exception:
        pass
    return out

def market_funding_bps():
    try:
        d = api("GET", "/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", private=False)
        return round(float(d[0]["fundingRate"]) * 10000, 2)
    except Exception:
        return None

# ---------------- v3.1 dynamic universe ----------------

def bracket_for(spread_pct):
    """Cost-aware bracket: TP = EDGE_MULT x all-in round-trip cost, clamped.
    Everything else (SL, trail arm/giveback/floor, vol reachability gate, and the
    noul question's threshold) derives from TP, so a wide-spread pair must promise
    a proportionally bigger move. Replaces v3's one-size-fits-all 1.5/0.7."""
    cost = spread_pct + MAKER_FEE_PCT + TAKER_FEE_PCT
    tp = min(max(cost * EDGE_MULT, MIN_TARGET_PCT), MAX_TARGET_PCT)
    return {"cost_pct": round(cost, 3), "tp_pct": round(tp, 3),
            "sl_pct": round(tp * SL_RATIO, 3),
            "trail_arm_pct": round(tp * TRAIL_ARM_RATIO, 3),
            "trail_giveback_pct": round(tp * TRAIL_GIVEBACK_RATIO, 3),
            "trail_floor_pct": round(tp * TRAIL_FLOOR_RATIO, 3),
            "min_vol30_pct": round(tp * MIN_VOL30_MULT, 3)}

def book_depth_eur(inst, levels=5):
    """EUR resting on the top N bid levels. Ticker spread/volume lie on thin
    EUR listings (CT-EUR 09-30) — the actual book is the only truth about
    whether our stake can enter/exit near mid. None on failure (caller decides)."""
    try:
        b = api("GET", f"/api/v5/market/books?instId={inst}&sz={levels}", private=False)[0]
        return sum(float(l[0]) * float(l[1]) for l in b.get("bids", [])[:levels])
    except Exception:
        return None

def select_universe(force=False):
    """Pick the pairs worth examining THIS cycle from every EUR-quoted spot pair.
    One keyless /market/tickers call (all 1420 instruments) -> filter by liquidity,
    real movement, and spread -> rank by 24h range -> top UNIVERSE_SCAN. Cached
    UNIVERSE_CACHE_MIN minutes because the scan is the expensive-ish step and the
    answer changes slowly; positions.json/cooldown filtering still happens live.

    Written to universe.json so social_feed.py follows the SAME set (the old
    hardcoded PAIRS list made sentiment coverage drift from what we traded)."""
    now = time.time()
    if not force and os.path.exists(UNIVERSE_FILE):
        try:
            d = json.load(open(UNIVERSE_FILE))
            if now - d.get("scanned", 0) < UNIVERSE_CACHE_MIN * 60 and d.get("pairs"):
                return d["pairs"], d.get("scanned")
        except Exception:
            pass
    try:
        rows = api("GET", "/api/v5/market/tickers?instType=SPOT", private=False)
    except Exception as e:
        logrec({"event": "universe_scan_err", "err": str(e)[:150]})
        try:
            stale = json.load(open(UNIVERSE_FILE)).get("pairs") or FALLBACK_PAIRS
        except Exception:
            stale = FALLBACK_PAIRS
        return stale, None
    cands = []
    for t in rows:
        inst = t.get("instId") or ""
        if not inst.endswith("-EUR"):
            continue
        if inst.split("-")[0] in STABLES:
            continue          # USDC/USDT-EUR are sweep rails, not trades
        try:
            last = float(t["last"]); open24 = float(t["open24h"])
            hi = float(t["high24h"]); lo = float(t["low24h"])
            bid = float(t["bidPx"] or 0); ask = float(t["askPx"] or 0)
            volq = float(t.get("volCcy24h") or 0)     # quote ccy = EUR turnover
        except Exception:
            continue
        if last <= 0 or open24 <= 0 or bid <= 0 or ask <= 0:
            continue
        spread = (ask - bid) / ask * 100
        if volq < MIN_VOL24H_EUR or spread > MAX_SPREAD_PCT:
            continue
        day_range = (hi - lo) / open24 * 100
        if day_range < MIN_DAY_RANGE_PCT:
            continue
        cands.append({"inst": inst, "day_range_pct": round(day_range, 2),
                      "chg24h_pct": round((last / open24 - 1) * 100, 2),
                      "vol24h_meur": round(volq / 1e6, 2), "spread_pct": round(spread, 4)})
    # rank by realized movement (the thing that decides whether TP is reachable),
    # not by volume — volume already gated. Ties -> tighter spread wins.
    cands.sort(key=lambda c: (-c["day_range_pct"], c["spread_pct"]))
    # DEPTH GATE (CT-EUR lesson): ticker spread/volume can look fine while the
    # resting book is too thin for our stake. Check the actual book for the top
    # candidates only (bounded API calls), then take the top UNIVERSE_SCAN.
    checked = []
    for c in cands[:UNIVERSE_SCAN * 2]:
        dep = book_depth_eur(c["inst"])
        if dep is None:
            continue          # book fetch failed -> don't trust it
        c["top5_depth_eur"] = round(dep)
        if dep < MIN_TOP5_DEPTH_EUR:
            logrec({"event": "universe_depth_reject", "inst": c["inst"], "depth_eur": round(dep)})
            continue
        c["bracket"] = bracket_for(c["spread_pct"])
        checked.append(c)
    sel = checked[:UNIVERSE_SCAN]
    cands = checked
    pairs = [c["inst"] for c in sel]
    if not pairs:
        logrec({"event": "universe_empty", "note": "no EUR pair passed liquidity/spread/movement",
                "scanned": len(rows)})
        try:
            pairs = json.load(open(UNIVERSE_FILE)).get("pairs") or FALLBACK_PAIRS
        except Exception:
            pairs = FALLBACK_PAIRS
        sel = []
    try:
        tmp = UNIVERSE_FILE + ".tmp"
        json.dump({"scanned": now, "scanned_iso": now_iso(), "pairs": pairs,
                   "n_eur_pairs": sum(1 for t in rows if (t.get("instId") or "").endswith("-EUR")),
                   "n_candidates": len(cands), "selected": sel},
                  open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, UNIVERSE_FILE)
    except Exception as e:
        logrec({"event": "universe_write_err", "err": str(e)[:150]})
    logrec({"event": "universe_scan", "n_eur": sum(1 for t in rows if (t.get("instId") or "").endswith("-EUR")),
            "n_candidates": len(cands), "selected": pairs})
    return pairs, now

_INSTR_CACHE = {}
def instrument(inst):
    if inst not in _INSTR_CACHE:
        d = api("GET", f"/api/v5/public/instruments?instType=SPOT&instId={inst}", private=False)
        _INSTR_CACHE[inst] = d[0]
    return _INSTR_CACHE[inst]

def round_step(x, step, down=True):
    """Round x to a multiple of step (string decimals from OKX lotSz/tickSz)."""
    import decimal
    s = decimal.Decimal(str(step))
    d = decimal.Decimal(str(x))
    q = (d / s).to_integral_value(rounding=decimal.ROUND_DOWN if down else decimal.ROUND_HALF_UP)
    return float(q * s)

# private
def balances():
    d = api("GET", "/api/v5/account/balance")[0]
    return {x["ccy"]: {"avail": float(x["availBal"] or 0), "cash": float(x["cashBal"] or 0),
                       "frozen": float(x["frozenBal"] or 0), "eq_usd": float(x.get("eqUsd") or 0)}
            for x in d.get("details", [])}

def eur_avail():
    return balances().get("EUR", {"avail": 0})["avail"]

def sweep_stables():
    """User directive 09-28: always trade with ALL available funds. Convert any
    USDC/USDT balance to EUR (market sell on X-EUR, tight books ~0.001-0.01%)."""
    swept = []
    bals = balances()
    for ccy in STABLES:
        avail = bals.get(ccy, {}).get("avail", 0)
        if avail <= 0:
            continue
        inst = f"{ccy}-EUR"
        try:
            ins = instrument(inst)
            sz = round_step(avail, ins["lotSz"])
            if sz < float(ins["minSz"]):
                logrec({"event": "sweep_skip_dust", "ccy": ccy, "sz": sz, "minSz": ins["minSz"]})
                continue
            o = place_market(inst, "sell", sz)
            time.sleep(1)
            od = order_detail(inst, o["ordId"])
            eur_got = float(od.get("accFillSz") or 0) * float(od.get("avgPx") or 0)
            swept.append({"ccy": ccy, "sz": sz, "eur_got": round(eur_got, 2),
                          "avgPx": od.get("avgPx"), "fee": od.get("fee")})
            logrec({"event": "sweep", "inst": inst, "sz": sz, "eur_got": round(eur_got, 2),
                    "ordId": o.get("ordId"), "fee": od.get("fee")})
        except Exception as e:
            logrec({"event": "sweep_err", "ccy": ccy, "err": str(e)[:200]})
    return swept

def order_detail(inst, ord_id):
    return api("GET", f"/api/v5/trade/order?instId={inst}&ordId={ord_id}")[0]

def pending_algos(inst=None, ordType="oco"):
    q = f"?ordType={ordType}&state=live" + (f"&instId={inst}" if inst else "")
    return api("GET", f"/api/v5/trade/orders-algo-pending{q}")

def place_market(inst, side, sz_base, attached_algo=None):
    body = {"instId": inst, "tdMode": "cash", "side": side, "ordType": "market",
            "sz": str(sz_base), "tgtCcy": "base_ccy"}
    if attached_algo:
        # OKX field is attachAlgoOrds (PLURAL). Wrong names are SILENTLY IGNORED —
        # verified live 09-28: entry fills, attachAlgoOrds=[] in order detail.
        body["attachAlgoOrds"] = [attached_algo]
    d = api("POST", "/api/v5/trade/order", body)
    return d[0]

def place_maker_entry(inst, qty, px, attached_algo=None):
    """post_only limit buy at px => fills as MAKER (0.1%) or rests; auto-cancelled
    by OKX if it would cross the book. Attached OCO arms at FILL time."""
    ins = instrument(inst)
    body = {"instId": inst, "tdMode": "cash", "side": "buy", "ordType": "post_only",
            "sz": str(qty), "px": str(round_step(px, ins["tickSz"])), "tgtCcy": "base_ccy"}
    if attached_algo:
        body["attachAlgoOrds"] = [attached_algo]
    d = api("POST", "/api/v5/trade/order", body)
    return d[0]

def cancel_order(inst, ord_id):
    try:
        api("POST", "/api/v5/trade/cancel-order", {"instId": inst, "ordId": ord_id})
        return True
    except Exception as e:
        logrec({"event": "cancel_err", "inst": inst, "ordId": ord_id, "err": str(e)[:150]})
        return False

def wait_fill(inst, ord_id, wait_s):
    """Poll until filled/canceled or timeout; returns final order detail."""
    t0 = time.time()
    while time.time() - t0 < wait_s:
        od = order_detail(inst, ord_id)
        if od["state"] in ("filled", "canceled", "cancelled"):
            return od
        time.sleep(2)
    return order_detail(inst, ord_id)

def cancel_algos(algo_ids):
    """algo_ids: list of (algoId, instId)"""
    if not algo_ids:
        return
    body = [{"algoId": a, "instId": i} for a, i in algo_ids]
    api("POST", "/api/v5/trade/cancel-algos", body)

def load_meta():
    try:
        return json.load(open(META))
    except Exception:
        return {}

def save_meta(m):
    json.dump(m, open(META, "w"))

SOCIAL = os.path.join(HERE, "social.json")
SOCIAL_MAX_AGE_MIN = 45   # feed caches 15m; tolerate one missed refresh

def load_social():
    """Keyless social/regime feed written by social_feed.py (CoinGecko per-coin
    sentiment + trending, alternative.me Fear&Greed). Replaces OKX orbit
    news/sentiment, which is not deployed for EEA accounts (see social_feed.py
    docstring for the endpoint evidence). Returns None on stale/absent file —
    every consumer must treat social as OPTIONAL, never as a hard gate."""
    try:
        if time.time() - os.path.getmtime(SOCIAL) > SOCIAL_MAX_AGE_MIN * 60:
            logrec({"event": "social_stale", "age_min": round((time.time() - os.path.getmtime(SOCIAL)) / 60)})
            return None
        return json.load(open(SOCIAL))
    except Exception:
        return None

def trim_state(state):
    """Enforce the Jev prompt budget (MAX_STATE_CHARS). Priority order for
    cuts: news headlines -> macro events -> per-pair redundant fields.
    `pairs` numbers and `context` are never dropped — they ARE the decision.
    Returns (state, list_of_cuts)."""
    cuts = []
    def size():
        return len(json.dumps(state, separators=(",", ":")))
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    # 1. drop news headlines (least decision-relevant at 5-min scale)
    if "news_catalysts" in state:
        state["news_catalysts"] = state["news_catalysts"][:3]
        cuts.append("news_catalysts->3")
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    state.pop("news_catalysts", None)
    state.pop("news_updated", None)
    cuts.append("news_catalysts=dropped")
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    # 2. shrink macro events (upcoming first — surprises are rarer/smaller)
    if "macro_events_24h" in state:
        state["macro_events_24h"] = state["macro_events_24h"][:3]
        cuts.append("macro->3")
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    state.pop("macro_events_24h", None)
    cuts.append("macro=dropped")
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    # 3. strip per-pair fields that duplicate the bracket or add little
    for pr, v in state.get("pairs", {}).items():
        for k in ("day_high", "day_low", "bid", "ask", "pos_off_day_low_pct", "vol24h_eur"):
            v.pop(k, None)
        soc = v.get("social")
        if isinstance(soc, dict):
            soc.pop("vol24h_meur", None)
            soc.pop("watchlists_k", None)
    cuts.append("pairs:secondary fields")
    if size() <= MAX_STATE_CHARS:
        return state, cuts
    # 4. last resort: keep only the top 3 pairs by momentum (they're ranked)
    pairs = state.get("pairs", {})
    if len(pairs) > 3:
        state["pairs"] = dict(list(pairs.items())[:3])
        cuts.append(f"pairs->{len(state['pairs'])}")
    return state, cuts

# ---------------- v3 risk state: exit ledger, cooldown, daily breaker ----------------

RISK = os.path.join(HERE, "risk_state.json")

def load_risk():
    try:
        return json.load(open(RISK))
    except Exception:
        return {"exits": {}, "day": None, "day_equity_start": None, "breaked": False}

def save_risk(r):
    json.dump(r, open(RISK, "w"))

def utc_day():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

def mark_exit(inst, upc):
    """Record a closed trade for cooldown + daily PnL tracking."""
    r = load_risk()
    r.setdefault("exits", {})[inst] = {"ts": now_iso(), "upc": round(upc, 2)}
    save_risk(r)

def in_cooldown(inst):
    """Per-pair cooldown after ANY exit. Loss -> COOLDOWN_MIN (30). Win ->
    WIN_COOLDOWN_MIN (15, v3.5): a TP/trail exit marks the local top; immediate
    re-entries bought that top and got stopped (Oct 2 CT-EUR churn). Returns the
    minutes still on cooldown, or 0 if clear."""
    r = load_risk()
    e = r.get("exits", {}).get(inst)
    if not e:
        return 0
    lim = COOLDOWN_MIN if float(e.get("upc", 0)) < 0 else WIN_COOLDOWN_MIN
    age_min = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(e["ts"])).total_seconds() / 60
    return max(0.0, lim - age_min) if age_min < lim else 0

def equity_eur():
    b = api("GET", "/api/v5/account/balance")[0]
    usd = float(b.get("totalEq") or 0)
    # totalEq is USD-denominated; derive the live USD/EUR rate from the EUR
    # balance row itself (eqUsd / cashBal) — no EURUSDT spot pair exists on EEA
    try:
        eur_row = next(d for d in b.get("details", []) if d["ccy"] == "EUR")
        cash = float(eur_row.get("cashBal") or 0)
        equsd = float(eur_row.get("eqUsd") or 0)
        if cash > 1 and equsd > 1:
            return usd / (equsd / cash)
    except Exception:
        pass
    return usd / 1.13        # fallback approximation

def check_daily_breaker():
    """Roll the daily ledger at UTC midnight; trip STOP if realized day loss
    exceeds DAILY_LOSS_PCT of start-of-day equity. Approximates realized PnL
    from logged exits (upc * stake)."""
    r = load_risk()
    day = utc_day()
    if r.get("day") != day:
        r.update({"day": day, "day_equity_start": round(equity_eur(), 2),
                  "breaked": False})
        save_risk(r)
        # auto-resume: the breaker is PER-DAY. Clear a breaker-written STOP at
        # UTC midnight roll so the engine restarts fresh (a human-written STOP
        # file has different content and is left untouched).
        try:
            if os.path.exists(STOP) and open(STOP).read().startswith("daily breaker"):
                os.remove(STOP)
                logrec({"event": "breaker_stop_cleared", "day": day,
                        "note": "new UTC day — daily breaker STOP auto-cleared"})
        except Exception:
            pass
        logrec({"event": "day_roll", "day": day, "start_equity_eur": r["day_equity_start"]})
        return False
    start = r.get("day_equity_start")
    if not start or r.get("breaked"):
        return False
    eq = equity_eur()
    pnl_pct = (eq - start) / start * 100
    if pnl_pct <= -DAILY_LOSS_PCT:
        r["breaked"] = True
        save_risk(r)
        open(STOP, "w").write(f"daily breaker {day}: {pnl_pct:.2f}% <= -{DAILY_LOSS_PCT}%\n")
        logrec({"event": "daily_breaker", "day": day, "pnl_pct": round(pnl_pct, 2),
                "start": start, "eq": round(eq, 2), "note": "STOP file written"})
        return True
    return False

# ---------------- jev ----------------

def jev(state, questions, timeout=30):
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TS['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ---------------- manage ----------------

def _rearm_oco(inst, sz_hint, px, bk, live_algos, m, meta):
    """Re-place the protective OCO after a failed/skipped trail arm. The OCO was
    cancelled to unfreeze the base balance, so WITHOUT this the position would
    sit unprotected until the next local poll. Anchor at the live price with the
    position's own bracket; sized from live avail if sz_hint unusable."""
    try:
        base = inst.split("-")[0]
        ins = instrument(inst)
        tick = float(ins["tickSz"])
        sz = sz_hint
        if not sz or sz < float(ins["minSz"]):
            sz = round_step(balances().get(base, {}).get("avail", 0), ins["lotSz"])
        if sz < float(ins["minSz"]):
            logrec({"event": "rearm_oco_skip", "inst": inst, "sz": sz})
            return
        body = {"instId": inst, "tdMode": "cash", "side": "sell", "ordType": "oco",
                "sz": str(sz),
                "tpTriggerPx": str(round_step(px * (1 + float(bk.get("tp_pct", MIN_TARGET_PCT)) / 100), tick)),
                "tpOrdPx": "-1",
                "slTriggerPx": str(round_step(px * (1 - float(bk.get("sl_pct", MIN_TARGET_PCT * SL_RATIO)) / 100), tick, down=False)),
                "slOrdPx": "-1"}
        d = api("POST", "/api/v5/trade/order-algo", body)
        aid = (d[0].get("algoId") if d else None)
        if aid:
            live_algos.setdefault(inst, []).append(aid)
            m["algo_ok"] = True
            save_meta(meta)
            logrec({"event": "rearm_oco", "inst": inst, "algoId": aid, "sz": sz})
        else:
            logrec({"event": "rearm_oco_err", "inst": inst, "resp": json.dumps(d)[:150]})
    except Exception as e:
        logrec({"event": "rearm_oco_err", "inst": inst, "err": str(e)[:200]})

def manage():
    """Deterministic exits: stop-loss, trailing take-profit, time-stop. Runs every
    cycle. Server-side OCO algos are the first line for the HARD stop; the trailing
    exit and time stops are owned here (OKX algos don't trail or time-exit)."""
    actions = []
    meta = load_meta()
    if not meta:
        check_daily_breaker()   # breaker must run even on flat days
        return actions
    bals = balances()
    live_algos = {}
    try:
        for a in pending_algos():
            live_algos.setdefault(a["instId"], []).append(a["algoId"])
        # v3.4: server-side trailing algos also protect the position — include
        # them so prune/cancel logic sees every live sell-side algo
        for a in pending_algos(ordType="move_order_stop"):
            live_algos.setdefault(a["instId"], []).append(a["algoId"])
    except Exception as e:
        logrec({"event": "algo_fetch_err", "err": str(e)[:120]})
    for inst, m in list(meta.items()):
        try:
            t = ticker(inst)
        except Exception as e:
            logrec({"event": "manage_ticker_err", "inst": inst, "err": str(e)[:120]})
            continue
        px = float(t["last"])
        entry = float(m.get("entry_ref") or 0)
        if entry <= 0:
            continue
        upc = (px - entry) / entry * 100
        # v3.1: each position carries its OWN cost-aware bracket (set at entry
        # from that pair's live spread). Positions opened before v3.1 have no
        # bracket stored, so derive one from the live spread — still sane.
        bk = m.get("bracket")
        if not bk:
            try:
                bid0, ask0 = float(t.get("bidPx") or 0), float(t.get("askPx") or 0)
                live_spread = (ask0 - bid0) / ask0 * 100 if ask0 > 0 and bid0 > 0 else 0.1
            except Exception:
                live_spread = 0.1
            bk = bracket_for(live_spread)
            logrec({"event": "bracket_derived", "inst": inst, "bracket": bk})
        stop_pct = float(bk.get("sl_pct") or MIN_TARGET_PCT * SL_RATIO)
        target_pct = float(bk.get("tp_pct") or MIN_TARGET_PCT)
        arm_pct = float(bk.get("trail_arm_pct") or target_pct * TRAIL_ARM_RATIO)
        gb_pct = float(bk.get("trail_giveback_pct") or target_pct * TRAIL_GIVEBACK_RATIO)
        floor_pct = float(bk.get("trail_floor_pct") or target_pct * TRAIL_FLOOR_RATIO)
        # --- trailing state (v3): HWM tracked in meta; arm/floor/giveback exits
        hwm = max(float(m.get("hwm_pct") or 0), upc)
        if abs(hwm - float(m.get("hwm_pct") or 0)) > 1e-9:
            m["hwm_pct"] = round(hwm, 3)
            save_meta(meta)
        # v3.4 SERVER-SIDE TRAIL: the local trail only evaluates on the 5-min
        # poll — SUI 10-01 16:40 gave back 1.30% HWM -> -0.13% exit between
        # cycles. Once the trail arms, swap the OCO for move_order_stop
        # (verified supported on eea.okx.com spot 10-02: ordType accepted,
        # callBackRatio range 0.001-1, activation via activePx). The exchange
        # then tracks the peak tick-by-tick and market-sells on the giveback.
        if hwm >= arm_pct and not m.get("trail_algo"):
            try:
                base_ = inst.split("-")[0]
                ins_ = instrument(inst)
                cash_ = bals.get(base_, {}).get("cash", 0)
                tsz = round_step(cash_, ins_["lotSz"])
                if tsz >= float(ins_["minSz"]):
                    # cancel the OCO first (it freezes the whole base balance)
                    cancel_algos([(a, inst) for a in live_algos.get(inst, [])])
                    live_algos.pop(inst, None)
                    bals = balances()
                    cash_ = bals.get(base_, {}).get("avail", 0)
                    tsz = round_step(cash_, ins_["lotSz"])
                if tsz >= float(ins_["minSz"]):
                    tick_ = float(ins_["tickSz"])
                    # activate immediately: activePx just under the live price
                    act = round_step(px * 0.9995, tick_)
                    d = api("POST", "/api/v5/trade/order-algo",
                            {"instId": inst, "tdMode": "cash", "side": "sell",
                             "ordType": "move_order_stop", "sz": str(tsz),
                             "callBackRatio": str(round(gb_pct / 100, 5)),
                             "activePx": str(act)})
                    aid = (d[0].get("algoId") if d else None)
                    if aid:
                        m["trail_algo"] = aid
                        live_algos.setdefault(inst, []).append(aid)
                        logrec({"event": "trail_armed", "inst": inst, "algoId": aid,
                                "hwm_pct": round(hwm, 3), "callBackRatio": round(gb_pct / 100, 5),
                                "activePx": act, "sz": tsz})
                        save_meta(meta)
                    else:
                        logrec({"event": "trail_arm_err", "inst": inst, "resp": json.dumps(d)[:150]})
                        _rearm_oco(inst, tsz, px, bk, live_algos, m, meta)
                else:
                    logrec({"event": "trail_arm_skip", "inst": inst, "sz": tsz,
                            "minSz": ins_["minSz"]})
                    _rearm_oco(inst, tsz, px, bk, live_algos, m, meta)
            except Exception as e:
                logrec({"event": "trail_arm_err", "inst": inst, "err": str(e)[:200]})
                try:
                    _rearm_oco(inst, 0, px, bk, live_algos, m, meta)
                except Exception:
                    pass
        opened = m.get("opened_ts")
        age_min = None
        if opened:
            age_min = (datetime.datetime.now(datetime.timezone.utc)
                       - datetime.datetime.fromisoformat(opened)).total_seconds() / 60
        why = None
        if upc <= -stop_pct:
            why = f"stop-loss {upc:.2f}% (sl {stop_pct:.2f}%)"
        elif hwm >= arm_pct:
            # armed: exit on giveback from HWM, never below the profit floor
            if upc <= max(hwm - gb_pct, floor_pct):
                why = f"trail-exit {upc:.2f}% (hwm {hwm:.2f}%, gb {gb_pct:.2f}%)"
        elif upc >= target_pct:
            why = f"take-profit {upc:.2f}% (tp {target_pct:.2f}%)"
        elif age_min and age_min > TIME_STOP_MIN and upc < 0:
            why = f"time-stop {age_min:.0f}m losing {upc:.2f}%"
        elif age_min and age_min > STALE_EXIT_MIN and upc < target_pct * 0.3:
            # capital rotation: stuck position goes nowhere -> free the slot
            why = f"stale-exit {age_min:.0f}m flat {upc:.2f}%"
        if not why:
            continue
        base = inst.split("-")[0]
        ins = instrument(inst)
        # v3 FIX: cancel the server OCO FIRST, then re-read balances — a live
        # OCO freezes the entire base balance (avail≈0), so sizing the sell
        # from the stale snapshot lands on the close_dust path and silently
        # drops a live position from meta.
        try:
            cancel_algos([(a, inst) for a in live_algos.get(inst, [])])
            live_algos.pop(inst, None)
        except Exception as e:
            logrec({"event": "algo_cancel_err", "inst": inst, "err": str(e)[:120]})
        try:
            bals = balances()
        except Exception:
            pass
        avail = bals.get(base, {}).get("avail", 0)
        # sell whatever is actually available (entry fee is deducted in base ccy,
        # so avail < bought qty — selling the full bought qty 51008s, verified live)
        sz = round_step(avail, ins["lotSz"])
        if sz < float(ins["minSz"]):
            # v3: do NOT pop here — the base balance being dust means the exit
            # already happened server-side (OCO). Leave meta in place so the
            # prune loop below logs the real fill (close_detected) + cooldown.
            logrec({"event": "close_dust", "inst": inst, "sz": sz, "minSz": ins["minSz"],
                    "note": "deferred to prune loop for server-side exit detection"})
            continue
        try:
            o = place_market(inst, "sell", sz)
            fee = None
            try:
                time.sleep(1)
                od = order_detail(inst, o["ordId"])
                fee = od.get("fee")
                fill_px = od.get("avgPx")
                if fill_px:
                    upc = (float(fill_px) - entry) / entry * 100
            except Exception:
                pass
            actions.append({"closed": inst, "reason": why, "ordId": o.get("ordId"), "upc": round(upc, 2)})
            logrec({"event": "close", "inst": inst, "reason": why, "upc": round(upc, 2),
                    "age_min": round(age_min or 0, 1), "ordId": o.get("ordId"), "realized_fee": fee})
            mark_exit(inst, upc)
            meta.pop(inst, None)
        except Exception as e:
            logrec({"event": "close_err", "inst": inst, "err": str(e)[:200]})
    # prune meta for instruments whose base CASH balance fell BELOW minSz — the
    # server-side OCO algo fired and left only fee dust (dust is >0, so a
    # `avail > 0` check never prunes and phantom positions block MAX_OPEN —
    # verified live 09-28).
    # v3 FIX (09-29): must use cashBal, NOT availBal — a live OCO sell leg
    # FREEZES the whole base balance, so avail≈0 while the position is very
    # much open. v2 pruned live positions this way (XRP 03:07, NEAR/XRP 03:59):
    # meta dropped them, trailing/time exits never ran, only the server OCO did.
    # Also skip pruning while an OCO for the inst is still live.
    for inst in list(meta):
        base = inst.split("-")[0]
        try:
            ins = instrument(inst)
            min_sz = float(ins["minSz"])
        except Exception:
            min_sz = 0
        cash = bals.get(base, {}).get("cash", 0)
        if cash >= max(min_sz, 1e-12) or live_algos.get(inst):
            continue
        entry = float(meta[inst].get("entry_ref") or 0)
        opened_ts = meta[inst].get("opened_ts")
        try:
            # v3 fix: match a sell fill that happened AFTER this position opened —
            # v2 matched the newest fill blindly and misreported stale closes
            # (e.g. "+4.59%" from a fill hours earlier).
            opened_ms = (int(datetime.datetime.fromisoformat(opened_ts).timestamp() * 1000)
                         if opened_ts else 0)
            fills = api("GET", f"/api/v5/trade/fills?instId={inst}&limit=20")
            sell = next((f for f in fills
                         if f.get("side") == "sell" and int(f["ts"]) >= opened_ms - 5000), None)
            if sell:
                fpx = float(sell["fillPx"])
                upc = (fpx - entry) / entry * 100 if entry else 0
                logrec({"event": "close_detected", "inst": inst, "upc": round(upc, 2),
                        "fill_px": fpx, "realized_fee": sell.get("fee"),
                        "note": "exit happened server-side (OCO algo)"})
                actions.append({"closed": inst, "reason": f"server-side OCO exit {upc:+.2f}%",
                                "upc": round(upc, 2)})
                mark_exit(inst, upc)
            else:
                logrec({"event": "prune_no_fill", "inst": inst, "avail": cash,
                        "note": "cash below minSz, no sell fill after opened_ts"})
        except Exception as e:
            logrec({"event": "prune_err", "inst": inst, "err": str(e)[:120]})
        meta.pop(inst, None)
    save_meta(meta)
    check_daily_breaker()
    return actions

# ---------------- decide ----------------

def pair_state(inst):
    t = ticker(inst)
    bid, ask = float(t["bidPx"] or 0), float(t["askPx"] or 0)
    if bid <= 0 or ask <= 0:
        return None   # stale/absent data = NOT tradeable, never a price
    last = float(t["last"])
    open24 = float(t.get("open24h") or last)
    hi24, lo24 = float(t.get("high24h") or last), float(t.get("low24h") or last)
    s = {"bid": bid, "ask": ask,
         "spread_pct": round((ask - bid) / ask * 100, 4),
         "vol24h_eur": round(float(t.get("volCcy24h") or 0)),
         "day_chg_pct": round((last - open24) / open24 * 100, 2),
         "day_high": hi24, "day_low": lo24,
         "pos_off_day_low_pct": round((last - lo24) / max(hi24 - lo24, 1e-9) * 100, 1)}
    bars = candles(inst, "5m", 7)
    if len(bars) >= 3:
        closes = [b["c"] for b in bars]
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        rng_hi, rng_lo = max(highs), min(lows)
        s.update({
            "chg_30min_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 3),
            "range30_pos_pct": round((closes[-1] - rng_lo) / max(rng_hi - rng_lo, 1e-9) * 100, 1),
            "bars_up_of_6": sum(1 for a, b in zip(closes, closes[1:]) if b > a),
            "volatility_30min_pct": round((rng_hi - rng_lo) / closes[0] * 100, 3),
        })
    # v3.3 MULTI-TIMEFRAME context (user-flagged gap 10-01: Jev saw only 30min
    # while NEAR was +25% on the week — the strategy premise is "pullback in a
    # strong uptrend", but the multi-day trend was invisible to the model).
    # Cheap: 1H + 1D candle calls, public endpoints.
    try:
        h1 = candles(inst, "1H", 5)
        if len(h1) >= 5:
            hc = [b["c"] for b in h1]
            s["chg_4h_pct"] = round((hc[-1] - hc[0]) / hc[0] * 100, 2)
    except Exception:
        pass
    try:
        d1 = candles(inst, "1D", 8)
        if len(d1) >= 8:
            dc = [b["c"] for b in d1]
            s["chg_7d_pct"] = round((last / dc[0] - 1) * 100, 1)
            # trend structure: is price above/below its 7d midline?
            rng_hi7 = max(b["h"] for b in d1)
            rng_lo7 = min(b["l"] for b in d1)
            s["pos_7d_range_pct"] = round((last - rng_lo7) / max(rng_hi7 - rng_lo7, 1e-9) * 100, 1)
            s["days_up_of_7"] = sum(1 for b in d1 if b["c"] > b["o"])
    except Exception:
        pass
    return s

def decide():
    if os.path.exists(STOP):
        return {"halted": True}
    cash = eur_avail()
    meta = load_meta()
    open_pos = [i for i in meta]
    if len(open_pos) >= MAX_OPEN:
        return {"skip": f"max open {len(open_pos)}"}
    # user directive 09-28: trade with ALL available funds — split the free EUR
    # evenly across the remaining open slots so the full bankroll is deployed
    # once all slots fill (one entry per cycle; later entries re-read free cash).
    slots_left = MAX_OPEN - len(open_pos)
    want_stake = cash * 0.98 / slots_left
    if want_stake < MIN_STAKE_EUR:
        return {"skip": f"insufficient EUR {cash:.2f} (stake {want_stake:.2f} < min)"}
    # v3.1: universe is selected live from ALL EUR pairs (cached 10 min), not a
    # hardcoded list. Positions in meta and pairs on cooldown (loss 30m / win
    # 15m, v3.5) are excluded.
    universe, _scanned = select_universe()
    pstate = {}
    for pr in universe:
        if pr in meta:
            continue
        cd_left = in_cooldown(pr)
        if cd_left:
            ex = load_risk().get("exits", {}).get(pr, {})
            logrec({"event": "cooldown_skip", "inst": pr,
                    "left_min": round(cd_left, 1),
                    "after": "loss" if float(ex.get("upc", 0)) < 0 else "win"})
            continue
        try:
            s = pair_state(pr)
        except Exception as e:
            logrec({"event": "state_err", "inst": pr, "err": str(e)[:120]})
            continue
        if s:
            # attach this pair's cost-aware bracket (from its LIVE spread) so the
            # gates, the Jev prompt and the entry order all use the same numbers
            s["bracket"] = bracket_for(s["spread_pct"])
            pstate[pr] = s
    tradeable = {k: v for k, v in pstate.items()
                 if v["spread_pct"] <= MAX_SPREAD_PCT and v.get("vol24h_eur", 0) >= MIN_VOL24H_EUR}
    # v3 entry-quality gates IN CODE (24h post-mortem: Jev at low conf entered
    # flat/falling pairs that never reached TP — momentum must exist BEFORE Jev
    # is consulted). v3.1: the volatility gate is per-pair — realized 30m range
    # must be big enough to REACH that pair's own TP, not a fixed 0.9%.
    quality = {}
    for k, v in tradeable.items():
        bk = v["bracket"]
        fails = []
        if v.get("volatility_30min_pct", 0) < bk["min_vol30_pct"]:
            fails.append(f"vol30 {v.get('volatility_30min_pct')}<{bk['min_vol30_pct']} (tp {bk['tp_pct']})")
        if v.get("chg_30min_pct", 0) <= MIN_CHG30_PCT:
            fails.append(f"chg30 {v.get('chg_30min_pct')}<={MIN_CHG30_PCT}")
        if v.get("bars_up_of_6", 0) < MIN_BARS_UP:
            fails.append(f"bars_up {v.get('bars_up_of_6')}<{MIN_BARS_UP}")
        rpos = v.get("range30_pos_pct", 0)
        if not (30 <= rpos <= 92):
            fails.append(f"range30_pos {rpos} not in 30-92")
        if fails:
            logrec({"event": "quality_gate_reject", "inst": k, "fails": fails})
        else:
            quality[k] = v
    # ASSERT non-empty BEFORE the Jev call: empty candidate set masquerades as
    # no_trade with prob 1.0 (alpaca-trading skill §1 lesson, venue-independent)
    if not quality:
        logrec({"event": "no_trade", "why": "no pairs passed quality gates", "pairs": list(pstate)})
        return {"skip": "no pairs passed quality gates", "acted": False}
    # deepen state with OKX-native sentiment (positioning + taker flow) per pair
    for pr in list(quality):
        base = pr.split("-")[0]
        s = rubik_sentiment(base)
        if s:
            quality[pr]["sentiment"] = s
        # net aggressive buying is part of the setup: drop pairs without it
        if s.get("taker_buy_share", 1.0) < MIN_TAKER_BUY:
            logrec({"event": "quality_gate_reject", "inst": pr,
                    "fails": [f"taker_buy_share {s.get('taker_buy_share')}<{MIN_TAKER_BUY}"]})
            quality.pop(pr)
    if not quality:
        logrec({"event": "no_trade", "why": "no pairs with net taker buying"})
        return {"skip": "no pairs with net taker buying", "acted": False}
    # PROMPT BUDGET: cap how many pairs Jev sees. Rank by momentum strength
    # (chg_30min x vol30) so the strongest setups survive the cap, then attach
    # each pair's social block only for survivors.
    ranked = sorted(quality.items(),
                    key=lambda kv: -(kv[1].get("chg_30min_pct", 0) * kv[1].get("volatility_30min_pct", 0)))
    if len(ranked) > MAX_JEV_PAIRS:
        dropped = [k for k, _ in ranked[MAX_JEV_PAIRS:]]
        logrec({"event": "jev_pair_cap", "kept": [k for k, _ in ranked[:MAX_JEV_PAIRS]],
                "dropped": dropped})
        ranked = ranked[:MAX_JEV_PAIRS]
    tradeable = dict(ranked)
    social = load_social()
    if social:
        coins = social.get("coins", {})
        for pr in tradeable:
            sc = coins.get(pr)
            if sc:
                tradeable[pr]["social"] = sc
    fbps = market_funding_bps()
    # v3.1 prompt: context trimmed (~40% shorter — the verbose v3 text cost
    # ~800 tokens/call explaining things the numbers already show), social +
    # macro folded in, per-pair bracket numbers embedded so Jev prices its own
    # noul question correctly. Budget enforced by trim_state() before the call.
    fg = (social or {}).get("fear_greed")
    state = {
        "context": ("Momentum scalping, OKX Europe (EEA) spot, EUR pairs, long-only. "
                    "Entry maker 0.1% (IOC chase if unfilled), exit taker 0.2%. Each pair's "
                    "bracket was priced from its live cost (spread+fees): tp_pct = take-profit, "
                    "sl_pct = hard stop, trail arms at trail_arm_pct and exits on "
                    "trail_giveback_pct retrace (floor trail_floor_pct); min_vol30_pct = realized "
                    "30m range needed for tp to be reachable. Time-stop 30m losing, stale-exit 90m. "
                    "All candidates already passed momentum gates (30m uptrend, green bars, "
                    "pullback position in range, net taker buying), so choose between them on "
                    "setup QUALITY and the facts below. "
                    "Per pair: chg_30min_pct (trend), range30_pos_pct (0=30m low,100=30m high), "
                    "bars_up_of_6, volatility_30min_pct, spread_pct, day_chg_pct, "
                    "MULTI-TIMEFRAME trend: chg_4h_pct, chg_7d_pct, pos_7d_range_pct (0=7d low, "
                    "100=7d high), days_up_of_7. A strong chg_7d_pct with a mild 30min dip is the "
                    "ideal pullback-in-uptrend; chg_7d_pct deeply negative means the 30min bounce "
                    "is a bear-market rally — treat with far more caution. sentiment "
                    "(ls_ratio = OKX perps crowd long/short; taker_buy_share >0.55 = real buying), "
                    "social (sent_up_pct = CoinGecko community poll 0-100, trending_rank = "
                    "attention spike 1-10, watchlists_k, off_ath_pct = overhead supply). "
                    "market_funding_bps = BTC perp funding (crowding). fear_greed = market-wide "
                    "0-100 with streak_days (long one-band streaks = contrarian caution). "
                    "macro_events_24h = upcoming high-impact releases (in_h = hours away; trade "
                    "the trend, not the event — avoid fresh entries right before imp3 releases). "
                    "macro_released_3h_surprises = recent releases where actual beat/missed "
                    "forecast (act vs fc) — these drive the current macro tape (e.g. soft "
                    "inflation print = risk-on tailwind for longs). "
                    "Best setup: pullback in a sustained uptrend, real taker buying, room to the "
                    "30m high, tp reachable vs realized vol, retail sentiment NOT diverging hard "
                    "from flow (e.g. sent_up 85% while taker_buy_share 0.5 = distribution risk). "
                    "When unsure pick no_trade: a missed scalp costs 0, a chop costs ~0.5-0.9%."),
        "market_funding_bps": fbps,
        "cash_eur": round(cash, 2),
        "stake_eur": round(want_stake, 2),
        "open_positions": [{"inst": i, "entry_ref": meta[i].get("entry_ref")} for i in open_pos],
        "pairs": tradeable,
    }
    if fg:
        state["fear_greed"] = fg
    tr = (social or {}).get("trending")
    if tr:
        state["trending_syms"] = tr[:8]
    # news catalysts: OKX orbit news/sentiment is GLOBAL-SITE ONLY (404 on
    # eea.okx.com, 401 on www with EEA key/OAuth — re-verified 09-30 via the
    # official MCP package paths), so use keyless RSS (catalyst.json) + macro
    try:
        cat = json.load(open(os.path.join(HERE, "catalyst.json")))
        state["news_catalysts"] = cat.get("catalysts", [])[:6]
        state["news_updated"] = cat.get("updated")
    except Exception:
        pass
    # OKX economic calendar (public, works on eea with key auth).
    # CRITICAL (verified live 09-30): before/after are INVERTED on this endpoint
    # — before=<ts> = events NEWER than ts (lower bound), after=<ts> = events
    # OLDER than ts (upper bound, defaults to now). The naive `after=now&
    # date>now` filter silently matched NOTHING, so macro context was empty all
    # day — including the 12:30 UTC PCE/GDP release that moved BTC +1.7%.
    # Correct windows: upcoming 24h = before=now & after=now+24h;
    #                  last 3h releases = after=now & before=now-3h.
    try:
        now_ms = int(time.time() * 1000)
        H = 3600000
        ev = []
        # upcoming high-impact catalysts (heads-up: volatility windows ahead)
        cal = api("GET", f"/api/v5/public/economic-calendar?before={now_ms}&after={now_ms + 24 * H}&limit=10")
        for c in cal:
            try:
                imp = int(c.get("importance") or 0)
            except Exception:
                imp = 0
            if imp < 2 or not c.get("date"):
                continue
            dt = int(c["date"])
            if now_ms <= dt < now_ms + 24 * H:
                ev.append({"e": c.get("event"), "ccy": c.get("ccy") or "$", "imp": imp,
                           "in_h": round((dt - now_ms) / H, 1)})
        ev.sort(key=lambda x: x["in_h"])
        if ev:
            state["macro_events_24h"] = ev[:6]
        # recent releases WITH surprises (actual != forecast): the momentum fuel
        # of the last hours — a soft inflation print pumping BTC is exactly the
        # regime fact Jev needs. imp>=2 only, compact fields.
        cal = api("GET", f"/api/v5/public/economic-calendar?after={now_ms}&before={now_ms - 3 * H}&limit=30")
        rel = []
        for c in cal:
            try:
                imp = int(c.get("importance") or 0)
            except Exception:
                imp = 0
            act, fc = c.get("actual") or "", c.get("forecast") or ""
            if imp < 2 or not act or not fc or act == fc or not c.get("date"):
                continue
            dt = int(c["date"])
            rel.append({"e": c.get("event"), "imp": imp, "act": act, "fc": fc,
                        "ago_h": round((now_ms - dt) / H, 1)})
        rel.sort(key=lambda x: x["ago_h"])
        if rel:
            state["macro_released_3h_surprises"] = rel[:5]
    except Exception as e:
        logrec({"event": "macro_err", "err": str(e)[:120]})
    # PROMPT BUDGET: measure and trim to MAX_STATE_CHARS before sending
    state, trimmed = trim_state(state)
    questions = {}
    crit = {}
    slug = {}
    for pr in sorted(tradeable):
        k = pr.split("-")[0].lower()
        slug["buy_" + k] = pr
        arm = tradeable[pr]["bracket"]["trail_arm_pct"]
        questions["p_up_" + k] = {"type": "noul",
            "instructions": f"{pr} will rise at least {arm}% within the next 45 minutes"}
        crit["buy_" + k] = f"Open long {pr} now"
    crit["no_trade"] = "No pair has a profitable momentum setup; stay in cash"
    questions["best_action"] = {"type": "choice",
        "instructions": "Which single action maximizes expected profit net of each pair's bracket cost over the next 30-90 minutes",
        "criteria": crit}
    ans = jev(state, questions)
    a = ans["answers"]
    choice = a["best_action"]["choice"]
    conf = a["best_action"].get("confidence", 0)
    # v3.3: decision margin = p(chosen buy) - p(no_trade). Confidence is a
    # certainty metric that saturates low on 45-min calls; the margin is the
    # actual preference signal (see MARGIN_BAR comment + 10-01 replay).
    probs = a["best_action"].get("probabilities") or {}
    margin = round(probs.get(choice, 0) - probs.get("no_trade", 0), 3) if choice != "no_trade" else 0.0
    nouls = {"buy_" + pr.split("-")[0].lower(): a["p_up_" + pr.split("-")[0].lower()]["noul"]
             for pr in tradeable}
    usage = ans.get("usage") or {}
    state_chars = len(json.dumps(state, separators=(",", ":")))
    est_tokens = round(state_chars / CHARS_PER_TOKEN)
    logrec({"event": "jev", "state": state, "answers": a, "usage": usage,
            "prompt_stats": {"state_chars": state_chars, "est_tokens": est_tokens,
                             "n_pairs": len(tradeable), "trimmed": trimmed,
                             "input_tokens": usage.get("input_tokens")}})
    # prompt-budget alarm: visible in log + surfaced so cron reports it
    if usage.get("input_tokens") and usage["input_tokens"] > MAX_JEV_INPUT_TOKENS:
        logrec({"event": "prompt_budget_warn", "input_tokens": usage["input_tokens"],
                "max": MAX_JEV_INPUT_TOKENS, "state_chars": state_chars})
    rec = {"choice": choice, "conf": conf, "margin": margin, "noul": nouls.get(choice)}
    noul_ok = (nouls.get(choice) or 0) >= NOUL_BAR
    edge_ok = (margin >= MARGIN_BAR) or (conf >= CONF_BAR)
    if choice == "no_trade" or not edge_ok or not noul_ok:
        rec["acted"] = False
        logrec({"event": "no_trade", "choice": choice,
                "why": f"conf {conf:.2f} margin {margin:.2f} (bar {MARGIN_BAR}) noul {nouls.get(choice)} (bar {NOUL_BAR})"})
        return rec
    inst = slug.get(choice)
    if not inst:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"unknown choice {choice}"})
        return rec
    sp = tradeable[inst]["spread_pct"]
    if sp > MAX_SPREAD_PCT:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"spread {sp} too wide", "choice": choice})
        return rec
    ins = instrument(inst)
    tick = float(ins["tickSz"])
    # v3.2 ENTRY RE-CHECK (CT-EUR lesson): the scan can be up to 10 min old and
    # thin books drift. Re-read the live book; re-price the bracket from the
    # live spread; require resting depth >= ENTRY_DEPTH_MULT x stake so our own
    # IOC can't move the price 1% against us on fill.
    try:
        b = api("GET", f"/api/v5/market/books?instId={inst}&sz=5", private=False)[0]
        lbid = float(b["bids"][0][0]); lask = float(b["asks"][0][0])
        depth = sum(float(l[0]) * float(l[1]) for l in b.get("bids", [])[:5])
    except Exception as e:
        rec["acted"] = False
        rec["why"] = f"book fetch failed: {str(e)[:80]}"
        logrec({"event": "no_trade", "why": rec["why"], "choice": choice})
        return rec
    live_spread = (lask - lbid) / lask * 100
    if depth < max(want_stake * ENTRY_DEPTH_MULT, MIN_TOP5_DEPTH_EUR):
        rec["acted"] = False
        rec["why"] = f"depth €{depth:.0f} < {ENTRY_DEPTH_MULT}x stake"
        logrec({"event": "no_trade", "why": f"insufficient depth {inst} €{depth:.0f}",
                "choice": choice, "depth_eur": round(depth)})
        return rec
    bk = bracket_for(live_spread)   # re-price off the LIVE spread, not scan-time
    bid = lbid
    qty = round_step(want_stake / bid, ins["lotSz"])
    min_sz = float(ins["minSz"])
    if qty < min_sz:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"qty {qty} < min {min_sz}"})
        return rec
    # OCO sell leg sized NET of entry fee (fee deducted in base ccy — gross 51020s).
    # Levels anchored to BID (expected maker fill), re-checked against fill below.
    # v3.1: TP/SL come from THIS pair's cost-aware bracket, stored in meta so
    # manage() exits at the same levels the order was armed with.
    # v3.2: `bk` is the LIVE-spread bracket from the entry re-check above (the
    # scan-time bracket in tradeable[inst] can be up to 10 min stale).
    algo_sz = round_step(qty * (1 - 0.0015), ins["lotSz"])
    algo = {"ordType": "oco", "sz": str(algo_sz),
            "tpTriggerPx": str(round_step(bid * (1 + bk["tp_pct"] / 100), tick)), "tpOrdPx": "-1",
            "slTriggerPx": str(round_step(bid * (1 - bk["sl_pct"] / 100), tick, down=False)), "slOrdPx": "-1"}
    # v3 entry: post_only at bid; if unfilled and the ask hasn't run away,
    # IOC-chase. v2's pure maker missed ~45% of entries (adverse selection:
    # we only got filled on the moves that came back against us).
    o = place_maker_entry(inst, qty, bid, attached_algo=algo)
    ord_id = o.get("ordId")
    od = wait_fill(inst, ord_id, FILL_WAIT_S)
    stt = od.get("state")
    entry_type = "maker"
    if stt != "filled":
        acc = float(od.get("accFillSz") or 0)
        if stt in ("live", "partially_filled"):
            cancel_order(inst, ord_id)
        if acc >= float(ins["minSz"]):
            # partial maker fill: keep it; standalone OCO anchored to true fill below
            od = order_detail(inst, ord_id)
            acc = float(od.get("accFillSz") or 0)
            qty = round_step(acc, ins["lotSz"])
            entry_type = "maker_partial"
            stt = "filled"
        else:
            try:
                ask_now = float(ticker(inst)["askPx"] or 0)
            except Exception:
                ask_now = 0
            if ask_now <= 0 or ask_now > bid * (1 + CHASE_MAX_PCT / 100):
                rec["acted"] = False
                rec["why"] = f"maker entry unfilled ({stt}), ask ran away"
                logrec({"event": "entry_unfilled", "inst": inst, "state": stt, "ordId": ord_id,
                        "bid": bid, "ask": ask_now})
                return rec
            # chase: market IOC for the remaining qty (taker fee 0.2%)
            o = place_market(inst, "buy", qty)
            ord_id = o.get("ordId")
            time.sleep(1)
            od = order_detail(inst, ord_id)
            stt = od.get("state")
            acc = float(od.get("accFillSz") or 0)
            entry_type = "taker_chase"
            if acc < float(ins["minSz"]):
                rec["acted"] = False
                rec["why"] = f"chase fill too small ({acc})"
                logrec({"event": "entry_chase_failed", "inst": inst, "state": stt, "ordId": ord_id})
                return rec
            qty = round_step(acc, ins["lotSz"])
    fill_px = float(od.get("avgPx") or 0) or bid
    fee = od.get("fee")
    attach_failed = None
    for aa in od.get("attachAlgoOrds") or []:
        if aa.get("failCode"):
            attach_failed = f"{aa['failCode']}: {aa.get('failReason','')}"
    # verify the attached OCO actually armed (never trust 200 = protection exists)
    algo_ok = False
    try:
        algo_ok = bool(live_algos_check(inst))
    except Exception:
        pass
    if not algo_ok:
        # fallback: standalone OCO sized to ACTUAL balance, anchored to TRUE fill
        try:
            avail = balances().get(inst.split("-")[0], {}).get("avail", 0)
            fsz = round_step(avail, ins["lotSz"])
            if fsz >= float(ins["minSz"]):
                body = {"instId": inst, "tdMode": "cash", "side": "sell", "ordType": "oco",
                        "sz": str(fsz),
                        "tpTriggerPx": str(round_step(fill_px * (1 + bk["tp_pct"] / 100), tick)), "tpOrdPx": "-1",
                        "slTriggerPx": str(round_step(fill_px * (1 - bk["sl_pct"] / 100), tick, down=False)), "slOrdPx": "-1"}
                d = api("POST", "/api/v5/trade/order-algo", body)
                algo_ok = bool(d and d[0].get("algoId"))
                logrec({"event": "algo_fallback", "inst": inst, "ok": algo_ok,
                        "attach_failed": attach_failed})
        except Exception as e:
            logrec({"event": "algo_fallback_err", "inst": inst, "err": str(e)[:200],
                    "attach_failed": attach_failed})
    meta = load_meta()
    meta[inst] = {"opened_ts": now_iso(), "stake": want_stake, "qty": qty,
                  "entry_ref": fill_px, "jev_conf": conf, "jev_noul": nouls.get(choice),
                  "entry_ordId": ord_id, "algo_ok": algo_ok, "entry_fee": fee,
                  "entry_type": entry_type, "hwm_pct": 0.0, "bracket": bk}
    save_meta(meta)
    rec.update({"acted": True, "inst": inst, "qty": qty, "stake": round(want_stake, 2),
                "ordId": ord_id, "algo_ok": algo_ok, "fill_px": fill_px, "entry_type": entry_type,
                "bracket": bk})
    logrec({"event": "open", "inst": inst, "qty": qty, "stake": round(want_stake, 2),
            "ordId": ord_id, "fill_px": fill_px, "conf": conf, "margin": margin,
            "noul": nouls.get(choice),
            "algo_ok": algo_ok, "realized_fee": fee, "entry_type": entry_type,
            "bracket": bk, "prompt_input_tokens": usage.get("input_tokens")})
    return rec

def live_algos_check(inst):
    return [a for a in pending_algos(inst)]

# ---------------- status ----------------

def status():
    bals = balances()
    meta = load_meta()
    out = {"eur_avail": round(bals.get("EUR", {}).get("avail", 0), 2),
           "total_eq_usd": round(sum(b.get("eq_usd", 0) for b in bals.values()), 2),
           "balances": {c: round(b["avail"], 8) for c, b in bals.items() if b["avail"] > 0},
           "tracked": [], "algos_pending": 0, "halted": os.path.exists(STOP)}
    try:
        u = json.load(open(UNIVERSE_FILE))
        out["universe"] = {"pairs": u.get("pairs"), "scanned": u.get("scanned_iso"),
                           "n_candidates": u.get("n_candidates")}
    except Exception:
        out["universe"] = None
    for inst, m in meta.items():
        try:
            t = ticker(inst)
            px = float(t["last"])
            entry = float(m.get("entry_ref") or px)
            out["tracked"].append({"inst": inst, "qty": m.get("qty"), "entry": entry,
                                   "last": px, "upc%": round((px - entry) / entry * 100, 2),
                                   "algo_ok": m.get("algo_ok")})
        except Exception:
            out["tracked"].append({"inst": inst, "qty": m.get("qty"), "err": "ticker"})
    try:
        out["algos_pending"] = len(pending_algos())
    except Exception:
        pass
    return out

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "manage":
        r = manage()
    elif mode == "decide":
        r = decide()
    elif mode == "cycle":
        # refresh the two keyless feeds (both have internal freshness guards:
        # news 30m, social 15m). OKX orbit news/sentiment is unavailable for EEA
        # accounts — re-verified 09-30 against the official MCP endpoint paths.
        try:
            import subprocess
            subprocess.run([sys.executable, os.path.join(HERE, "news_feed.py")],
                           capture_output=True, timeout=40)
        except Exception:
            pass
        try:
            import subprocess
            subprocess.run([sys.executable, os.path.join(HERE, "social_feed.py")],
                           capture_output=True, timeout=45)
        except Exception as e:
            logrec({"event": "social_feed_err", "err": str(e)[:150]})
        lines = []
        for sw in sweep_stables():
            lines.append(f"SWEPT {sw['ccy']} {sw['sz']} -> {sw['eur_got']} EUR")
        m = manage()
        for c in m:
            lines.append(f"CLOSED {c['closed']} — {c['reason']}")
        # volume directive: keep entering while slots are free
        for _ in range(MAX_ENTRIES_PER_CYCLE):
            d = decide()
            if d.get("acted"):
                bk = d.get("bracket") or {}
                lines.append(f"OPENED {d['inst']} qty={d['qty']} stake={d['stake']}EUR "
                             f"fill={d.get('fill_px')} conf={d['conf']:.2f} "
                             f"tp={bk.get('tp_pct')}% sl={bk.get('sl_pct')}% "
                             f"algo_ok={d.get('algo_ok')}")
                continue
            if d.get("halted"):
                lines.append("HALTED (STOP file) — manage still active")
            elif d.get("why"):
                lines.append(f"entry skipped: {d['why']} (jev {d.get('choice')} conf {d.get('conf', 0):.2f})")
            elif d.get("choice"):
                lines.append(f"no trade: jev {d['choice']} conf {d.get('conf'):.2f} noul {d.get('noul')}")
            break   # gate/no-slot/unfilled — stop entering this cycle
        s = status()
        lines.append(f"eur={s['eur_avail']} eq_usd={s['total_eq_usd']} tracked={s['tracked']} halted={s['halted']}")
        r = {"lines": lines}
    else:
        r = status()
    print(json.dumps(r, indent=1))
