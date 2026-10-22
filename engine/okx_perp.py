#!/usr/bin/env python3
"""OKX Europe X-Perps two-way scalper (v4.0) — LONG and SHORT via MiFID-regulated
USD-margined expiry perps. Companion to okx_jev.py (spot, long-only, EUR): imports
its plumbing (api signing, ticker, candles, jev client, round_step, balances) but
runs fully isolated state:

  - own log      : perp_log.jsonl
  - own positions: perp_positions.json
  - own risk     : perp_risk_state.json (daily USDC-equity breaker, -8%)
  - own kill     : STOP_PERP (breaker) + respects shared STOP (all engines halt)
  - collateral   : USDC only (PERP_COLLATERAL_CCY); EUR spot bankroll untouched
  - margin mode  : ISOLATED at PERP_LEV=1x — a losing position can only eat its
                   own margin, never the account

Why X-Perps (verified live 10-15): EEA account post-unlock (acctLv=2, X-Perp
appropriateness assessment passed) can trade 242 XPERP contracts via API. Fees
0.02% maker / 0.05% taker vs spot EUR 0.1/0.2 — round trip ~0.07-0.11% vs
~0.3%. The fee wall that made 128 spot trades net-negative (-21 EUR on gross
+3.8) does not exist here. Contract naming: <BASE>-USD_UM_XPERP-<yymmdd>.

Two-sided Jev: for each pair a noul p_up AND p_dn, choice set {long_<k>,
short_<k>, no_trade}. Direction-specific noul must clear NOUL_BAR.

Modes: status | decide | manage | cycle   (+ --dry-run: full decision path,
zero order placement — logs perp_dry_open instead of trading).
"""
import json, os, sys, time, datetime, decimal
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import okx_jev as J   # reuse signed api(), ticker(), candles(), jev(), balances()

ENGINE_VERSION = "v4.9.3-perp"
LOG = os.path.join(HERE, "perp_log.jsonl")
META = os.path.join(HERE, "perp_positions.json")
RISK = os.path.join(HERE, "perp_risk_state.json")
STOP_PERP = os.path.join(HERE, "STOP_PERP")
DRY = "--dry-run" in sys.argv

# ---------------- universe ----------------
CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "OKB", "ZEC", "SUI", "LINK",
                "LTC", "ADA", "AVAX", "TON", "DOT", "UNI", "AAVE", "NEAR", "HYPE"}
# 10-21 (v4.9.2): NEAR excluded — 26 closes, 31% WR, sum -1.49% = over half the
# book's -2.73% total; 4 same-day re-entries into one stop. Same bleed as spot
# NEAR-EUR (excluded v3.10, only OOS-robust filter there). Containment, not edge:
# non-NEAR half is still -1.24%/27 trades. Kill-gate count stays anchored to v4.9 (10-18).
UNIVERSE_EXCLUDE = {"NEAR"}
# Stock/commodity X-Perps (AAPL, TSLA, GOLD, ...) are explicitly NOT traded.
MAX_JEV_PAIRS = 6  # v4.9.3 (10-21, operator directive): widen opportunity set.
                     # WATCH: input_tokens was ~2840 at 4 pairs; budget alarm 3400.
MIN_DAY_RANGE_PCT = 1.5        # X-Perp majors move less than EUR small caps
MIN_VOL24H_USD = 300_000
MIN_TOP5_DEPTH_USD = 1_000     # tiny stakes ($8-16) but never enter a ghost book
ENTRY_DEPTH_MULT = 3.0
MAX_SPREAD_PCT = 0.10          # perp books should be tight; skip if not

# ---------------- cost + bracket ----------------
PERP_MAKER_PCT = 0.02
PERP_TAKER_PCT = 0.05
EDGE_MULT = 4.0
MIN_TARGET_PCT = 0.6           # cost ~0.08% -> TP floors at 0.6% (was 1.5 on spot;
MAX_TARGET_PCT = 2.0           # perp round trip is 3x cheaper so targets shrink)
SL_COST_MULT = 1.75
SL_FLOOR_PCT = 0.35
SL_CAP_PCT = 0.80
ARM_TP_RATIO = 0.40            # trail geometry scaled off TP (v3.5 proven ratios)
GB_TP_RATIO = 0.23
FLOOR_TP_RATIO = 0.07
MIN_VOL30_PCT = 0.30   # v4.9: 450 rejects since 10-17 had median vol30 0.27;
                       # 0.35 was calibrated on a moving tape — weekend books sit
                       # 0.25-0.35. 0.30 keeps the reachability logic vs the
                       # ~0.6% TP while admitting the bulk of the opportunity set.           # realized 30m range reachability gate

# ---------------- risk / sizing ----------------
PERP_LEV = "1"                 # isolated leverage; 1x = margin == notional
# FULL-CAPITAL (10-15, operator directive: "if you see opportunity use entire
# capital — spot or short, it's trades"). Was 0.70 + a $25 notional cap, sized
# as a liquidation buffer. That buffer protects against a risk this book does not
# have: at PERP_LEV=1 isolated the exchange liqPx sits ~98% away (measured: BTC
# short liqPx 161324 vs entry 81227), and the stop is ~0.12%, so per-trade risk
# is the stop, not liquidation. A cash buffer only earns its keep at high
# leverage. CANNOT be 1.00: posted margin + fees must fit inside equity or the
# order is rejected for insufficient margin — 0.98 is the practical ceiling.
# Re-introduce a buffer only if PERP_LEV is ever raised above 1.
MAX_NOTIONAL_FRAC = 0.98       # of USDC equity — deploy (nearly) all of it
# BURN-IN SIZE CAP (v4.6, 10-15, CTO decision): back to $25/trade until ~30 closed
# perp trades exist. The 0.40 CONF_FLOOR burn-in was approved as CONTAINED
# (worst bad trade ~-$0.25); same-day full-capital sizing broke that containment
# (n=9: 1W/8L, -$3.41 in 10h, the three conf<0.50 entries each ~-$0.48). At $25
# the sample still accrues at ~1/5 the burn. GATE: at n>=30 run pnl_replay; if no
# edge, close the book and park USDC in flexible savings — do NOT re-size up
# before that review.
MAX_NOTIONAL_USD = 25.0        # burn-in cap (see above); also a bug guard
                               # against a garbage usdc_equity() read
MAX_OPEN = 1
MAX_ENTRIES_PER_CYCLE = 1
LIQ_BUFFER_PCT = 15.0          # force-close if price comes within 15% of liqPx

# ---------------- gates / timing ----------------
# BURN-IN (10-15, user-approved): CONF_FLOOR was inherited from the spot engine
# at 0.50, calibrated on 128 spot trades where sub-0.50 conf lost money AT a
# ~0.28% round-trip cost. Perp round-trip is ~0.07% (~4x cheaper) and Jev simply
# does not emit 0.50+ confidence on these instruments at a 45-min horizon: first
# 16 perp calls averaged conf 0.32 (max 0.51), so CONF_FLOOR 0.50 blocked 15/16
# = an engine that never trades and thus never collects the data to calibrate on
# (chicken-and-egg). Lowered to 0.40 to let a sane ~3/day cadence through while
# contained at $25 max notional / 1x isolated / $22 collateral (worst bad trade
# ~ -$0.25). margin>=0.30 stays the PRIMARY edge gate; noul>=0.45 unchanged.
# PROTOCOL: after ~30 closed perp trades run pnl_replay and set the real floor
# from PERP evidence (exactly how spot v3.8 was derived). Do NOT tune again
# before that sample.
# v4.8 (10-16) FLOOR RE-BASELINED ON THE ENRICHED-STATE DISTRIBUTION: v4.7
# (BTC regime, trade memory, news, macro, rubik) shifted Jev conf down globally
# (avg 0.45->0.26, max 0.85->0.39 over 36 calls) while directional noul held
# (0.497->0.51, 19/21 picks clear NOUL_BAR). The 0.40 floor then blocked 36/36
# — a dead gate. Re-measured grid on those 36 calls: CONF_FLOOR 0.30 +
# MARGIN_BAR 0.20 restores the pre-v4.7 flow rate (~14%), not looser. Same
# lesson as v4.1: never keep a threshold calibrated on a different decision-
# metric distribution. Containment unchanged (5/trade cap, worst -bash.09).
# v4.9 (10-18) RE-BASELINED AGAIN ON THE ENRICHED DISTRIBUTION: v4.8's 0.30/0.20
# was set from the FIRST 36 post-enrichment calls (a chop hour) and yielded 10
# order attempts / 2 fills / 3 closed trades in 60h = 0.05 trades/day — the n>=30
# review could never accrue. Measured over 702 calls / 115 picks (pick conf avg
# 0.28, max 0.55; model chooses no_trade 88%): grid says CF 0.20 + MB 0.15 gives
# ~6% flow (~8-9 attempts/day), 4x the pre-v4.7 rate at 1/5 the stake. This is
# distribution re-calibration, not outcome-mining: NO closed-trade outcomes were
# used to pick the thresholds. Calibration count restarts at v4.9.
CONF_FLOOR = 0.20
CONF_BAR = 0.50
MARGIN_BAR = 0.15   # v4.9: re-baselined with CONF_FLOOR (see above)
NOUL_BAR = 0.45
FILL_WAIT_S = 25
TIME_STOP_MIN = 30
STALE_EXIT_MIN = 60
COOLDOWN_MIN = 20
WIN_COOLDOWN_MIN = 10
DAILY_LOSS_PCT = 8.0           # USDC-equity breaker -> STOP_PERP (auto-clears UTC roll)

_INSTR = {}

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def logrec(rec):
    rec["ts"] = now_iso()
    rec["engine"] = "perp"
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")

def halted():
    return os.path.exists(J.STOP) or os.path.exists(STOP_PERP)

# ---------------- instruments ----------------

def perp_instruments():
    """PRIVATE /account/instruments FUTURES -> X-Perps our account may trade."""
    rows = J.api("GET", "/api/v5/account/instruments?instType=FUTURES")
    out = []
    for x in rows or []:
        iid = x.get("instId", "")
        if "_XPERP" not in iid:
            continue
        base = iid.split("-")[0]
        if base not in CRYPTO_BASES:
            continue
        out.append(x)
    return out

def instr(inst):
    if inst not in _INSTR:
        d = J.api("GET", f"/api/v5/public/instruments?instType=FUTURES&instId={inst}")
        _INSTR[inst] = d[0]
    return _INSTR[inst]

def rstep(x, step, down=True):
    return J.round_step(x, step, down)

def bracket_for(spread_pct):
    cost = spread_pct + PERP_MAKER_PCT + PERP_TAKER_PCT
    tp = min(max(cost * EDGE_MULT, MIN_TARGET_PCT), MAX_TARGET_PCT)
    sl = min(max(cost * SL_COST_MULT, SL_FLOOR_PCT), SL_CAP_PCT)
    return {"cost_pct": round(cost, 3), "tp_pct": round(tp, 3), "sl_pct": round(sl, 3),
            "trail_arm_pct": round(tp * ARM_TP_RATIO, 3),
            "trail_giveback_pct": round(tp * GB_TP_RATIO, 3),
            "trail_floor_pct": round(tp * FLOOR_TP_RATIO, 3)}

def usdc_equity():
    b = J.balances().get("USDC") or {}
    return float(b.get("cash", 0)) + float(b.get("frozen", 0)) or float(b.get("avail", 0))

# ---------------- v4.7 decision-context enrichment ----------------
# WHY (10-15, CTO decision after n=26 review): the perp Jev state was THINNER
# than the retired spot state. Measured: BTC-aligned entries +0.07%/trade
# (6W/12L) vs against-BTC entries -0.17%/trade (3W/16L), and the engine
# re-entered NEAR 12x overnight into the same -0.3% stop with no memory of it.
# Fixes: btc_regime, own recent trade results, news catalysts, macro calendar,
# rubik order-flow per pair. NOT a threshold change — CONF_FLOOR/gates untouched.
# Calibration counter for the n>=30 review restarts at this version.

BTC_PROXY = "BTC-USDT"          # spot proxy; BTC XPERP ids rotate with expiry

def btc_regime():
    """Market regime the alts are beta'd to. Public candles, no auth."""
    try:
        c5 = J.candles(BTC_PROXY, "5m", 7)
        c1h = J.candles(BTC_PROXY, "1H", 5)
        last = c5[-1]["c"]
        chg30 = round((last / c5[0]["c"] - 1) * 100, 3) if c5 and c5[0]["c"] else None
        chg4h = round((last / c1h[0]["c"] - 1) * 100, 2) if c1h and c1h[0]["c"] else None
        out = {}
        if chg30 is not None:
            out["btc_chg_30m_pct"] = chg30
        if chg4h is not None:
            out["btc_chg_4h_pct"] = chg4h
        return out or None
    except Exception:
        return None

def recent_trade_memory(n=8):
    """The engine's own last exits, so Jev stops re-entering a losing setup.
    Source: risk_state exits map (inst:side -> ts, upc), newest first."""
    try:
        r = load_risk()
        rows = []
        for k, v in (r.get("exits") or {}).items():
            inst, _, side = k.partition(":")
            ts = datetime.datetime.fromisoformat(v["ts"])
            ago = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60
            rows.append({"inst": inst.split("-")[0], "side": side,
                         "upc_pct": v.get("upc"), "ago_min": round(ago)})
        rows.sort(key=lambda x: x["ago_min"])
        return rows[:n] or None
    except Exception:
        return None

def news_catalysts():
    """Keyless RSS headlines; refresh the file if older than 35 min
    (the retired spot cycle used to keep it fresh)."""
    try:
        p = os.path.join(HERE, "catalyst.json")
        d = json.load(open(p))
        upd = datetime.datetime.fromisoformat(d.get("updated", "2000-01-01T00:00+00:00"))
        age_min = (datetime.datetime.now(datetime.timezone.utc) - upd).total_seconds() / 60
        if age_min > 35:
            subprocess.run([sys.executable, os.path.join(HERE, "news_feed.py")],
                           timeout=60, capture_output=True)
            d = json.load(open(p))
        return d.get("catalysts", [])[:5] or None
    except Exception:
        return None

def macro_calendar():
    """Upcoming 24h imp>=2 events + last-3h surprises (INVERTED before/after
    params — see okx-trading skill; copied from the spot cycle)."""
    try:
        now_ms = int(time.time() * 1000)
        H = 3600000
        out = {}
        cal = J.api("GET", f"/api/v5/public/economic-calendar?before={now_ms}&after={now_ms + 24 * H}&limit=10")
        ev = []
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
            out["macro_events_24h"] = ev[:5]
        cal = J.api("GET", f"/api/v5/public/economic-calendar?after={now_ms}&before={now_ms - 3 * H}&limit=30")
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
            out["macro_released_3h_surprises"] = rel[:4]
        return out or None
    except Exception:
        return None

def funding_rate(inst):
    try:
        d = J.api("GET", f"/api/v5/public/funding-rate?instId={inst}", private=False)[0]
        return round(float(d["fundingRate"]) * 10000, 3)  # bps
    except Exception:
        return None

# ---------------- universe scan ----------------

def select_universe(force=False):
    """Rank tradeable crypto X-Perps by 24h range; cache 10 min in perp_universe.json."""
    uf = os.path.join(HERE, "perp_universe.json")
    now = time.time()
    if not force and os.path.exists(uf):
        try:
            d = json.load(open(uf))
            if now - d.get("scanned", 0) < 600 and d.get("pairs"):
                return d["pairs"]
        except Exception:
            pass
    perps = perp_instruments()
    cands = []
    for ins in perps:
        iid = ins["instId"]
        if iid.split("-")[0] in UNIVERSE_EXCLUDE:
            continue
        try:
            t = J.ticker(iid)
            last = float(t["last"]); open24 = float(t.get("open24h") or last)
            hi = float(t.get("high24h") or last); lo = float(t.get("low24h") or last)
            bid = float(t.get("bidPx") or 0); ask = float(t.get("askPx") or 0)
            volq = float(t.get("volCcy24h") or 0)  # quote (USD) turnover
            if last <= 0 or bid <= 0 or ask <= 0:
                continue
            spread = (ask - bid) / ask * 100
            day_range = (hi - lo) / open24 * 100
            if spread > MAX_SPREAD_PCT or day_range < MIN_DAY_RANGE_PCT or volq < MIN_VOL24H_USD:
                continue
            b = J.api("GET", f"/api/v5/market/books?instId={iid}&sz=5", private=False)[0]
            depth = sum(float(l[0]) * float(l[1]) for l in b.get("bids", [])[:5])
            if depth < MIN_TOP5_DEPTH_USD:
                continue
            cands.append({"inst": iid, "base": iid.split("-")[0], "day_range_pct": round(day_range, 2),
                          "chg24h_pct": round((last / open24 - 1) * 100, 2),
                          "vol24h_usd": round(volq), "spread_pct": round(spread, 4),
                          "top5_depth_usd": round(depth), "bracket": bracket_for(spread),
                          "funding_bps": funding_rate(iid)})
        except Exception as e:
            logrec({"event": "perp_scan_err", "inst": iid, "err": str(e)[:120]})
    cands.sort(key=lambda c: (-c["day_range_pct"], c["spread_pct"]))
    sel = cands[:MAX_JEV_PAIRS]
    pairs = [c["inst"] for c in sel]
    try:
        json.dump({"scanned": now, "pairs": pairs, "selected": sel},
                  open(uf, "w"), separators=(",", ":"))
    except Exception:
        pass
    logrec({"event": "perp_universe", "n_perps": len(perps), "n_cands": len(cands),
            "selected": pairs})
    return pairs

# ---------------- features ----------------

def pair_features(inst):
    t = J.ticker(inst)
    bid = float(t["bidPx"]); ask = float(t["askPx"]); last = float(t["last"])
    cs = J.candles(inst, "5m", 7)          # 6 bars + current
    closes = [c["c"] for c in cs]
    hi30 = max(c["h"] for c in cs[-7:]); lo30 = min(c["l"] for c in cs[-7:])
    vol30 = (hi30 - lo30) / last * 100 if last else 0
    chg30 = (last / closes[-7] - 1) * 100 if len(closes) >= 7 and closes[-7] else 0
    bars_up = sum(1 for i in range(-6, 0) if closes[i] > closes[i - 1])
    rpos = (last - lo30) / (hi30 - lo30) * 100 if hi30 > lo30 else 50
    # daily trend context (X-Perps have full candle history)
    try:
        d1 = J.candles(inst, "1H", 25)
        chg4h = (last / d1[-5]["c"] - 1) * 100 if len(d1) >= 5 else None
        d1d = J.candles(inst, "1D", 8)
        chg7d = (last / d1d[0]["c"] - 1) * 100 if len(d1d) >= 7 else None
    except Exception:
        chg4h = chg7d = None
    return {"bid": bid, "ask": ask, "last": last,
            "spread_pct": round((ask - bid) / ask * 100, 4),
            "chg_30min_pct": round(chg30, 3), "volatility_30min_pct": round(vol30, 3),
            "range30_pos_pct": round(rpos, 1), "bars_up_of_6": bars_up,
            "chg_4h_pct": round(chg4h, 2) if chg4h is not None else None,
            "chg_7d_pct": round(chg7d, 2) if chg7d is not None else None}

# ---------------- risk state ----------------

def load_risk():
    try:
        return json.load(open(RISK))
    except Exception:
        return {}

def save_risk(r):
    json.dump(r, open(RISK, "w"))

def check_breaker():
    r = load_risk()
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if r.get("day") != day:
        if os.path.exists(STOP_PERP):
            try:
                if open(STOP_PERP).read().startswith("perp daily breaker"):
                    os.remove(STOP_PERP)
                    logrec({"event": "perp_day_roll", "note": "breaker STOP auto-cleared"})
            except Exception:
                pass
        eq = usdc_equity()
        r = {"day": day, "day_equity_start": round(eq, 4), "breaked": False,
             "exits": r.get("exits", {})}
        save_risk(r)
        return False
    if r.get("breaked"):
        return True
    start = r.get("day_equity_start") or 0
    eq = usdc_equity()
    if start > 0:
        pnl_pct = (eq - start) / start * 100
        if pnl_pct <= -DAILY_LOSS_PCT:
            r["breaked"] = True
            save_risk(r)
            open(STOP_PERP, "w").write(f"perp daily breaker {day}: {pnl_pct:.2f}%\n")
            logrec({"event": "perp_daily_breaker", "pnl_pct": round(pnl_pct, 2)})
            return True
    return False

def in_cooldown(inst, side):
    r = load_risk()
    e = (r.get("exits") or {}).get(f"{inst}:{side}")
    if not e:
        return 0
    age = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.datetime.fromisoformat(e["ts"])).total_seconds() / 60
    lim = WIN_COOLDOWN_MIN if e.get("upc", 0) > 0 else COOLDOWN_MIN
    return max(0, round(lim - age))

def mark_exit(inst, side, upc):
    r = load_risk()
    r.setdefault("exits", {})[f"{inst}:{side}"] = {"ts": now_iso(), "upc": round(upc, 2)}
    save_risk(r)

# ---------------- meta ----------------

def load_meta():
    try:
        return json.load(open(META))
    except Exception:
        return {}

def save_meta(m):
    json.dump(m, open(META, "w"))

# ---------------- orders ----------------

def set_leverage(inst):
    try:
        J.api("POST", "/api/v5/account/set-leverage",
              {"instId": inst, "lever": PERP_LEV, "mgnMode": "isolated"})
    except Exception as e:
        logrec({"event": "perp_leverage_err", "inst": inst, "err": str(e)[:150]})
        return False
    return True

def perp_position(inst):
    d = J.api("GET", f"/api/v5/account/positions?instType=FUTURES&instId={inst}")
    for p in d or []:
        if float(p.get("pos") or 0) != 0:
            return p
    return None

def open_perp(inst, side, contracts, px, bk, dry=False):
    """post_only entry (maker 0.02%) + attached OCO bracket. Unfilled after
    FILL_WAIT_S = cancel, no trade (v3.8 lesson: never chase as taker)."""
    ins = instr(inst)
    tick = float(ins["tickSz"]); lot = float(ins["lotSz"])
    sz = str(rstep(contracts, lot, down=False))
    entry_side = "buy" if side == "long" else "sell"
    tp_mult = 1 if side == "long" else -1
    algo = {"ordType": "oco", "sz": sz,
            "tpTriggerPx": str(rstep(px * (1 + tp_mult * bk["tp_pct"] / 100), tick, down=(side == "short"))),
            "tpOrdPx": "-1",
            "slTriggerPx": str(rstep(px * (1 - tp_mult * bk["sl_pct"] / 100), tick, down=(side == "long"))),
            "slOrdPx": "-1"}
    body = {"instId": inst, "tdMode": "isolated", "side": entry_side, "ordType": "post_only",
            "sz": sz, "px": str(rstep(px, tick, down=(side == "long"))),
            "attachAlgoOrds": [algo]}
    if dry:
        logrec({"event": "perp_dry_open", "inst": inst, "side": side, "sz": sz,
                "px": str(px), "algo": algo})
        return {"dry": True, "ordId": "dry", "state": "filled"}, None
    if not set_leverage(inst):
        return None, "leverage set failed"
    d = J.api("POST", "/api/v5/trade/order", body)
    o = d[0]
    if o.get("sCode") not in ("0", None):
        return None, f"{o.get('sCode')}: {o.get('sMsg')}"
    od = J.wait_fill(inst, o["ordId"], FILL_WAIT_S)
    st = od.get("state")
    if st != "filled":
        acc = float(od.get("accFillSz") or 0)
        if st in ("live", "partially_filled"):
            J.cancel_order(inst, o["ordId"])
        if acc >= float(ins.get("minSz") or 1):
            od = J.order_detail(inst, o["ordId"])
            od["entry_type"] = "maker_partial"
            return od, "partial"
        # v4.9 IOC-TAKER FALLBACK: maker-only was the v3.8 SPOT lesson (0.2% taker
        # made chasing EV-negative). Perp taker is 0.05% (rt 0.10% vs 0.07% maker):
        # 8 of 10 v4.8 attempts died unfilled = signals wasted. One IOC at the
        # reference px, bracket re-anchored to the ask (worst case = market px),
        # so the OCO protects the REAL fill level. If the IOC doesn't fill, the
        # tape ran away -> let it go, no trade (same as before, logged).
        try:
            if not dry:
                t = J.ticker(inst)
                ref_s = t.get("askPx") if entry_side == "buy" else t.get("bidPx")
                ref = float(ref_s or 0)
                if ref <= 0:
                    raise ValueError("IOC ref px missing from ticker")
                tick_r = float(ins["tickSz"])
                algo2 = {k: v for k, v in algo.items()}
                algo2["tpTriggerPx"] = str(rstep(ref * (1 + tp_mult * bk["tp_pct"] / 100), tick_r, down=(side == "short")))
                algo2["slTriggerPx"] = str(rstep(ref * (1 - tp_mult * bk["sl_pct"] / 100), tick_r, down=(side == "long")))
                body2 = {k: v for k, v in body.items()}
                body2["ordType"] = "ioc"
                body2["px"] = str(rstep(ref, tick_r, down=(entry_side == "buy")))
                body2["attachAlgoOrds"] = [algo2]
                d2 = J.api("POST", "/api/v5/trade/order", body2)
                o2 = d2[0] if d2 else {}
                if o2.get("sCode") in ("0", None):
                    od2 = J.wait_fill(inst, o2["ordId"], 10)
                    st2 = od2.get("state")
                    acc2 = float(od2.get("accFillSz") or 0)
                    if st2 == "filled" or acc2 >= float(ins.get("minSz") or 1):
                        if st2 in ("live", "partially_filled"):
                            J.cancel_order(inst, o2["ordId"])
                        od2 = J.order_detail(inst, o2["ordId"])
                        od2["entry_type"] = "taker_ioc"
                        logrec({"event": "perp_entry_taker_fallback", "inst": inst,
                                "side": side, "ref": ref})
                        return od2, ("partial" if st2 == "partially_filled" else None)
                    logrec({"event": "perp_entry_ioc_unfilled", "inst": inst, "side": side})
                else:
                    logrec({"event": "perp_entry_ioc_err", "inst": inst,
                            "err": f"{o2.get('sCode')}: {o2.get('sMsg')}"[:120]})
        except Exception as e:
            logrec({"event": "perp_entry_ioc_err", "inst": inst, "err": str(e)[:120]})
        od["entry_type"] = "maker_unfilled"
        return od, "unfilled"
    od["entry_type"] = "maker"
    return od, None

def close_perp_market(inst, side, contracts):
    """reduceOnly market close. Cancel server algos FIRST (they hold margin)."""
    try:
        algos = J.pending_algos(inst)
        ids = [(a["algoId"], inst) for a in algos]
        if ids:
            J.cancel_algos(ids)
    except Exception as e:
        logrec({"event": "perp_cancel_algos_err", "inst": inst, "err": str(e)[:120]})
    ins = instr(inst)
    close_side = "sell" if side == "long" else "buy"
    body = {"instId": inst, "tdMode": "isolated", "side": close_side, "ordType": "market",
            "sz": str(contracts), "reduceOnly": "true"}
    if DRY:
        logrec({"event": "perp_dry_close", "inst": inst, "side": side, "sz": str(contracts)})
        return None
    d = J.api("POST", "/api/v5/trade/order", body)
    o = d[0] if d else {}
    oid = o.get("ordId")
    # The POST response has no avgPx/fee — a market fill lands async, so read the
    # order detail (retry briefly) to capture the REALIZED exit fee + fill price.
    # Without this the close log records fee=None and the terminal's perp P&L
    # silently omits the exit fee (overstates returns) — same class of bug as the
    # -32% sum-of-stake-% error. Fee on FUTURES is quoted in the settle ccy (USDC).
    od = None
    if oid:
        for _ in range(4):
            try:
                od = J.order_detail(inst, oid)
                if od.get("state") == "filled" or od.get("fee"):
                    break
            except Exception:
                pass
            time.sleep(1)
        od = od or (J.order_detail(inst, oid) if oid else None)
    return od or o

# ---------------- manage ----------------

def manage():
    actions = []
    meta = load_meta()
    if not meta:
        check_breaker()
        return actions
    for inst, m in list(meta.items()):
        try:
            t = J.ticker(inst)
            px = float(t["last"])
            side = m["side"]
            sgn = 1 if side == "long" else -1
            entry = float(m["entry_ref"])
            upc = sgn * (px - entry) / entry * 100
            hwm = max(float(m.get("hwm_pct") or 0), upc)
            if abs(hwm - float(m.get("hwm_pct") or 0)) > 1e-9:
                m["hwm_pct"] = round(hwm, 3)
                save_meta(meta)
            bk = m["bracket"]
            arm = bk["trail_arm_pct"]; gb = bk["trail_giveback_pct"]; floor = bk["trail_floor_pct"]
            opened = datetime.datetime.fromisoformat(m["opened_ts"])
            age_min = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 60
            why = None
            if upc <= -bk["sl_pct"]:
                why = f"perp stop-loss {upc:.2f}%"
            elif hwm >= arm and upc <= max(hwm - gb, floor):
                why = f"perp trail-exit {upc:.2f}% (hwm {hwm:.2f}%)"
            elif age_min > TIME_STOP_MIN and upc < 0:
                why = f"perp time-stop {age_min:.0f}m {upc:.2f}%"
            elif age_min > STALE_EXIT_MIN and upc < bk["tp_pct"] * 0.3:
                why = f"perp stale-exit {age_min:.0f}m {upc:.2f}%"
            # liquidation guard: at 1x isolated this should never bind, but if
            # leverage is ever raised the exchange liq price is the hard rail
            pos = perp_position(inst)
            liq = float(pos.get("liqPx") or 0) if pos else 0
            if liq > 0:
                liq_dist = abs(px - liq) / px * 100
                if liq_dist < LIQ_BUFFER_PCT:
                    why = f"perp liq-guard {liq_dist:.1f}% from liqPx"
            if pos is None and not DRY:
                # server OCO already closed us — find the close fill
                try:
                    opened_ms = int(opened.timestamp() * 1000)
                    fills = J.api("GET", f"/api/v5/trade/fills?instType=FUTURES&instId={inst}&limit=20")
                    close_side = "sell" if side == "long" else "buy"
                    f = next((x for x in fills if x.get("side") == close_side
                              and int(x["ts"]) >= opened_ms - 5000), None)
                    if f:
                        fpx = float(f["fillPx"])
                        upc = sgn * (fpx - entry) / entry * 100
                        logrec({"event": "perp_close_detected", "inst": inst, "side": side,
                                "upc": round(upc, 2), "fill_px": fpx,
                                "realized_fee": f.get("fee"),
                                "note": "server-side OCO exit"})
                        actions.append({"closed": inst, "side": side, "upc": round(upc, 2)})
                        mark_exit(inst, side, upc)
                    else:
                        logrec({"event": "perp_prune_no_fill", "inst": inst, "side": side})
                except Exception as e:
                    logrec({"event": "perp_prune_err", "inst": inst, "err": str(e)[:120]})
                meta.pop(inst, None)
                save_meta(meta)
                continue
            if not why:
                continue
            cod = close_perp_market(inst, side, float(m["contracts"]))
            # Prefer the REALIZED fill price + fee from the close order detail.
            # upc from the live ticker is only an estimate; the fill is truth.
            exit_fee = None
            exit_px = None
            if cod:
                exit_fee = cod.get("fee")
                try:
                    exit_px = float(cod.get("avgPx") or 0) or None
                except Exception:
                    exit_px = None
                if exit_px and entry:
                    upc = sgn * (exit_px - entry) / entry * 100
            logrec({"event": "perp_close", "inst": inst, "side": side, "reason": why,
                    "upc": round(upc, 2), "age_min": round(age_min, 1),
                    "fill_px": exit_px, "realized_fee": exit_fee,
                    "notional_usd": m.get("notional_usd"),
                    "ordId": (cod or {}).get("ordId")})
            actions.append({"closed": inst, "side": side, "reason": why, "upc": round(upc, 2)})
            mark_exit(inst, side, upc)
            meta.pop(inst, None)
            save_meta(meta)
        except Exception as e:
            logrec({"event": "perp_manage_err", "inst": inst, "err": str(e)[:200]})
    check_breaker()
    return actions

# ---------------- decide (two-sided) ----------------

def decide():
    rec = {"acted": False, "dry": DRY}
    if halted():
        rec["halted"] = True
        logrec({"event": "perp_skip", "why": "halted (STOP or STOP_PERP)"})
        return rec
    if check_breaker():
        rec["halted"] = True
        return rec
    meta = load_meta()
    if len(meta) >= MAX_OPEN:
        rec["why"] = "max open perps"
        return rec
    pairs = select_universe()
    pairs = [p for p in pairs if p not in meta]
    if not pairs:
        rec["why"] = "no universe pairs"
        logrec({"event": "perp_no_trade", "why": rec["why"]})
        return rec
    tradeable = {}
    for inst in pairs:
        try:
            f = pair_features(inst)
        except Exception as e:
            logrec({"event": "perp_feat_err", "inst": inst, "err": str(e)[:120]})
            continue
        if f["spread_pct"] > MAX_SPREAD_PCT or f["volatility_30min_pct"] < MIN_VOL30_PCT:
            logrec({"event": "perp_gate_reject", "inst": inst,
                    "why": f"spread {f['spread_pct']} vol30 {f['volatility_30min_pct']}"})
            continue
        univ = json.load(open(os.path.join(HERE, "perp_universe.json")))
        selrow = next((c for c in univ.get("selected", []) if c["inst"] == inst), {})
        f["bracket"] = selrow.get("bracket") or bracket_for(f["spread_pct"])
        f["funding_bps"] = selrow.get("funding_bps")
        # v4.7: crowd positioning + taker flow (public rubik, same as spot had)
        try:
            ccy = inst.split("-")[0]
            rs = J.rubik_sentiment(ccy)
            for k in ("ls_ratio", "taker_buy_share"):
                if k in rs:
                    f[k] = rs[k]
        except Exception:
            pass
        tradeable[inst] = f
    if not tradeable:
        rec["why"] = "all pairs gated"
        logrec({"event": "perp_no_trade", "why": rec["why"]})
        return rec

    eq = usdc_equity()
    budget = min(eq * MAX_NOTIONAL_FRAC * float(PERP_LEV), MAX_NOTIONAL_USD)
    state = {
        "context": ("Two-way momentum scalping on OKX Europe X-Perps (MiFID-regulated "
                    "USD-margined expiry perps, isolated margin, 1x). You may go LONG or "
                    "SHORT each pair. Fees: maker 0.02% / taker 0.05% — round trip ~0.07-0.11%, "
                    "much cheaper than spot. Entry is maker-only; exits are market on trigger. "
                    "Per pair: spread_pct, chg_30min_pct, volatility_30min_pct, "
                    "range30_pos_pct (0=30m low, 100=30m high), bars_up_of_6, chg_4h_pct, "
                    "chg_7d_pct, funding_bps (positive = longs pay shorts; heavily positive "
                    "funding with a stretched range30_pos = crowded long, short-side edge; "
                    "negative funding = crowded short), ls_ratio (crowd long/short accounts, "
                    ">1 = crowd long) and taker_buy_share (>0.5 = net aggressive buying). "
                    "btc_chg_30m_pct / btc_chg_4h_pct: the whole alt complex is beta to BTC — "
                    "a LONG against a falling BTC or a SHORT against a rising BTC is a "
                    "low-quality setup regardless of the pair's own numbers; measured on our "
                    "own ledger, BTC-aligned entries earned +0.07%/trade vs -0.17%/trade "
                    "against BTC. our_recent_trades: OUR OWN last exits (instrument, side, "
                    "result, minutes ago) — if the same inst+side was stopped out recently, "
                    "re-entering it is repeating a setup that already failed; require a "
                    "materially changed picture, else no_trade. news_catalysts and "
                    "macro_events_24h / macro_released_3h_surprises: headlines and high-"
                    "impact macro prints (a surprise print in the last hours is the current "
                    "tape driver; a big event within 1-2h means headline risk over a 45-min "
                    "hold). Bracket: tp_pct/sl_pct/trail numbers "
                    "are the exit geometry your trade will be managed with. "
                    "LONG setups: pullback in uptrend (chg_7d>0, mild chg_30 dip, range30_pos "
                    "mid), real buying. SHORT setups: blow-off into 30m high with chg_7d flat/"
                    "negative, or crowded funding + stalling momentum. Symmetric rule: the "
                    "move must plausibly reach tp_pct within 30-90min given volatility_30min. "
                    "When unsure pick no_trade: a missed scalp costs 0, a chop costs ~0.3-0.6%."),
        "collateral_usdc": round(eq, 2),
        "notional_budget_usd": round(budget, 2),
        "open_positions": [{"inst": i, "side": m["side"], "entry_ref": m["entry_ref"]}
                           for i, m in meta.items()],
        "pairs": {i: {k: v for k, v in f.items() if k not in ("bid", "ask", "last")}
                  for i, f in tradeable.items()},
    }
    # v4.7 enrichment — each block optional; failures degrade to absent key
    for blk in (btc_regime(), macro_calendar()):
        if blk:
            state.update(blk)
    mem = recent_trade_memory()
    if mem:
        state["our_recent_trades"] = mem
    news = news_catalysts()
    if news:
        state["news_catalysts"] = news
    questions = {}
    crit = {}
    slug = {}
    for inst in sorted(tradeable):
        k = inst.split("-")[0].lower()
        bk = tradeable[inst]["bracket"]
        questions[f"p_up_{k}"] = {"type": "noul",
            "instructions": f"{inst.split('-')[0]} will RISE at least {bk['trail_arm_pct']}% within the next 45 minutes"}
        questions[f"p_dn_{k}"] = {"type": "noul",
            "instructions": f"{inst.split('-')[0]} will FALL at least {bk['trail_arm_pct']}% within the next 45 minutes"}
        slug[f"long_{k}"] = (inst, "long")
        slug[f"short_{k}"] = (inst, "short")
        crit[f"long_{k}"] = f"Open LONG {inst} now"
        crit[f"short_{k}"] = f"Open SHORT {inst} now"
    crit["no_trade"] = "No pair has a profitable setup in either direction; stay flat"
    questions["best_action"] = {"type": "choice",
        "instructions": "Which single action maximizes expected profit net of costs over the next 30-90 minutes",
        "criteria": crit}
    ans = J.jev(state, questions)
    a = ans["answers"]
    choice = a["best_action"]["choice"]
    conf = a["best_action"].get("confidence", 0)
    probs = a["best_action"].get("probabilities") or {}
    margin = round(probs.get(choice, 0) - probs.get("no_trade", 0), 3) if choice != "no_trade" else 0.0
    noul_map = {}
    for inst in tradeable:
        k = inst.split("-")[0].lower()
        noul_map[f"long_{k}"] = a.get(f"p_up_{k}", {}).get("noul")
        noul_map[f"short_{k}"] = a.get(f"p_dn_{k}", {}).get("noul")
    noul = noul_map.get(choice)
    usage = ans.get("usage") or {}
    # chosen direction/instrument for the terminal's two-way Jev display
    chosen = slug.get(choice) if choice != "no_trade" else None
    chosen_inst = chosen[0] if chosen else None
    chosen_side = chosen[1] if chosen else None
    # per-pair two-sided detail so the UI can show p(long)/p(short) + both nouls
    pairs_detail = []
    for inst in sorted(tradeable):
        k = inst.split("-")[0].lower()
        f = tradeable[inst]
        pairs_detail.append({
            "inst": inst, "base": inst.split("-")[0],
            "p_long": probs.get(f"long_{k}"), "p_short": probs.get(f"short_{k}"),
            "noul_up": noul_map.get(f"long_{k}"), "noul_dn": noul_map.get(f"short_{k}"),
            "chg_30min_pct": f.get("chg_30min_pct"), "chg_4h_pct": f.get("chg_4h_pct"),
            "chg_7d_pct": f.get("chg_7d_pct"), "range30_pos_pct": f.get("range30_pos_pct"),
            "spread_pct": f.get("spread_pct"), "funding_bps": f.get("funding_bps"),
            "bracket": f.get("bracket"),
        })
    logrec({"event": "perp_jev", "choice": choice, "conf": conf, "margin": margin,
            "noul": noul, "n_pairs": len(tradeable),
            "chosen_inst": chosen_inst, "chosen_side": chosen_side,
            "probabilities": probs, "pairs": pairs_detail,
            "collateral_usdc": round(eq, 2), "budget_usd": round(budget, 2),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens")})
    rec.update({"choice": choice, "conf": conf, "margin": margin, "noul": noul})
    edge_ok = (margin >= MARGIN_BAR or conf >= CONF_BAR) and conf >= CONF_FLOOR
    noul_ok = (noul or 0) >= NOUL_BAR
    if choice == "no_trade" or not edge_ok or not noul_ok:
        rec["why"] = f"conf {conf:.2f} margin {margin:.2f} noul {noul} — gated"
        logrec({"event": "perp_no_trade", "choice": choice, "why": rec["why"]})
        return rec
    inst, side = slug[choice]
    cd = in_cooldown(inst, side)
    if cd > 0:
        rec["why"] = f"cooldown {cd}m on {inst}:{side}"
        logrec({"event": "perp_cooldown_skip", "inst": inst, "side": side, "mins": cd})
        return rec
    f = tradeable[inst]
    bk = f["bracket"]
    ins = instr(inst)
    px = f["bid"] if side == "long" else f["ask"]   # maker entry rests at the touch
    ctval = float(ins["ctVal"])
    notional_per_ct = ctval * px
    contracts = int(budget // notional_per_ct)
    contracts = max(contracts, int(float(ins.get("minSz") or 1)))
    notional = contracts * notional_per_ct
    if notional > budget * 1.15 or notional > MAX_NOTIONAL_USD * 1.15:
        rec["why"] = f"min contract {notional:.1f}USD exceeds budget {budget:.1f}"
        logrec({"event": "perp_no_trade", "inst": inst, "why": rec["why"]})
        return rec
    od, err = open_perp(inst, side, contracts, px, bk, dry=DRY)
    if err == "unfilled":
        rec["why"] = "maker entry unfilled (no chase)"
        logrec({"event": "perp_entry_unfilled", "inst": inst, "side": side})
        return rec
    if od is None:
        rec["why"] = f"order failed: {err}"
        logrec({"event": "perp_open_err", "inst": inst, "err": str(err)[:150]})
        return rec
    if DRY:
        rec.update({"acted": True, "inst": inst, "side": side, "contracts": contracts,
                    "notional_usd": round(notional, 2)})
        return rec
    fill_px = float(od.get("avgPx") or 0) or px
    fee = od.get("fee")
    algo_ok = True
    try:
        algo_ok = bool(J.pending_algos(inst))
    except Exception:
        pass
    meta = load_meta()
    meta[inst] = {"opened_ts": now_iso(), "side": side, "contracts": float(contracts),
                  "entry_ref": fill_px, "notional_usd": round(notional, 2),
                  "jev_conf": conf, "jev_margin": margin, "jev_noul": noul,
                  "bracket": bk, "hwm_pct": 0.0, "algo_ok": algo_ok,
                  "entry_fee": fee, "entry_ordId": od.get("ordId")}
    save_meta(meta)
    logrec({"event": "perp_open", "inst": inst, "side": side, "contracts": contracts,
            "notional_usd": round(notional, 2), "fill_px": fill_px, "conf": conf,
            "margin": margin, "noul": noul, "algo_ok": algo_ok, "realized_fee": fee,
            "entry_type": od.get("entry_type"),
            "bracket": bk})
    rec.update({"acted": True, "inst": inst, "side": side, "contracts": contracts,
                "fill_px": fill_px, "notional_usd": round(notional, 2), "algo_ok": algo_ok})
    return rec

# ---------------- status ----------------

def status():
    meta = load_meta()
    eq = usdc_equity()
    # Whole-account truth, computed ONCE here so both the status CLI and the
    # terminal snapshot report the same numbers (the deck has no credentials and
    # can only read what this dict publishes). eur_float matters because the spot
    # book is retired — its EUR is a leftover float, not a live bankroll, and the
    # deck used to mistake the frozen spot log value for current cash.
    _bal = J.balances()
    _eur = float((_bal.get("EUR") or {}).get("avail") or 0)
    _tot_usd = round(sum(float(v.get("eq_usd") or 0) for v in _bal.values()), 2)
    out = {"engine": ENGINE_VERSION, "usdc_equity": round(eq, 2),
           "eur_float": round(_eur, 4), "account_total_usd": _tot_usd,
           "collateral_ccy": J.PERP_COLLATERAL_CCY,   # defined in okx_jev (imported as J), NOT here
           "halted": halted(), "dry": DRY,
           "positions": [], "algos_pending": 0}
    for inst, m in meta.items():
        row = dict(m); row["inst"] = inst
        try:
            t = J.ticker(inst)
            px = float(t["last"])
            sgn = 1 if m["side"] == "long" else -1
            row["last"] = px
            row["upc"] = round(sgn * (px - float(m["entry_ref"])) / float(m["entry_ref"]) * 100, 2)
        except Exception:
            pass
        try:
            p = perp_position(inst)
            if p:
                row["liq_px"] = p.get("liqPx")
                row["upl"] = p.get("upl")
                row["lever"] = p.get("lever")
        except Exception:
            pass
        out["positions"].append(row)
    try:
        out["algos_pending"] = len(J.pending_algos())
    except Exception:
        pass
    # snapshot for the observation terminal (container has no creds; reads this)
    try:
        # Publish the WHOLE account, not just this book's collateral — copied from
        # `out` so the CLI and the deck can never disagree, and so we make one
        # balances() call rather than two. Without the EUR float the deck read
        # cash from the retired spot engine's last logged jev event, frozen at the
        # pre-conversion 100.73 EUR, and narrated it as current account state.
        snap = {"engine": out["engine"], "usdc_equity": out["usdc_equity"],
                "eur_float": out["eur_float"], "account_total_usd": out["account_total_usd"],
                "collateral_ccy": out["collateral_ccy"],
                "halted": out["halted"], "dry": DRY,
                "n_positions": len(out["positions"]),
                "algos_pending": out["algos_pending"], "ts": now_iso(),
                "positions": out["positions"]}
        tmp = os.path.join(HERE, "perp_status.json.tmp")
        json.dump(snap, open(tmp, "w"), default=str, separators=(",", ":"))
        os.replace(tmp, os.path.join(HERE, "perp_status.json"))
    except Exception:
        pass
    return out

# ---------------- main ----------------

if __name__ == "__main__":
    mode = next((a for a in sys.argv[1:] if not a.startswith("--")), "status")
    if mode == "manage":
        r = {"lines": [f"closed {c['closed']} {c.get('side')} {c.get('reason','server')} {c.get('upc')}%"
                       for c in manage()]}
    elif mode == "decide":
        d = decide()
        r = {"lines": [("OPENED %s %s x%s @%s ($%s)" % (d["inst"], d["side"], d.get("contracts"),
                         d.get("fill_px"), d.get("notional_usd"))) if d.get("acted")
                        else f"no trade: {d.get('why') or d.get('choice')}"]}
    elif mode == "cycle":
        lines = []
        for c in manage():
            lines.append(f"CLOSED {c['closed']} {c.get('side')} — {c.get('reason','server OCO')} {c.get('upc')}%")
        if not halted():
            for _ in range(MAX_ENTRIES_PER_CYCLE):
                d = decide()
                if d.get("acted"):
                    lines.append(("DRY-" if DRY else "") +
                                 f"OPENED {d['inst']} {d['side'].upper()} x{d.get('contracts')} "
                                 f"@{d.get('fill_px')} ${d.get('notional_usd')} conf={d.get('conf',0):.2f}")
                    break
                lines.append(f"no trade: {d.get('why') or d.get('choice')}")
                break
        else:
            lines.append("HALTED (STOP/STOP_PERP)")
        s = status()
        lines.append(f"usdc={s['usdc_equity']} pos={len(s['positions'])} halted={s['halted']}")
        r = {"lines": lines}
    else:
        r = status()
    print(json.dumps(r, indent=1, default=str))
