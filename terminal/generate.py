#!/usr/bin/env python3
"""
OBEKT TERMINAL — data generator.

Reads the LIVE trading system state (~/.hermes/workspace/okx):
  okx_jev_log.jsonl, universe.json, social.json, catalyst.json,
  risk_state.json, positions.json
+ keyless OKX public tickers (eea.okx.com)
+ a local/hosted LLM (OpenAI-compatible) for desk commentary, cached

Writes site/data.json and site/data.js (window.TT_DATA = ...).
Read-only: never touches the trading system, no orders, no credentials.

Usage:  python3 generate.py        (one-shot build)
        served continuously by serve.py
"""
import json
import os
import re
import sys
import time
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

# Engine workspace: the directory holding okx_jev_log.jsonl, universe.json,
# social.json, catalyst.json, risk_state.json, positions.json. Override with env.
BASE = os.path.expanduser(os.environ.get("OKX_TERMINAL_BASE", "~/.hermes/workspace/okx"))
SITE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "okx_jev_log.jsonl")

# Desk-commentary LLM: ANY OpenAI-compatible chat-completions endpoint works —
# a local model (llama.cpp / LM Studio / vLLM), or a cheap hosted relay.
LLM_URL = os.environ.get("TERMINAL_LLM_URL", "http://localhost:1234/v1/chat/completions")
LLM_MODEL = os.environ.get("TERMINAL_LLM_MODEL", "qwen3-flash")
LLM_KEY_ENV = os.environ.get("TERMINAL_LLM_KEY_ENV", "TERMINAL_LLM_API_KEY")
COMMENTARY_MIN_INTERVAL_S = 300      # never call LLM more often than this
COMMENTARY_MAX_AGE_S = 900           # force refresh if inputs changed and this old
COMMENTARY_CACHE = os.path.join(SITE, "commentary_cache.json")
TICKER_CACHE = os.path.join(SITE, "ticker_cache.json")

MARQUEE_INSTS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "NEAR-EUR",
                 "SUI-EUR", "PUMP-EUR", "LINK-EUR", "AAVE-EUR", "AVAX-EUR"]

CONTACT_LINE = os.environ.get(
    "TERMINAL_CONTACT_LINE",
    "OBEKT AUTONOMOUS TRADING PLATFORM  //  BUILT BY OBEKT AI WORKS -> https://obekt.com")

ENGINE = {
    "version": "v3.5",
    "venue": "OKX EUROPE (EEA) SPOT",
    "cycle_s": 300,
    "quote": "EUR",
    "decision_model": "jev-latest (TypeSafe System One)",
    "desk_model": LLM_MODEL,
    "mode": "LIVE CAPITAL — FULLY AUTONOMOUS",
}


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def read_log():
    events = []
    try:
        with open(LOG_PATH, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return events


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def build_round_trips(events):
    """Pair open -> close/close_detected per instrument, chronologically."""
    open_by_inst = {}
    trips = []
    for e in events:
        ev = e.get("event")
        inst = e.get("inst")
        if ev == "open":
            open_by_inst.setdefault(inst, []).append(e)
        elif ev in ("close", "close_detected"):
            stack = open_by_inst.get(inst)
            o = stack.pop() if stack else None
            upc = e.get("upc")
            if upc is None:
                continue
            stake = (o or {}).get("stake") or 0
            fee_close = abs(float(e.get("realized_fee") or 0))
            # entry fee estimate from entry type (maker 0.1% / taker 0.2%)
            et = (o or {}).get("entry_type", "")
            fee_open = stake * (0.002 if "taker" in str(et) else 0.001)
            gross = stake * upc / 100.0
            fees = fee_open + fee_close
            ts_close = parse_ts(e.get("ts", ""))
            ts_open = parse_ts((o or {}).get("ts", "")) if o else None
            age_min = None
            if ts_close and ts_open:
                age_min = round((ts_close - ts_open).total_seconds() / 60, 1)
            anchor_ts = (o or {}).get("ts") or e.get("ts") or ""
            tid = "%s-%s" % (inst, re.sub(r"[^0-9]", "", anchor_ts)[:14])
            trips.append({
                "tid": tid,
                "inst": inst,
                "open_ts": (o or {}).get("ts"),
                "close_ts": e.get("ts"),
                "entry_px": (o or {}).get("fill_px"),
                "exit_px": e.get("fill_px"),
                "stake_eur": round(stake, 2),
                "upc": upc,
                "gross_eur": round(gross, 2),
                "fees_eur": round(fees, 3),
                "net_eur": round(gross - fees, 2),
                "age_min": age_min if age_min is not None else e.get("age_min"),
                "reason": e.get("reason") or e.get("note") or "exit",
                "exit_kind": "server-oco" if ev == "close_detected" else "managed",
                "conf": (o or {}).get("conf"),
                "margin": (o or {}).get("margin"),
                "noul": (o or {}).get("noul"),
                "entry_type": et or None,
                "algo_ok": (o or {}).get("algo_ok"),
                "bracket": (o or {}).get("bracket"),
            })
    return trips


def build_stats(trips, events):
    wins = [t for t in trips if t["net_eur"] > 0]
    losses = [t for t in trips if t["net_eur"] <= 0]
    gross_win = sum(t["net_eur"] for t in wins)
    gross_loss = abs(sum(t["net_eur"] for t in losses))
    by_day = {}
    for t in trips:
        if not t.get("close_ts"):
            continue
        d = t["close_ts"][:10]
        b = by_day.setdefault(d, {"date": d, "trades": 0, "wins": 0, "net_eur": 0.0})
        b["trades"] += 1
        b["net_eur"] = round(b["net_eur"] + t["net_eur"], 2)
        if t["net_eur"] > 0:
            b["wins"] += 1
    days = sorted(by_day.values(), key=lambda x: x["date"])[-14:]
    best = max(trips, key=lambda t: t["net_eur"], default=None)
    worst = min(trips, key=lambda t: t["net_eur"], default=None)

    # cumulative PnL series (chronological) for sparkline
    chrono = sorted([t for t in trips if t.get("close_ts")], key=lambda t: t["close_ts"])
    cum, run = [], 0.0
    for t in chrono:
        run = round(run + t["net_eur"], 2)
        cum.append(run)

    # PnL per instrument
    per_inst = {}
    for t in trips:
        b = per_inst.setdefault(t["inst"], {"inst": t["inst"], "trades": 0, "wins": 0, "net_eur": 0.0})
        b["trades"] += 1
        b["net_eur"] = round(b["net_eur"] + t["net_eur"], 2)
        if t["net_eur"] > 0:
            b["wins"] += 1
    per_inst = sorted(per_inst.values(), key=lambda x: -x["net_eur"])

    # hold-time histogram
    buckets = [(0, 15), (15, 30), (30, 45), (45, 60), (60, 90), (90, 10**6)]
    hold = []
    for lo, hi in buckets:
        n = sum(1 for t in trips if t.get("age_min") is not None and lo <= t["age_min"] < hi)
        label = "%dm-%s" % (lo, ("%dm" % hi) if hi < 10**5 else "+")
        hold.append({"label": label, "n": n})

    # streaks (chronological)
    cur_kind, cur_len, max_w, max_l, run_kind, run_len = None, 0, 0, 0, None, 0
    for t in chrono:
        k = "W" if t["net_eur"] > 0 else "L"
        if k == run_kind:
            run_len += 1
        else:
            run_kind, run_len = k, 1
        if k == "W":
            max_w = max(max_w, run_len)
        else:
            max_l = max(max_l, run_len)
    cur_kind, cur_len = run_kind, run_len

    # PnL by UTC hour of entry
    hours = {}
    for t in chrono:
        h = (t.get("open_ts") or t.get("close_ts") or "")[11:13]
        if not h.isdigit():
            continue
        b = hours.setdefault(int(h), {"h": int(h), "n": 0, "net_eur": 0.0})
        b["n"] += 1
        b["net_eur"] = round(b["net_eur"] + t["net_eur"], 2)
    by_hour = [hours.get(i, {"h": i, "n": 0, "net_eur": 0.0}) for i in range(24)]

    # exit-reason breakdown
    def exit_cat(t):
        r = (t.get("reason") or "").lower()
        if "trail" in r:
            return "TRAIL EXIT"
        if "time-stop" in r:
            return "TIME STOP"
        if "stale" in r:
            return "STALE EXIT"
        if "server-side" in r or t.get("exit_kind") == "server-oco":
            return "OCO TAKE-PROFIT" if (t.get("upc") or 0) >= 0 else "OCO STOP-LOSS"
        return "OTHER"
    exits = {}
    for t in chrono:
        c = exit_cat(t)
        b = exits.setdefault(c, {"cat": c, "n": 0, "net_eur": 0.0, "wins": 0})
        b["n"] += 1
        b["net_eur"] = round(b["net_eur"] + t["net_eur"], 2)
        if t["net_eur"] > 0:
            b["wins"] += 1
    by_exit = sorted(exits.values(), key=lambda x: -x["n"])

    def _slim(t):
        return None if not t else {k: t[k] for k in
                                   ("inst", "close_ts", "upc", "net_eur", "reason", "age_min")}

    return {
        "trades_all": len(trips),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trips), 1) if trips else None,
        "net_all_eur": round(sum(t["net_eur"] for t in trips), 2),
        "fees_all_eur": round(sum(t["fees_eur"] for t in trips), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_eur": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_eur": round(-gross_loss / len(losses), 2) if losses else None,
        "avg_hold_min": round(sum(t["age_min"] for t in chrono if t.get("age_min")) /
                              max(1, sum(1 for t in chrono if t.get("age_min"))), 1) if chrono else None,
        "best_trade": _slim(best),
        "worst_trade": _slim(worst),
        "days": days,
        "cum_pnl": cum,
        "per_inst": per_inst,
        "hold_buckets": hold,
        "by_hour": by_hour,
        "by_exit": by_exit,
        "streak": {"cur_kind": cur_kind, "cur_len": cur_len, "max_win": max_w, "max_loss": max_l},
    }


def build_scatter(events):
    """One dot per Jev buy-vote: x=margin, y=noul, outcome from the trade it caused."""
    opens = [e for e in events if e.get("event") == "open"]
    closes = [e for e in events if e.get("event") in ("close", "close_detected")]
    pts = []
    for e in events:
        if e.get("event") != "jev":
            continue
        ba = (e.get("answers") or {}).get("best_action") or {}
        choice = ba.get("choice")
        if not choice or choice == "no_trade":
            continue
        probs = ba.get("probabilities") or {}
        p_no = probs.get("no_trade")
        p_c = probs.get(choice)
        if p_c is None or p_no is None:
            continue
        m = re.match(r"buy_(.+)", choice)
        if not m:
            continue
        base = m.group(1)
        inst = None
        for k in (e.get("state") or {}).get("pairs") or {}:
            if k.split("-")[0].lower() == base:
                inst = k
                break
        noul = ((e.get("answers") or {}).get("p_up_%s" % base) or {}).get("noul")
        jts = parse_ts(e.get("ts", ""))
        if not inst or not jts:
            continue
        # find the open this call caused
        fired = None
        for o in opens:
            if o.get("inst") != inst:
                continue
            ots = parse_ts(o.get("ts", ""))
            if ots and 0 <= (ots - jts).total_seconds() <= 200:
                fired = o
                break
        outcome = "blocked"
        net = None
        if fired:
            ots = parse_ts(fired.get("ts", ""))
            # find its close
            for c in closes:
                if c.get("inst") != inst:
                    continue
                cts = parse_ts(c.get("ts", ""))
                if cts and ots and cts >= ots and (cts - ots).total_seconds() < 6 * 3600:
                    outcome = "win" if (c.get("upc") or 0) > 0 else "loss"
                    net = c.get("upc")
                    break
            else:
                outcome = "open"
        pts.append({"ts": e.get("ts"), "inst": inst, "margin": round(p_c - p_no, 3),
                    "noul": noul, "conf": ba.get("confidence"), "outcome": outcome,
                    "upc": net})
    return pts[-120:]


def build_funnel(events, universe):
    """Selectivity funnel + model I/O stats over the whole log."""
    c = {"universe_scan": 0, "quality_gate_reject": 0, "universe_depth_reject": 0,
         "jev": 0, "buy_picks": 0, "open": 0, "cooldown_skip": 0}
    tok_in = tok_out = state_chars = 0
    tok_today = 0
    sum_n_eur = sum_n_cand = sum_jev_pairs = 0
    today = _now().strftime("%Y-%m-%d")
    first_ts = None
    cycle_starts = []
    prev_ts = None
    for e in events:
        ev = e.get("event")
        ts = e.get("ts")
        if ts and first_ts is None:
            first_ts = ts
        if ev in c:
            c[ev] += 1
        if ev == "universe_scan":
            sum_n_eur += e.get("n_eur") or 0
            sum_n_cand += e.get("n_candidates") or 0
        if ev == "jev":
            ans = (e.get("answers") or {}).get("best_action") or {}
            if ans.get("choice") and ans.get("choice") != "no_trade":
                c["buy_picks"] += 1
            u = e.get("usage") or {}
            tok_in += u.get("input_tokens") or 0
            tok_out += u.get("output_tokens") or 0
            pstat = e.get("prompt_stats") or {}
            state_chars += pstat.get("state_chars") or 0
            sum_jev_pairs += pstat.get("n_pairs") or len((e.get("state") or {}).get("pairs") or {})
            if (ts or "").startswith(today):
                tok_today += u.get("input_tokens") or 0
        # cycle clustering: gap > 90s = new cycle
        ets = parse_ts(ts or "")
        if ets:
            if prev_ts is None or (ets - prev_ts).total_seconds() > 90:
                cycle_starts.append(ets)
            prev_ts = ets
    gaps = sorted((cycle_starts[i] - cycle_starts[i - 1]).total_seconds()
                  for i in range(1, len(cycle_starts)))
    cycle_median = gaps[len(gaps) // 2] if gaps else 300
    n = c["jev"] or 1
    first_dt = parse_ts(first_ts) if first_ts else None
    uptime_h = round((_now() - first_dt).total_seconds() / 3600, 1) if first_dt else None
    return {
        "eur_pairs": universe.get("n_eur_pairs"),
        "cycles": len(cycle_starts),
        "cycle_median_s": int(cycle_median),
        "last_cycle_ts": cycle_starts[-1].isoformat() if cycle_starts else None,
        "scans": c["universe_scan"],
        "pair_scans_all": sum_n_eur,
        "candidates_all": sum_n_cand,
        "jev_pair_evals_all": sum_jev_pairs,
        "gate_rejects": c["quality_gate_reject"],
        "depth_rejects": c["universe_depth_reject"],
        "cooldown_skips": c["cooldown_skip"],
        "jev_calls": c["jev"],
        "buy_picks": c["buy_picks"],
        "opens": c["open"],
        "avg_input_tok": int(tok_in / n),
        "avg_output_tok": int(tok_out / n),
        "avg_state_chars": int(state_chars / n),
        "max_input_tok": max((e.get("usage") or {}).get("input_tokens") or 0 for e in events if e.get("event") == "jev") if c["jev"] else 0,
        "budget_warns": sum(1 for e in events if e.get("event") == "prompt_budget_warn"),
        "tok_input_today": tok_today,
        "tok_input_all": tok_in,
        "tok_output_all": tok_out,
        # Jev pricing $0.04/M input
        "usd_today": round(tok_today * 0.04 / 1e6, 6),
        "usd_all": round(tok_in * 0.04 / 1e6, 4),
        "uptime_h": uptime_h,
        "first_ts": first_ts,
    }


def build_equity(events, trips):
    day_start = None
    day = None
    for e in reversed(events):
        if e.get("event") == "day_roll":
            day_start = e.get("start_equity_eur")
            day = e.get("day")
            break
    today = _now().strftime("%Y-%m-%d")
    day_pnl = round(sum(t["net_eur"] for t in trips
                        if (t.get("close_ts") or "").startswith(today)), 2)
    day_trades = sum(1 for t in trips if (t.get("close_ts") or "").startswith(today))
    # latest known cash from newest jev state
    cash = None
    for e in reversed(events):
        if e.get("event") == "jev":
            cash = (e.get("state") or {}).get("cash_eur")
            break
    equity = round(day_start + day_pnl, 2) if day_start is not None else None
    return {
        "day": day or today,
        "day_start_eur": day_start,
        "day_pnl_eur": day_pnl,
        "day_pnl_pct": round(100 * day_pnl / day_start, 2) if day_start else None,
        "day_trades": day_trades,
        "equity_eur": equity,
        "cash_eur": cash,
    }


TAPE_FMT = {
    "open": lambda e: ("OPEN", "▲ OPEN %s  qty %s @ %s  stake €%s  conf %s margin %s noul %s [%s]" % (
        e.get("inst"), e.get("qty"), e.get("fill_px"), e.get("stake"),
        e.get("conf"), e.get("margin"), e.get("noul"), e.get("entry_type", "-"))),
    "close": lambda e: ("CLOSE", "▼ CLOSE %s  %+.2f%%  €%s  after %sm — %s" % (
        e.get("inst"), e.get("upc", 0),
        round((e.get("stake_eur") or 0), 2) if e.get("stake_eur") else "-",
        e.get("age_min", "?"), e.get("reason", ""))),
    "close_detected": lambda e: ("CLOSE", "▼ EXIT %s  %+.2f%% — server-side OCO fired" % (
        e.get("inst"), e.get("upc", 0))),
    "trail_armed": lambda e: ("TRAIL", "◈ TRAIL ARMED %s  hwm +%.2f%%  callback %.3f%%  active @ %s" % (
        e.get("inst"), e.get("hwm_pct", 0), 100 * (e.get("callBackRatio") or 0), e.get("activePx"))),
    "jev": None,  # special-cased
    "no_trade": lambda e: ("JEV", "JEV → NO-TRADE (%s)" % e.get("why", "")),
    "quality_gate_reject": lambda e: ("GATE", "GATE REJECT %s: %s" % (
        e.get("inst"), "; ".join(e.get("fails", [])))),
    "universe_scan": lambda e: ("SCAN", "UNIVERSE SCAN %s EUR pairs → %s candidates → top: %s" % (
        e.get("n_eur"), e.get("n_candidates"), ",".join(e.get("selected", [])[:5]))),
    "universe_depth_reject": lambda e: ("GATE", "DEPTH REJECT %s — top5 book €%s < floor" % (
        e.get("inst"), e.get("depth_eur"))),
    "cooldown_skip": lambda e: ("RISK", "COOLDOWN %s — re-entry locked %sm" % (
        e.get("inst"), e.get("min"))),
    "entry_unfilled": lambda e: ("FILL", "MAKER ENTRY UNFILLED %s — cancelled, no chase" % e.get("inst")),
    "day_roll": lambda e: ("DAY", "═ DAY ROLL %s — opening equity €%s" % (
        e.get("day"), e.get("start_equity_eur"))),
    "daily_breaker": lambda e: ("ALERT", "‼ DAILY LOSS BREAKER TRIPPED — entries halted until UTC midnight"),
    "algo_fallback": lambda e: ("RISK", "OCO ATTACH FALLBACK %s — standalone stop placed" % e.get("inst")),
    "algo_fallback_err": lambda e: ("ALERT", "OCO ATTACH FAILED %s — poll stop-loss owns the position" % e.get("inst")),
    "sweep": lambda e: ("CASH", "STABLE SWEEP → EUR"),
    "close_dust": lambda e: ("DUST", "DUST CLEANUP %s" % e.get("inst")),
    "jev_pair_cap": lambda e: ("JEV", "JEV PAIR CAP — dropped lowest-ranked candidates"),
    "prompt_budget_warn": lambda e: ("JEV", "PROMPT BUDGET WARN — state trimmed"),
}


def build_tape(events, limit=90):
    tape = []
    for e in events:
        ev = e.get("event")
        ts = e.get("ts")
        if not ts:
            continue
        if ev == "jev":
            ans = e.get("answers") or {}
            ba = ans.get("best_action") or {}
            probs = ba.get("probabilities") or {}
            choice = ba.get("choice", "?")
            txt = "JEV CALL → %s  conf %s  p[%s]" % (
                choice.upper(), ba.get("confidence"),
                " ".join("%s=%.2f" % (k, v) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:4]))
            kind = "JEV"
        elif ev == "close":
            fmt = TAPE_FMT[ev]
            kind, txt = "CLOSE", ("▼ CLOSE %s  %+.2f%%  after %sm — %s" % (
                e.get("inst"), e.get("upc", 0), e.get("age_min", "?"), e.get("reason", "")))
        elif ev in TAPE_FMT and TAPE_FMT[ev]:
            try:
                kind, txt = TAPE_FMT[ev](e)
            except Exception:
                continue
        else:
            continue
        color = "dim"
        if kind == "OPEN":
            color = "green"
        elif kind == "CLOSE":
            upc = e.get("upc", 0) or 0
            color = "green" if upc >= 0 else "red"
        elif kind in ("ALERT",):
            color = "red"
        elif kind in ("JEV",):
            color = "amber"
        elif kind in ("SCAN", "TRAIL", "DAY"):
            color = "cyan"
        tape.append({"ts": ts, "kind": kind, "text": txt, "color": color, "event": ev})
    return tape[-limit:][::-1]


DOSSIER_DIR = os.path.join(SITE, "dossiers")
DEBRIEFS_PER_RUN = 3  # cap LLM debrief calls per build (dossiers are cached forever)


def make_debrief(dossier):
    """One-line LLM post-mortem for a closed trade. Cached in the dossier file."""
    t = dossier["trade"]
    dec = dossier["decision"]
    fg = (dec.get("fear_greed") or {})
    snap = json.dumps({
        "inst": t["inst"], "net_eur": t["net_eur"], "upc": t["upc"],
        "held_min": t["age_min"], "exit": t["reason"], "entry_type": t.get("entry_type"),
        "vote": {"choice": dec.get("choice"), "conf": dec.get("confidence"),
                 "margin": None, "noul": dec.get("noul"),
                 "probs": dec.get("probabilities")},
        "mkt_30m": (dec.get("pair_state") or {}).get("chg_30min_pct"),
        "mkt_7d": (dec.get("pair_state") or {}).get("chg_7d_pct"),
        "fear_greed": fg.get("value"),
        "timeline": [e.get("event") for e in dossier.get("timeline", [])],
    }, separators=(",", ":"))
    prompt = (
        "You are the post-mortem voice of OBEKT TERMINAL, a showcase of a live autonomous crypto scalper. "
        "One closed trade, raw telemetry:\n" + snap + "\n"
        "Write ONE debrief line, max 150 chars, ALL-CAPS terminal voice: name what worked or failed "
        "(entry timing, exit engine, model vote vs outcome) using ONLY these facts. No advice, no filler. "
        'Reply with the line alone, no JSON, no quotes.'
    )
    key = os.environ.get(LLM_KEY_ENV, "")
    if not key:
        if LLM_URL.startswith(("http://localhost", "http://127.0.0.1")):
            key = "***"
        else:
            return None
    try:
        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 60, "temperature": 0.5,
        }).encode()
        req = urllib.request.Request(LLM_URL, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bea" + "rer " + key})
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read().decode())
        txt = resp["choices"][0]["message"]["content"].strip().strip('"').strip("'")
        txt = txt.splitlines()[0][:160] if txt else None
        return txt
    except Exception as ex:
        print("debrief failed: %s" % ex, file=sys.stderr)
        return None


def fetch_trade_candles(inst, ts_open, ts_close, bar="5m"):
    """OHLC 5m candles covering a trade window (+pad). One keyless call.
    NB: OKX history-candles `after=<ms>` returns the 100 candles ENDING at that ms
    (same inversion quirk as the econ calendar), so anchor `after` past the close."""
    if not (ts_open and ts_close):
        return []
    hi = int((ts_close + timedelta(minutes=45)).timestamp() * 1000)
    lo = int((ts_open - timedelta(minutes=45)).timestamp() * 1000)
    url = ("https://eea.okx.com/api/v5/market/history-candles?instId=%s&bar=%s&limit=100&after=%d"
           % (urllib.parse.quote(inst), bar, hi))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jev-terminal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as ex:
        print("candle fetch failed %s: %s" % (inst, ex), file=sys.stderr)
        return []
    out = []
    for row in data.get("data") or []:
        try:
            ts = int(row[0])
        except (ValueError, IndexError):
            continue
        if not (lo <= ts <= hi):
            continue
        out.append([ts, float(row[1]), float(row[2]), float(row[3]), float(row[4])])  # ts,o,h,l,c
    out.sort(key=lambda x: x[0])
    return out


DOSSIER_CANDLE_BACKFILL = 8  # cached dossiers get their chart backfilled, capped per run


def build_dossier(trip, events, jev_events, debrief_budget=0, candle_budget=0):
    """Full decision replay for one closed trade. Cached: closed trades are immutable."""
    os.makedirs(DOSSIER_DIR, exist_ok=True)
    path = os.path.join(DOSSIER_DIR, trip["tid"] + ".json")
    cached = _load_json(path)
    if cached and cached.get("complete"):
        used_d = 0
        if (not cached.get("debrief") and debrief_budget > 0
                and (cached.get("debrief_fails") or 0) < 3):
            line = make_debrief(cached)
            if line:
                cached["debrief"] = line
                used_d = 1
            else:
                cached["debrief_fails"] = (cached.get("debrief_fails") or 0) + 1
                with open(path, "w") as f:
                    json.dump(cached, f, separators=(",", ":"))
        used_c = 0
        if not cached.get("candles") and candle_budget > 0:
            tso = parse_ts(cached.get("trade", {}).get("open_ts") or "")
            tsc = parse_ts(cached.get("trade", {}).get("close_ts") or "")
            cnd = fetch_trade_candles(cached["trade"]["inst"], tso, tsc)
            if cnd:
                cached["candles"] = cnd
                used_c = 1
        # cheap in-place schema top-ups (no network / LLM cost)
        dec = cached.get("decision") or {}
        margin_added = False
        if dec.get("margin") is None and dec.get("probabilities"):
            probs = dec["probabilities"]
            p_no = probs.get("no_trade")
            p_c = probs.get(dec.get("choice") or "")
            if p_c is not None and p_no is not None:
                dec["margin"] = round(p_c - p_no, 3)
                margin_added = True
        if (cached.get("trade") or {}).get("bracket") is None:
            pb = (dec.get("pair_state") or {}).get("bracket")
            if pb:
                cached["trade"]["bracket"] = pb
                margin_added = True
        if used_d or used_c or margin_added:
            with open(path, "w") as f:
                json.dump(cached, f, separators=(",", ":"))
        return cached, used_d, used_c

    inst = trip["inst"]
    ts_open = parse_ts(trip.get("open_ts") or "")
    ts_close = parse_ts(trip.get("close_ts") or "")

    # the triggering jev call: nearest jev event at/before the open (<=10 min back)
    trigger = None
    for e in jev_events:
        ets = parse_ts(e.get("ts", ""))
        if not ets or not ts_open:
            continue
        dt = (ts_open - ets).total_seconds()
        if -5 <= dt <= 600:
            trigger = e  # keep last match before open
    if trigger is None:
        for e in jev_events:  # fallback: any call for this inst closest to open
            if inst.split("-")[0].lower() in json.dumps(e.get("answers") or {}):
                ets = parse_ts(e.get("ts", ""))
                if ets and ts_open and abs((ts_open - ets).total_seconds()) <= 900:
                    trigger = e

    state = (trigger or {}).get("state") or {}
    answers = (trigger or {}).get("answers") or {}
    ba = answers.get("best_action") or {}
    probs = ba.get("probabilities") or {}
    base = inst.split("-")[0].lower()
    pair_state = (state.get("pairs") or {}).get(inst) or {}
    p_no = probs.get("no_trade")
    p_c = probs.get(ba.get("choice") or "")
    margin = round(p_c - p_no, 3) if (p_c is not None and p_no is not None) else None

    # gates that this pair passed pre-jev: look back for rejects (absence = passed)
    gate_window_start = (ts_open - timedelta(minutes=12)) if ts_open else None
    recent_rejects = []
    if gate_window_start:
        for e in events:
            if e.get("event") != "quality_gate_reject" or e.get("inst") != inst:
                continue
            ets = parse_ts(e.get("ts", ""))
            if ets and gate_window_start <= ets <= (ts_open or ets):
                recent_rejects.append({"ts": e.get("ts"), "fails": e.get("fails", [])})

    # lifecycle: every event for this inst between open-6m and close+2m
    timeline = []
    lo = (ts_open - timedelta(minutes=6)) if ts_open else None
    hi = (ts_close + timedelta(minutes=2)) if ts_close else None
    for e in events:
        ets = parse_ts(e.get("ts", ""))
        if not ets:
            continue
        if e.get("inst") != inst:
            continue
        if lo and ets < lo:
            continue
        if hi and ets > hi:
            continue
        ev = e.get("event")
        if ev == "jev":
            continue  # rendered separately as the decision block
        timeline.append(e)

    # news/social/macro context that was in the prompt
    dossier = {
        "tid": trip["tid"],
        "complete": bool(trip.get("close_ts")),
        "trade": trip,
        "decision": {
            "ts": (trigger or {}).get("ts"),
            "choice": ba.get("choice"),
            "confidence": ba.get("confidence"),
            "probabilities": probs,
            "noul": (answers.get("p_up_%s" % base) or {}).get("noul"),
            "margin": margin,
            "pair_state": pair_state,
            "all_pairs": {k: v for k, v in (state.get("pairs") or {}).items()},
            "cash_eur": state.get("cash_eur"),
            "stake_eur": state.get("stake_eur"),
            "open_positions": state.get("open_positions"),
            "funding_bps": state.get("market_funding_bps"),
            "news": state.get("news_catalysts", []),
            "news_updated": state.get("news_updated"),
            "fear_greed": state.get("fear_greed"),
            "trending": state.get("trending_syms", []),
            "macro_upcoming": state.get("macro_events_24h", []),
            "macro_surprises": state.get("macro_released_3h_surprises", []),
            "usage": (trigger or {}).get("usage"),
            "prompt_stats": (trigger or {}).get("prompt_stats"),
            "context": state.get("context"),
        },
        "pre_entry_rejects": recent_rejects[-3:],
        "timeline": timeline,
        "candles": fetch_trade_candles(inst, ts_open, ts_close) if ts_open and ts_close else [],
    }
    used = 0
    if dossier["complete"] and debrief_budget > 0:
        line = make_debrief(dossier)
        if line:
            dossier["debrief"] = line
            used = 1
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dossier, f, separators=(",", ":"))
    os.replace(tmp, path)
    return dossier, used, (1 if dossier["candles"] else 0)


def build_jev_latest(events):
    latest = None
    for e in reversed(events):
        if e.get("event") == "jev":
            latest = e
            break
    if not latest:
        return None
    ans = latest.get("answers") or {}
    ba = ans.get("best_action") or {}
    probs = ba.get("probabilities") or {}
    state = latest.get("state") or {}
    pairs = state.get("pairs") or {}
    rows = []
    for inst, p in pairs.items():
        base = inst.split("-")[0].lower()
        noul = (ans.get("p_up_%s" % base) or {}).get("noul")
        rows.append({
            "inst": inst,
            "p_buy": probs.get("buy_%s" % base),
            "noul": noul,
            "chg_30min_pct": p.get("chg_30min_pct"),
            "chg_4h_pct": p.get("chg_4h_pct"),
            "chg_7d_pct": p.get("chg_7d_pct"),
            "range30_pos_pct": p.get("range30_pos_pct"),
            "spread_pct": p.get("spread_pct"),
            "bracket": p.get("bracket"),
        })
    rows.sort(key=lambda r: -(r.get("p_buy") or 0))
    choice = ba.get("choice", "no_trade")
    p_no = probs.get("no_trade")
    p_choice = probs.get(choice) if choice != "no_trade" else p_no
    margin = round(p_choice - p_no, 3) if (p_choice is not None and p_no is not None) else None
    chosen_inst = None
    chosen_noul = None
    if choice != "no_trade":
        m = re.match(r"buy_(.+)", choice)
        if m:
            for inst in pairs:
                if inst.split("-")[0].lower() == m.group(1):
                    chosen_inst = inst
                    chosen_noul = (ans.get("p_up_%s" % m.group(1)) or {}).get("noul")
    # engine bar: (margin>=0.25 or conf>=0.5) and noul>=0.45
    conf = ba.get("confidence")
    if chosen_inst:
        bar_margin = (margin is not None and margin >= 0.25)
        bar_conf = (conf is not None and conf >= 0.50)
        bar_noul = (chosen_noul is not None and chosen_noul >= 0.45)
        fired = bar_margin or bar_conf
        verdict_ok = bool(fired and bar_noul)
    else:
        bar_margin = bar_conf = bar_noul = None
        fired = False
        verdict_ok = False
    # what actually happened right after (open OR no_trade veto within 3 min)?
    action = None
    lts = parse_ts(latest.get("ts", ""))
    if chosen_inst and lts:
        for e in events:
            ets = parse_ts(e.get("ts", ""))
            if not ets or not (0 <= (ets - lts).total_seconds() <= 180):
                continue
            if e.get("event") == "open" and e.get("inst") == chosen_inst:
                action = {"type": "opened", "ts": e.get("ts"), "fill_px": e.get("fill_px"),
                          "stake": e.get("stake"), "entry_type": e.get("entry_type"),
                          "text": "ORDER SENT — %s filled @ %s (€%s, %s entry)" % (
                              e.get("inst"), e.get("fill_px"), e.get("stake"), e.get("entry_type"))}
                break
            if e.get("event") == "no_trade":
                why = e.get("why", "")
                action = {"type": "blocked", "ts": e.get("ts"), "why": why,
                          "text": "BLOCKED BY RISK LAYER — %s" % why}
                break
    usage = latest.get("usage") or {}
    ps = latest.get("prompt_stats") or {}
    return {
        "ts": latest.get("ts"),
        "choice": choice,
        "chosen_inst": chosen_inst,
        "confidence": conf,
        "margin": margin,
        "noul": chosen_noul,
        "probabilities": probs,
        "pairs": rows,
        "criteria": {"margin_bar": 0.25, "conf_bar": 0.50, "noul_bar": 0.45,
                     "margin_ok": bar_margin, "conf_ok": bar_conf, "noul_ok": bar_noul,
                     "fire": verdict_ok},
        "action": action,
        "tokens": {"in": usage.get("input_tokens"), "out": usage.get("output_tokens"),
                   "state_chars": ps.get("state_chars"), "n_pairs": ps.get("n_pairs")},
        "cash_eur": state.get("cash_eur"),
        "stake_eur": state.get("stake_eur"),
        "funding_bps": state.get("market_funding_bps"),
        "open_positions": state.get("open_positions") or [],
    }


def build_jev_history(events, limit=40):
    out = []
    for e in events:
        if e.get("event") != "jev":
            continue
        ba = (e.get("answers") or {}).get("best_action") or {}
        probs = ba.get("probabilities") or {}
        choice = ba.get("choice", "?")
        p_no = probs.get("no_trade")
        p_c = probs.get(choice)
        ans = e.get("answers") or {}
        noul = None
        m = re.match(r"buy_(.+)", choice or "")
        if m:
            noul = (ans.get("p_up_%s" % m.group(1)) or {}).get("noul")
        out.append({
            "ts": e.get("ts"), "choice": choice, "conf": ba.get("confidence"),
            "margin": round(p_c - p_no, 3) if (p_c is not None and p_no is not None) else None,
            "noul": noul,
            "tok": (e.get("usage") or {}).get("input_tokens"),
        })
    return out[-limit:][::-1]


def fetch_tickers(extra_insts=()):
    url = "https://eea.okx.com/api/v5/market/tickers?instType=SPOT"
    wanted = set(MARQUEE_INSTS) | set(extra_insts)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jev-terminal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        rows = {}
        for t in data.get("data", []):
            inst = t.get("instId")
            if inst not in wanted:
                continue
            last = float(t.get("last") or 0)
            open24 = float(t.get("open24h") or 0)
            rows[inst] = {
                "inst": inst, "last": last,
                "chg24h_pct": round(100 * (last - open24) / open24, 2) if open24 else None,
                "high24": float(t.get("high24h") or 0), "low24": float(t.get("low24h") or 0),
                "vol24h_eur": round(float(t.get("volCcy24h") or 0)),
            }
        if rows:
            out = [rows[i] for i in MARQUEE_INSTS if i in rows]
            with open(TICKER_CACHE, "w") as f:
                json.dump({"ts": time.time(), "tickers": out, "px": {i: rows[i]["last"] for i in rows}}, f)
            return out, {i: rows[i]["last"] for i in rows}
    except Exception as ex:
        print("ticker fetch failed: %s" % ex, file=sys.stderr)
    c = _load_json(TICKER_CACHE) or {}
    return c.get("tickers", []), c.get("px", {})


def fetch_candles(inst, bar="5m", limit=36):
    """Last N 5m candles for an inst (keyless public). Returns [close,...] oldest→newest."""
    url = ("https://eea.okx.com/api/v5/market/candles?instId=%s&bar=%s&limit=%d"
           % (urllib.parse.quote(inst), bar, limit))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jev-terminal/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        rows = data.get("data", [])
        rows.reverse()  # API returns newest first
        return [float(x[4]) for x in rows]
    except Exception:
        return []


def fetch_sparks(insts, limit=24):
    """Mini price series for the marquee (keyless, cached in ticker_cache)."""
    out = {}
    for inst in insts:
        out[inst] = fetch_candles(inst, "5m", limit)
    return out


def build_positions(positions_raw, px):
    out = []
    now = _now()
    for inst, meta in (positions_raw or {}).items():
        if not isinstance(meta, dict):
            continue
        qty = meta.get("qty") or meta.get("sz")
        entry = (meta.get("entry_ref") or meta.get("entry_px") or meta.get("avgPx")
                 or meta.get("open_px"))
        last = px.get(inst)
        upc = upnl = None
        if last and entry:
            upc = round(100 * (float(last) - float(entry)) / float(entry), 2)
            upnl = round(float(qty or 0) * (float(last) - float(entry)), 2)
        ots = parse_ts(meta.get("opened_ts", ""))
        age_min = round((now - ots).total_seconds() / 60, 1) if ots else None
        candles = fetch_candles(inst)
        out.append({
            "inst": inst, "qty": qty, "entry_px": entry, "last_px": last,
            "upc": upc, "upnl_eur": upnl, "hwm_pct": meta.get("hwm_pct"),
            "age_min": age_min, "stake_eur": round(meta.get("stake") or 0, 2),
            "entry_type": meta.get("entry_type"), "jev_conf": meta.get("jev_conf"),
            "jev_noul": meta.get("jev_noul"), "candles_5m": candles,
            "opened_ts": meta.get("opened_ts") or meta.get("ts"),
            "bracket": meta.get("bracket"), "algo_ok": meta.get("algo_ok"),
        })
    return out


def build_commentary(jev_latest, stats, equity, news, sentiment, trips):
    """LLM desk note via halogen-qwen3.8-flash-next. Cached + rate-limited."""
    cache = _load_json(COMMENTARY_CACHE, {}) or {}
    recent = trips[-6:]
    payload_txt = json.dumps({
        "equity": equity,
        "stats": {k: stats[k] for k in ("trades_all", "win_rate_pct", "net_all_eur",
                                        "profit_factor", "fees_all_eur")},
        "latest_decision": jev_latest and {
            "ts": jev_latest["ts"], "choice": jev_latest["choice"],
            "inst": jev_latest["chosen_inst"], "conf": jev_latest["confidence"],
            "margin": jev_latest["margin"], "noul": jev_latest["noul"],
            "probabilities": jev_latest["probabilities"]},
        "recent_trades": [{k: t[k] for k in ("inst", "upc", "net_eur", "reason", "close_ts")}
                          for t in recent],
        "news": news[:6],
        "fear_greed": (sentiment or {}).get("fear_greed"),
        "funding_bps": jev_latest and jev_latest.get("funding_bps"),
    }, separators=(",", ":"))
    h = hashlib.sha256(payload_txt.encode()).hexdigest()
    age = time.time() - (cache.get("ts") or 0)
    if cache.get("hash") == h and age < COMMENTARY_MAX_AGE_S * 4:
        return cache.get("out")
    if age < COMMENTARY_MIN_INTERVAL_S and cache.get("out"):
        return cache.get("out")

    key = os.environ.get(LLM_KEY_ENV, "")
    # local servers (llama.cpp/LM Studio) need no key; send a dummy bearer then
    if not key:
        if LLM_URL.startswith(("http://localhost", "http://127.0.0.1")):
            key = "***"
        else:
            print("no LLM key in env — skipping desk note", file=sys.stderr)
            return cache.get("out")
    prompt = (
        "You are the desk commentator for OBEKT TERMINAL, a read-only Bloomberg-style showcase of a "
        "live autonomous crypto scalping system (OKX EEA spot, EUR pairs, 5-min cycles). "
        "The engine: deterministic Python gates scan ~270 EUR pairs, momentum/depth gates filter them, "
        "then the Jev System-One model returns TYPED decisions (probabilities + confidence, no free text); "
        "exits are server-side OCO + trailing stops. News/sentiment/macro are injected into Jev's state.\n\n"
        "LIVE SYSTEM SNAPSHOT (UTC):\n" + payload_txt + "\n\n"
        "Write for sophisticated visitors who might hire the sysop to build one. Rules: "
        "reference ONLY facts in the snapshot, no invented numbers, no financial advice, terse terminal voice. "
        "Reply STRICT JSON: {\"desk_note\": \"<3-5 sentences: tape read, what the engine is doing and why, "
        "risk posture>\", \"decision_line\": \"<ONE line <=110 chars explaining the latest Jev decision or "
        "why the engine is flat>\"}"
    )
    try:
        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400, "temperature": 0.4,
        }).encode()
        req = urllib.request.Request(LLM_URL, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bea" + "rer " + key})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
        txt = resp["choices"][0]["message"]["content"].strip()
        txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
        out = json.loads(txt)
        if not isinstance(out.get("desk_note"), str):
            raise ValueError("bad shape")
        out["model"] = LLM_MODEL
        out["generated"] = _now().isoformat()
        with open(COMMENTARY_CACHE, "w") as f:
            json.dump({"hash": h, "ts": time.time(), "out": out}, f)
        return out
    except Exception as ex:
        print("LLM commentary failed: %s" % ex, file=sys.stderr)
        return cache.get("out") or {
            "desk_note": "Desk note unavailable — LLM relay offline. Engine telemetry below is live and unaffected.",
            "decision_line": "COMMENTARY FEED DEGRADED — SHOWING RAW TELEMETRY",
            "model": LLM_MODEL, "generated": _now().isoformat()}


def build():
    t0 = time.time()
    events = read_log()
    trips = build_round_trips(events)
    jev_events = [e for e in events if e.get("event") == "jev"]
    # dossiers for the trades shown in the ledger (cached; only new ones rebuild).
    # Debriefs cost one LLM call each — capped per build so 40 cached dossiers backfill
    # gradually without hammering the relay; new closed trades get one immediately.
    debrief_budget = DEBRIEFS_PER_RUN
    candle_budget = DOSSIER_CANDLE_BACKFILL
    for t in trips[-40:][::-1]:
        try:
            dos, used_d, used_c = build_dossier(t, events, jev_events, debrief_budget, candle_budget)
            debrief_budget -= used_d
            candle_budget -= used_c
            if dos.get("debrief"):
                t["debrief"] = dos["debrief"]
        except Exception as ex:
            print("dossier failed %s: %s" % (t.get("tid"), ex), file=sys.stderr)
    stats = build_stats(trips, events)
    equity = build_equity(events, trips)
    jev_latest = build_jev_latest(events)
    jev_hist = build_jev_history(events)
    tape = build_tape(events)
    universe = _load_json(os.path.join(BASE, "universe.json"), {}) or {}
    funnel = build_funnel(events, universe)
    scatter = build_scatter(events)
    social = _load_json(os.path.join(BASE, "social.json"), {}) or {}
    catalyst = _load_json(os.path.join(BASE, "catalyst.json"), {}) or {}
    risk = _load_json(os.path.join(BASE, "risk_state.json"), {}) or {}
    positions_raw = _load_json(os.path.join(BASE, "positions.json"), {}) or {}

    positions_raw_keys = [k for k in (positions_raw or {}) if isinstance((positions_raw or {}).get(k), dict)]
    tickers, px_map = fetch_tickers(extra_insts=positions_raw_keys)
    positions = build_positions(positions_raw, px_map)

    news = catalyst.get("catalysts", [])
    macro = {}
    if jev_latest:
        st = None
        for e in reversed(events):
            if e.get("event") == "jev":
                st = e.get("state") or {}
                break
        macro = {"upcoming": (st or {}).get("macro_events_24h", []),
                 "surprises": (st or {}).get("macro_released_3h_surprises", [])}

    sentiment = {
        "fear_greed": social.get("fear_greed"),
        "trending": social.get("trending", []),
        "coins": social.get("coins", {}),
        "updated": social.get("updated"),
        "funding_bps": jev_latest.get("funding_bps") if jev_latest else None,
    }

    desk = build_commentary(jev_latest, stats, equity, news, sentiment, trips)

    data = {
        "generated": _now().isoformat(),
        "last_event_ts": events[-1].get("ts") if events else None,
        "build_ms": int((time.time() - t0) * 1000),
        "engine": ENGINE,
        "contact": CONTACT_LINE,
        "equity": equity,
        "stats": stats,
        "funnel": funnel,
        "scatter": scatter,
        "positions": positions,
        "jev_latest": jev_latest,
        "jev_history": jev_hist,
        "universe": universe,
        "tickers": tickers,
        "sparks": fetch_sparks(MARQUEE_INSTS[:6]),
        "news": news,
        "news_updated": catalyst.get("updated"),
        "macro": macro,
        "sentiment": sentiment,
        "risk": {"day": risk.get("day"), "breaked": risk.get("breaked"),
                 "cooldowns": {k: v for k, v in (risk.get("exits") or {}).items()}},
        "tape": tape,
        "trades": trips[-40:][::-1],
        "desk_note": desk,
    }
    tmp = os.path.join(SITE, "data.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(SITE, "data.json"))
    tmp = os.path.join(SITE, "data.js.tmp")
    with open(tmp, "w") as f:
        f.write("window.TT_DATA = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    os.replace(tmp, os.path.join(SITE, "data.js"))
    # regenerate the OG share card, throttled (numbers don't need 45s churn)
    try:
        og_path = os.path.join(SITE, "og.png")
        og_age = time.time() - (os.path.getmtime(og_path) if os.path.exists(og_path) else 0)
        if og_age > 300 or "--og" in sys.argv:
            import make_og
            make_og.render(data)
    except Exception as ex:
        print("og render skipped: %s" % ex, file=sys.stderr)
    print("built in %dms — trades=%d tape=%d tickers=%d desk=%s" % (
        data["build_ms"], stats["trades_all"], len(tape), len(tickers),
        "ok" if desk and desk.get("model") else "fallback"))
    return data


if __name__ == "__main__":
    build()
