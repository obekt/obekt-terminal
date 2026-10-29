#!/usr/bin/env python3
"""okx_mom.py — v5.0-mom: deterministic cross-sectional momentum engine for the
OKX EEA X-Perp book. Replaces the Jev LLM signal (kill-gated 10-25: 79 closes,
net -1.47 USDC, gross +0.03%/trade vs 0.086% cost) with the backtested XS
momentum signal: 14d lookback rank across the 30 most liquid XPERPs, long
top-2 / short bottom-2, 6h hold, 1.5% protective stop.

Backtest evidence (10-25, 25 coins x 100d hourly OHLC, taker 0.05%/leg,
intrabar stops w/ gap-through conservatism, OOS time split):
  XS L336 H24 K2 stop1.5: +0.857%/d net, OOS halves +0.440 / +1.285 (both >0)
  stop-robust: 1.0/1.5/2.0/3.0 all << ; stops IMPROVE the raw signal (+0.779).
Known biases (honest): universe = top-30 by volume AS OF 10-25 (selection
bias inflates the second half); funding excluded (positive-funding names make
the short leg a mild tailwind); 100d window. Conservative live expectation:
the OOS-first-half number (~+0.4%/d on deployed), not the full-sample one.

Reuses okx_perp execution machinery (open_perp post_only+IOC fallback, attached
OCO, close_perp_market with realized-fee capture, logrec -> perp_log.jsonl,
meta -> perp_positions.json, status snapshot -> perp_status.json) so the public
deck keeps working unchanged. NO Jev/LLM calls: fully deterministic, ~0 cost.

Modes: cycle (manage+rebalance+status), status, ranks (debug scan).
Kill-gate v5.2: at 30 rebalance cycles (~7.5d at H6) OR 10 days, compare live net
%/day vs the backtest recent-window expectation (H6 row: +1.0%/d last-50d, +2.1%/d
last-30d, full-window +0.60; OOS1 regime was -0.34 — hold-window sensitive).
If live <= 0 at the gate, close/replace the book (pre-authorized pattern).
"""
import json, os, sys, time, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import okx_jev as J          # api/ticker/instruments helpers (EEA key auth)
import okx_perp as P         # execution machinery + files + constants

ENGINE_VERSION = "v5.2-mom"
DRY = P.DRY                   # --dry-run passthrough (smoke tests only)

# ---- strategy parameters (from the 10-25 stop-aware backtest) -------------
MOM_LOOKBACK_H   = 336        # 14d rank lookback (L336 in hourly bars)
MOM_HOLD_MIN     = 360        # 6h hold, then full rebalance (H6) — v5.2 grid 10-26
MOM_K            = 2          # long top-2 / short bottom-2 (K2) = 4 slots
MOM_SL_PCT       = 1.5        # protective server-side stop (best OOS row)
MOM_TP_PCT       = 25.0       # far TP stub (25%): winners run to the 24h rebalance; 99% broke
MOM_UNIVERSE     = 30         # rank within the 30 most liquid XPERPs
MOM_MIN_VOL24H   = 3_000_000  # USD turnover floor (volCcy24h*last — UNIT FIX)
MOM_RANK_CACHE_S = 1800       # rank scan TTL (rebalance is 24h; 30m is ample)
RANK_CACHE_FILE  = os.path.join(HERE, "mom_ranks.json")

def log(rec):
    rec = dict(rec)
    rec.setdefault("strategy", "mom")
    rec.setdefault("dry", bool(DRY))
    P.logrec(rec)

# ---- universe + ranking ----------------------------------------------------

def scan_ranks(force=False):
    """Top-MOM_UNIVERSE XPERPs by 24h USD turnover; rank by 14d return.
    Uses 4H candles (84 bars = 336h) so one request per instrument covers the
    lookback; /market/candles rows are NEWEST-FIRST."""
    try:
        c = json.load(open(RANK_CACHE_FILE))
        if not force and time.time() - c.get("scanned", 0) < MOM_RANK_CACHE_S:
            return c
    except Exception:
        pass
    tickers = J.api("GET", "/api/v5/market/tickers?instType=FUTURES") or []
    rows = []
    for t in tickers:
        inst = t.get("instId", "")
        if "XPERP" not in inst:
            continue
        try:
            vol_usd = float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
        except Exception:
            continue
        if vol_usd >= MOM_MIN_VOL24H:
            rows.append((vol_usd, inst, float(t.get("last") or 0)))
    rows.sort(reverse=True)
    universe = rows[:MOM_UNIVERSE]
    need = MOM_LOOKBACK_H // 4 + 2          # 4H bars covering the lookback
    ranked = []
    for vol_usd, inst, last in universe:
        try:
            c4 = J.api("GET", f"/api/v5/market/candles?instId={inst}&bar=4H&limit=100") or []
            if len(c4) < need:
                continue                     # young listing — same exclusion as backtest
            newest = float(c4[0][4])         # close of the latest (possibly forming) bar
            oldest = float(c4[need - 1][4])  # close ~336h ago
            if oldest <= 0:
                continue
            ranked.append({"inst": inst, "base": inst.split("-")[0],
                           "chg_lookback_pct": round((newest / oldest - 1) * 100, 2),
                           "vol24h_usd": round(vol_usd), "last": last})
            time.sleep(0.06)
        except Exception as e:
            log({"event": "mom_scan_err", "inst": inst, "err": str(e)[:120]})
    ranked.sort(key=lambda r: -r["chg_lookback_pct"])
    out = {"scanned": time.time(), "n_universe": len(universe), "ranked": ranked,
           "longs": [r["inst"] for r in ranked[:MOM_K]],
           "shorts": [r["inst"] for r in ranked[-MOM_K:]] if len(ranked) >= 2 * MOM_K + 2 else []}
    if not DRY:
        tmp = RANK_CACHE_FILE + ".tmp"
        json.dump(out, open(tmp, "w"))
        os.replace(tmp, RANK_CACHE_FILE)
    log({"event": "mom_scan", "n_universe": out["n_universe"], "n_ranked": len(ranked),
         "longs": [(r["base"], r["chg_lookback_pct"]) for r in ranked[:MOM_K]],
         "shorts": [(r["base"], r["chg_lookback_pct"]) for r in ranked[-MOM_K:]],
         "top": [(r["base"], r["chg_lookback_pct"]) for r in ranked[:5]]})
    return out

# ---- open / close ----------------------------------------------------------

def _open_one(inst, side, ranks_row):
    """One $25-slot entry via the shared execution path (post_only 25s -> IOC
    fallback, attached OCO SL). Returns True on fill."""
    try:
        t = J.ticker(inst)
        px = float(t["last"])
        ins = P.instr(inst)
        ctval = float(ins["ctVal"])
        eq = P.usdc_equity()
        budget = min(eq * P.MAX_NOTIONAL_FRAC * float(P.PERP_LEV), P.MAX_NOTIONAL_USD)
        notional_per_ct = ctval * px
        # szfix 10-29: int() truncation dropped legs on expensive instruments
        # (SKHYNIX ~$1149/ct, lotSz 0.001 -> int(30//1149)=0 -> OKX 51000 sz
        # error). Size in lot steps, decimal-safe, honoring fractional minSz.
        lot = float(ins.get("lotSz") or 1)
        minsz = float(ins.get("minSz") or lot)
        contracts = P.rstep(budget / notional_per_ct, lot, down=True)
        if contracts < minsz:
            contracts = minsz
        notional = contracts * notional_per_ct
        if contracts <= 0 or notional > budget * 1.15 or notional > P.MAX_NOTIONAL_USD * 1.15:
            log({"event": "mom_skip", "inst": inst, "why": f"min size notional {notional:.1f} > budget {budget:.1f}"})
            return False
        bk = {"cost_pct": round(MOM_SL_PCT / 15, 3), "tp_pct": MOM_TP_PCT, "sl_pct": MOM_SL_PCT,
              "trail_arm_pct": 999.0, "trail_giveback_pct": 999.0, "trail_floor_pct": 999.0}
        od, err = P.open_perp(inst, side, contracts, px, bk, dry=DRY)
        if err == "unfilled" or od is None:
            log({"event": "mom_entry_unfilled", "inst": inst, "side": side, "err": str(err)[:120]})
            return False
        if DRY:
            log({"event": "perp_dry_open", "inst": inst, "side": side, "contracts": contracts,
                 "px": px, "notional_usd": round(notional, 2)})
            return True
        fill_px = float(od.get("avgPx") or 0) or px
        fee = od.get("fee")
        meta = P.load_meta()
        meta[inst] = {"opened_ts": P.now_iso(), "side": side, "contracts": float(contracts),
                      "entry_ref": fill_px, "notional_usd": round(notional, 2),
                      "strategy": "mom", "mom_rank_chg_pct": ranks_row.get("chg_lookback_pct"),
                      "jev_conf": None, "jev_margin": None, "jev_noul": None,
                      "bracket": bk, "hwm_pct": 0.0, "algo_ok": False,
                      "entry_fee": fee, "entry_ordId": od.get("ordId")}
        try:
            meta[inst]["algo_ok"] = bool(J.pending_algos(inst))
        except Exception:
            pass
        P.save_meta(meta)
        log({"event": "perp_open", "inst": inst, "side": side, "contracts": contracts,
             "notional_usd": round(notional, 2), "fill_px": fill_px, "conf": None,
             "margin": None, "noul": None, "algo_ok": meta[inst]["algo_ok"],
             "realized_fee": fee, "entry_type": od.get("entry_type"),
             "mom_rank_chg_pct": ranks_row.get("chg_lookback_pct"), "bracket": bk})
        return True
    except Exception as e:
        log({"event": "mom_open_err", "inst": inst, "side": side, "err": str(e)[:160]})
        return False

def _server_exit_sync(inst, m, meta):
    """Detect a server-side OCO stop fill (position gone) and log it — mirrors
    okx_perp.manage()'s close_detected branch, minus the scalp cooldowns."""
    try:
        opened = datetime.datetime.fromisoformat(m["opened_ts"])
        opened_ms = int(opened.timestamp() * 1000)
        fills = J.api("GET", f"/api/v5/trade/fills?instType=FUTURES&instId={inst}&limit=20")
        side = m["side"]
        close_side = "sell" if side == "long" else "buy"
        f = next((x for x in fills if x.get("side") == close_side
                  and int(x["ts"]) >= opened_ms - 5000), None)
        if f:
            fpx = float(f["fillPx"])
            entry = float(m["entry_ref"])
            sgn = 1 if side == "long" else -1
            upc = sgn * (fpx - entry) / entry * 100
            age_min = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 60
            log({"event": "perp_close_detected", "inst": inst, "side": side,
                 "upc": round(upc, 2), "age_min": round(age_min, 1), "fill_px": fpx,
                 "realized_fee": f.get("fee"), "notional_usd": m.get("notional_usd"),
                 "note": "server-side SL exit (mom book)"})
        else:
            log({"event": "perp_prune_no_fill", "inst": inst, "side": side})
    except Exception as e:
        log({"event": "mom_sync_err", "inst": inst, "err": str(e)[:140]})
    meta.pop(inst, None)
    P.save_meta(meta)

def manage_mom():
    """Sync server-side exits + poll-backup stop. No trail/time-stop/stale-exit:
    the ONLY exits in the backtest are the 1.5% stop and the 24h rebalance.
    The poll stop is the backup for a failed OCO attach (algo_ok=False) — the
    spot-book lesson: never assume the server-side bracket is live."""
    meta = P.load_meta()
    for inst, m in list(meta.items()):
        if m.get("strategy") != "mom":
            continue
        try:
            pos = P.perp_position(inst)
        except Exception:
            pos = None
        if pos is None:
            _server_exit_sync(inst, m, meta)
            continue
        # poll-backup protective stop (always on: double protection is free)
        try:
            px = float(J.ticker(inst)["last"])
            entry = float(m["entry_ref"])
            sgn = 1 if m["side"] == "long" else -1
            upc = sgn * (px - entry) / entry * 100
            if upc <= -MOM_SL_PCT:
                log({"event": "mom_poll_stop", "inst": inst, "side": m["side"],
                     "upc": round(upc, 2), "algo_ok": m.get("algo_ok")})
                close_all({inst: m}, f"mom poll-stop {upc:.2f}%")
        except Exception as e:
            log({"event": "mom_poll_err", "inst": inst, "err": str(e)[:140]})
    return P.load_meta()

MOM_STATE_FILE = os.path.join(HERE, "mom_state.json")

def _load_mom_state():
    try:
        return json.load(open(MOM_STATE_FILE))
    except Exception:
        return {}

def _save_mom_state(s):
    if not DRY:
        tmp = MOM_STATE_FILE + ".tmp"
        json.dump(s, open(tmp, "w"))
        os.replace(tmp, MOM_STATE_FILE)

def rebalance_due(meta):
    """6h-anchored rebalance (matches backtest semantics): due when the anchor
    is >= MOM_HOLD_MIN old — NOT merely when slots are free. If all legs stop
    out mid-hold the book stays FLAT until the next anchor (the backtest never
    re-enters mid-period)."""
    st = _load_mom_state()
    last = st.get("last_rebalance_ts")
    now = datetime.datetime.now(datetime.timezone.utc)
    if not last:
        return True, "initial deploy — open fresh book"
    age_min = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 60
    if age_min >= MOM_HOLD_MIN:
        return True, f"anchor expired ({age_min:.0f}m >= {MOM_HOLD_MIN}m)"
    return False, f"next rebalance in {(MOM_HOLD_MIN - age_min):.0f}m"

def close_all(meta, reason):
    for inst, m in list(meta.items()):
        if m.get("strategy") != "mom":
            continue
        od = P.close_perp_market(inst, m["side"], float(m["contracts"]))  # cancels OCO first
        exit_fee = (od or {}).get("fee")
        exit_px = None
        upc = 0.0
        try:
            exit_px = float((od or {}).get("avgPx") or 0) or None
        except Exception:
            exit_px = None
        entry = float(m["entry_ref"])
        sgn = 1 if m["side"] == "long" else -1
        if exit_px and entry:
            upc = sgn * (exit_px - entry) / entry * 100
        else:
            try:
                px = float(J.ticker(inst)["last"])
                upc = sgn * (px - entry) / entry * 100
                exit_px = px
            except Exception:
                pass
        opened = datetime.datetime.fromisoformat(m["opened_ts"])
        age_min = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds() / 60
        log({"event": "perp_close", "inst": inst, "side": m["side"], "reason": reason,
             "upc": round(upc, 2), "age_min": round(age_min, 1), "fill_px": exit_px,
             "realized_fee": exit_fee, "notional_usd": m.get("notional_usd"),
             "ordId": (od or {}).get("ordId")})
        meta.pop(inst, None)
        P.save_meta(meta)

def cycle():
    if P.halted():
        log({"event": "perp_skip", "why": "halted (STOP or STOP_PERP)"})
        return {"halted": True}
    P.check_breaker()
    if P.halted():
        return {"halted": True, "why": "breaker"}
    meta = manage_mom()
    due, why = rebalance_due({k: v for k, v in meta.items() if v.get("strategy") == "mom"})
    out = {"rebalance": False, "why": why, "opened": [], "skipped": []}
    if due:
        out["rebalance"] = True
        close_all(meta, f"mom rebalance ({why})")
        _save_mom_state({**_load_mom_state(), "last_rebalance_ts": P.now_iso()})
        ranks = scan_ranks()
        by_inst = {r["inst"]: r for r in ranks["ranked"]}
        if len(ranks["ranked"]) < 2 * MOM_K + 2:
            out["skipped"].append("universe too thin")
            log({"event": "mom_no_rebalance", "why": "universe too thin",
                 "n_ranked": len(ranks["ranked"])})
        else:
            for inst in ranks["longs"]:
                if _open_one(inst, "long", by_inst.get(inst, {})):
                    out["opened"].append(("long", inst))
            for inst in ranks["shorts"]:
                if _open_one(inst, "short", by_inst.get(inst, {})):
                    out["opened"].append(("short", inst))
    if not due:
        # heartbeat (30-min throttle): the deck's liveness strip reads the log's
        # last event; a silent book looks paused. Quiet != dead — say so.
        st = _load_mom_state()
        last_hb = st.get("last_heartbeat_ts")
        hb_age = 1e9 if not last_hb else (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.datetime.fromisoformat(last_hb)).total_seconds()
        if hb_age >= 1800:
            log({"event": "mom_heartbeat", "why": why,
                 "n_positions": len([1 for v in meta.values() if v.get("strategy") == "mom"])})
            _save_mom_state({**st, "last_heartbeat_ts": P.now_iso()})
    try:
        P.status()          # writes perp_status.json for the public deck
    except Exception as e:
        log({"event": "mom_status_err", "err": str(e)[:160]})
    return out

def main():
    mode = next((a for a in sys.argv[1:] if not a.startswith("--")), "cycle")
    if mode == "status":
        print(json.dumps(P.status(), indent=1, default=str))
    elif mode == "ranks":
        r = scan_ranks(force=True)
        print(json.dumps({"longs": r["longs"], "shorts": r["shorts"],
                          "top5": r["ranked"][:5], "bottom5": r["ranked"][-5:]}, indent=1))
    else:
        print(json.dumps(cycle(), indent=1, default=str))

if __name__ == "__main__":
    main()
