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
# STATE_DIR holds the expensive-to-rebuild artifacts (LLM debrief dossiers, the
# commentary cache, the ticker cache). Point it at a persistent volume so a
# container restart does not re-spend those LLM calls. Defaults to SITE.
STATE_DIR = os.path.expanduser(os.environ.get("TERMINAL_STATE_DIR", SITE))
os.makedirs(STATE_DIR, exist_ok=True)
COMMENTARY_CACHE = os.path.join(STATE_DIR, "commentary_cache.json")
TICKER_CACHE = os.path.join(STATE_DIR, "ticker_cache.json")

MARQUEE_INSTS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "NEAR-EUR",
                 "SUI-EUR", "PUMP-EUR", "LINK-EUR", "AAVE-EUR", "AVAX-EUR"]

CONTACT_LINE = os.environ.get(
    "TERMINAL_CONTACT_LINE",
    "OBEKT AUTONOMOUS TRADING PLATFORM  //  BUILT BY OBEKT AI WORKS -> https://obekt.com")

def _choice_base(choice):
    """Base symbol from a Jev choice token. Spot emits buy_<base>; the two-way
    perp book emits long_<base> / short_<base>. Accepting only buy_ silently
    empties every panel that resolves the chosen instrument."""
    m = re.match(r"(?:buy|long|short)_(.+)", str(choice or ""))
    return m.group(1) if m else None


def _choice_side(choice):
    s = str(choice or "")
    if s.startswith("short_"):
        return "short"
    if s.startswith("long_"):
        return "long"
    return "long" if s.startswith("buy_") else None


def _p_directional(probs, base):
    """The model's probability for TAKING the directional trade on `base`.
    Spot has one entry key (buy_<base>); the perp book has two (long_/short_) and
    the engine picks the better side — mirror that choice so the deck shows the
    number that actually drove the decision, not an arbitrary one."""
    if not base:
        return None
    pl, psh = probs.get("long_%s" % base), probs.get("short_%s" % base)
    pb = probs.get("buy_%s" % base)
    cands = [v for v in (pb, pl, psh) if v is not None]
    return max(cands) if cands else None


def _live_params():
    """Every number the ARCHITECTURE panel quotes, parsed from the LIVE engine's
    source. This panel was the single worst offender for drift: it hardcoded all
    eight steps describing the retired spot engine — "MARGIN ≥0.25", "IOC CHASE
    FALLBACK" (deleted in v3.8), "STAKE = ALL FREE EUR", cooldowns 15/30, stale
    exit 90m — while the live book ran entirely different values. Parse, never
    transcribe."""
    fname = "okx_perp.py" if SPOT_BOOK_RETIRED else "okx_jev.py"
    defaults = {"MARGIN_BAR": 0.30, "CONF_BAR": 0.50, "CONF_FLOOR": 0.40,
                "NOUL_BAR": 0.45, "MIN_VOL24H_USD": 300000, "MIN_TOP5_DEPTH_USD": 1000,
                "MAX_SPREAD_PCT": 0.10, "MIN_DAY_RANGE_PCT": 1.5, "EDGE_MULT": 4.0,
                "MIN_TARGET_PCT": 0.6, "MAX_TARGET_PCT": 2.0, "SL_COST_MULT": 1.75,
                "TIME_STOP_MIN": 30, "STALE_EXIT_MIN": 60, "COOLDOWN_MIN": 20,
                "WIN_COOLDOWN_MIN": 10, "DAILY_LOSS_PCT": 8.0, "MAX_NOTIONAL_FRAC": 0.98,
                "PERP_LEV": "1", "MIN_VOL30_PCT": 0.35, "MAX_JEV_PAIRS": 4}
    try:
        src = open(os.path.join(BASE, fname)).read(40960)
    except Exception:
        return dict(defaults, source=fname, retired_book=SPOT_BOOK_RETIRED,
                    has_prompt_budget=False)
    out = dict(defaults)
    for name, dflt in defaults.items():
        m = re.search(r'^%s\s*=\s*"?([0-9_.]+)"?' % name, src, re.M)
        if not m:
            continue
        raw = m.group(1).replace("_", "")
        try:
            out[name] = float(raw) if "." in raw else int(raw)
        except ValueError:
            out[name] = raw
    out["source"] = fname
    out["retired_book"] = SPOT_BOOK_RETIRED
    # Prompt-budget machinery only exists in whichever engine actually defines it.
    # The live X-Perp engine has NO trim loop (its prompt is small: <=4 contracts),
    # so a panel claiming "the engine hard-caps input" would describe code that
    # isn't running. Report presence explicitly instead of implying it.
    for pname in ("MAX_STATE_CHARS", "MAX_JEV_INPUT_TOKENS", "CHARS_PER_TOKEN"):
        m = re.search(r'^%s\s*=\s*([0-9_.]+)' % pname, src, re.M)
        try:
            out[pname] = float(m.group(1).replace("_", "")) if m else None
        except Exception:
            out[pname] = None
    out["has_prompt_budget"] = out.get("MAX_STATE_CHARS") is not None
    return out


def _engine_bars(fname="okx_jev.py"):
    """Parse gate thresholds from a specific engine's source so the deck's
    DECISION GATES panel can never drift from what that engine enforces.

    MUST be called with the engine whose decisions are actually being displayed:
    the spot and perp books run DIFFERENT bars (spot CONF_FLOOR 0.50 vs perp
    burn-in 0.40), so scoring one book's call against the other's thresholds
    would show a contradiction — the exact drift this function exists to prevent.
    """
    defaults = (0.30, 0.50, 0.50, 0.45)  # MARGIN_BAR, CONF_BAR, CONF_FLOOR, NOUL_BAR
    names = ("MARGIN_BAR", "CONF_BAR", "CONF_FLOOR", "NOUL_BAR")
    try:
        src = open(os.path.join(BASE, fname)).read(32768)
    except Exception:
        return defaults
    def _num(name):
        m = re.search(r'^%s\s*=\s*([0-9.]+)' % name, src, re.M)
        return float(m.group(1)) if m else None
    vals = [_num(n) for n in names]
    return tuple(vals) if all(v is not None for v in vals) else defaults


def _perp_cooldowns():
    """Cooldown windows parsed from the LIVE engine source (never hardcoded — they
    drifted twice already) plus the perp risk-state exits map, so the gates panel
    shows the live book's lockouts rather than the retired spot book's frozen ones.
    """
    try:
        src = open(os.path.join(BASE, "okx_perp.py")).read(32768)
        def _num(name):
            m = re.search(r'^%s\s*=\s*([0-9.]+)' % name, src, re.M)
            return int(float(m.group(1))) if m else None
        loss = _num("COOLDOWN_MIN")
        win = _num("WIN_COOLDOWN_MIN")
    except Exception:
        loss = win = None
    exits = (_load_json(os.path.join(BASE, "perp_risk_state.json"), {}) or {}).get("exits") or {}
    return {"loss_min": loss or 20, "win_min": win or 10, "exits": exits}


def _perp_engine_version():
    """ENGINE_VERSION from okx_perp.py source — single source of truth, so the
    perp panel's version can never drift from the engine actually running."""
    try:
        src = open(os.path.join(BASE, "okx_perp.py")).read(16384)
        m = re.search(r'ENGINE_VERSION\s*=\s*["\']([^"\']+)["\']', src)
        return m.group(1) if m else None
    except Exception:
        return None


# 2026-10-15: the operator consolidated the account into the two-way book. The
# spot EUR engine's timer is disabled and its EUR was converted to USDC, so the
# spot book no longer trades. ONE flag drives every "retired" label and the
# version string so the deck can't contradict itself; flip it back to False if
# the spot timer is ever re-enabled.
SPOT_BOOK_RETIRED = True


def _src_version(fname):
    """Read ENGINE_VERSION out of an engine source file. Parsed, never hardcoded."""
    try:
        src = open(os.path.join(BASE, fname)).read(16384)
        m = re.search(r'ENGINE_VERSION\s*=\s*["\']([^"\']+)["\']', src)
        return m.group(1) if m else None
    except Exception:
        return None


def _engine_versions():
    return {"spot": _src_version("okx_jev.py"), "perp": _src_version("okx_perp.py")}


_VERSIONS_CACHE = {"ts": 0.0, "val": None}


def _versions():
    """Versions read fresh per build (30s cache). This used to be a module-level
    constant: serve.py keeps this module imported across its 45s rebuilds, so the
    header version froze at whatever the engine source said when the CONTAINER
    STARTED (observed 10-22: header v4.8-perp while the engine ran v4.9.3-perp and
    the build-time perp.engine field was correct — the deck contradicted itself)."""
    now = time.time()
    if _VERSIONS_CACHE["val"] is None or now - _VERSIONS_CACHE["ts"] > 30:
        _VERSIONS_CACHE["val"] = _engine_versions()
        _VERSIONS_CACHE["ts"] = now
    return _VERSIONS_CACHE["val"]


def _engine_version():
    """Combined version across both engines (spot okx_jev.py + perp okx_perp.py).
    Read from source = single source of truth; never hardcode (it drifts)."""
    v = _versions()
    spot = v["spot"]
    perp = v["perp"]
    # Live book first. If spot is retired, saying "SPOT vX + PERP vY" implies two
    # running engines when one is switched off. Driven by SPOT_BOOK_RETIRED so the
    # label can never disagree with the rest of the deck.
    if perp and spot:
        if SPOT_BOOK_RETIRED:
            return "X-PERPS %s (LIVE) · SPOT %s (retired)" % (perp, spot)
        return "SPOT %s + PERP %s" % (spot, perp)
    return perp or spot or "unknown"


def _engine_header():
    """One compact line for the sticky header.

    The header is the only consumer of version/venue/mode, and composing the full
    strings there produced 179 chars / 1256px — it wrapped into four rows and pushed
    the page down. Two things in it were also wrong rather than merely long:
      * "X-PERPS" appeared twice (inside the version string AND inside the venue),
      * the RETIRED spot engine's version was shown next to the live one, reading as
        if two engines were current.
    So the header gets a short line naming only the live engine. Nothing is lost:
    `version`/`venue`/`mode` stay in the payload for the boot text and the dossier
    payload, and the spot version is preserved in spot_book.version and printed in
    the retired record panel.
    """
    v = _versions()
    live = v["perp"] or v["spot"] or "unknown"
    mode = "LIVE · TWO-WAY" if SPOT_BOOK_RETIRED else "LIVE · TWO-WAY + SPOT"
    return "%s · OKX EUROPE (EEA) X-PERPS · %s" % (live, mode)

# Launch bankroll (09-28) — the anchor for the all-time realized return %.
# Realized trading P&L is expressed against THIS, not against a moving equity
# base, so the number stays honest as capital is reallocated (e.g. EUR->USDC
# for the perp book is a transfer, not a loss).
LAUNCH_EUR = 147.77

# X-Perp book capital base. Like LAUNCH_EUR, this anchors the perp book's
# all-time return % to money actually PUT IN — not to a moving equity value.
# Each entry is (iso UTC timestamp, USDC added). The account-level LAUNCH_EUR is
# unaffected: moving EUR into USDC is a TRANSFER between books, not a loss.
#   07:22 book funded   22.4017 USDC (bought with ~EUR 20)
#   12:06 consolidation +111.7876 USDC (EUR 99.73 converted at 0.89211, the
#         operator-directed move of the spot bankroll into the two-way book)
PERP_CAPITAL_EVENTS = (
    ("2026-10-15T07:22:07+00:00", 22.4017),
    ("2026-10-15T12:06:30+00:00", 111.7876),
)
PERP_CAPITAL_USD = round(sum(a for _, a in PERP_CAPITAL_EVENTS), 4)


def _perp_capital_at(iso_ts):
    """USDC put into the perp book as of iso_ts. Injections are CAPITAL, not
    P&L — without this the consolidation would show up as a +498% 'gain'."""
    if not iso_ts:
        return PERP_CAPITAL_USD
    base = 0.0
    for ts, amt in PERP_CAPITAL_EVENTS:
        if ts <= iso_ts:
            base += amt
        else:
            break
    return base or PERP_CAPITAL_USD


ENGINE = {
    "version": _engine_version(),
    # Compact one-liner for the sticky header — see _engine_header() for why the
    # header must not compose version + venue + mode itself.
    "header": _engine_header(),
    # 10-15: the operator consolidated the account into the two-way book — EUR was
    # converted to USDC and the spot engine's timer was DISABLED (not deleted; the
    # code and its 132-trade record stay). So: one live book, X-Perps only.
    "venue": "OKX EUROPE (EEA) · X-PERPS",
    "cycle_s": 300,
    "quote": "USDC",
    "decision_model": "jev-latest (TypeSafe System One)",
    "desk_model": LLM_MODEL,
    "mode": "LIVE CAPITAL — FULLY AUTONOMOUS — TWO-WAY (LONG+SHORT)",
    "spot_book": {
        "status": "RETIRED 2026-10-15" if SPOT_BOOK_RETIRED else "ACTIVE",
        # Parsed from source so the retired record still names the engine that
        # produced it. This is where the spot version lives now that the header
        # names only the LIVE engine — removing it from the header must not mean
        # losing it from the deck.
        "version": _versions()["spot"],
        "why": ("Capital consolidated into the X-Perp book at operator direction. The spot "
                "EUR book was long-only (no way to profit from a decline) and carried ~0.25% "
                "round-trip fees vs the perp book's ~0.07%. Engine code and full history "
                "retained; its systemd timer is disabled, so it no longer trades."),
        "trades_closed": None,      # filled from the log by build() — never hardcode,
        "net_eur": None,            # it drifts the moment the engine trades again
        "net_pct": None,
        "profit_factor": None,
    },
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


def read_perp_log():
    """The LIVE book's raw event stream (perp_log.jsonl)."""
    events = []
    try:
        with open(os.path.join(BASE, "perp_log.jsonl"), errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return events


def _chosen_funding(e, base):
    """Funding rate for the chosen perp contract. The perp engine logs funding per
    contract inside `pairs`, not as a top-level market field like spot did, so the
    dossier's funding box would otherwise be empty for every live trade."""
    if not base:
        return None
    for p in (e.get("pairs") or []):
        if str(p.get("base", "")).lower() == base:
            v = p.get("funding_bps")
            if v is not None:
                return v
    return None


def normalize_perp_events(pevents):
    """Adapt the perp engine's flat event vocabulary onto the generic shapes the
    panel builders already consume (open/close/close_detected/jev/no_trade/
    universe_scan/quality_gate_reject/entry_unfilled), so ONE set of tested
    builders serves both books instead of a second copy that would drift.

    Monetary fields carry the LIVE book's currency (USD), not EUR — the key names
    are internal identifiers; `ccy` on the returned dicts tells the UI which
    symbol to print. Never reuse the spot builders' EUR assumption here.

    perp_jev is flat (choice/conf/margin/noul/probabilities/pairs at top level)
    while spot nests them under answers.best_action + state.pairs. Synthesising
    the nested shape is what lets build_scatter/build_jev_history work unchanged.
    """
    out = []
    for e in pevents:
        ev = e.get("event")
        if ev == "perp_open":
            out.append(dict(e, event="open", ccy="USD",
                            stake=e.get("notional_usd"),
                            qty=e.get("contracts"),
                            entry_type="maker"))
        elif ev == "perp_close":
            out.append(dict(e, event="close", ccy="USD",
                            stake_eur=e.get("notional_usd"),
                            reason=e.get("reason") or "exit"))
        elif ev == "perp_close_detected":
            out.append(dict(e, event="close_detected", ccy="USD",
                            stake_eur=e.get("notional_usd"),
                            reason=e.get("note") or "server OCO"))
        elif ev == "perp_jev":
            probs = e.get("probabilities") or {}
            choice = e.get("choice") or "no_trade"
            side = e.get("chosen_side")
            # The generic builders resolve a chosen instrument by (a) looking the
            # choice string up in `probabilities` and (b) regex-extracting the base
            # symbol from it. Perp's real probability keys are long_<base> /
            # short_<base>, so pass the REAL key through — inventing a synthetic
            # buy_<base> made the probabilities lookup return None and silently
            # emptied the scatter panel. The extraction regexes accept all three
            # prefixes (buy_/long_/short_).
            base = None
            inst = e.get("chosen_inst")
            if inst:
                base = inst.split("-")[0].lower()
            pairs = {}
            for p in (e.get("pairs") or []):
                pk = p.get("inst")
                if pk:
                    pairs[pk] = p
            # direction-specific noul: longs use the rise prob, shorts the fall prob
            noul_key = "noul_up" if side == "long" else "noul_dn"
            noul_val = e.get("noul")
            if base:
                for p in (e.get("pairs") or []):
                    if p.get("base", "").lower() == base and p.get(noul_key) is not None:
                        noul_val = p.get(noul_key)
                        break
            out.append(dict(e, event="jev", ccy="USD",
                            answers={"best_action": {
                                "choice": choice,
                                "confidence": e.get("conf"),
                                "probabilities": probs,
                            }, **({"p_up_%s" % base: {"noul": noul_val}} if base else {})},
                            state={"pairs": pairs,
                                   "cash_eur": e.get("collateral_usdc"),
                                   "stake_eur": e.get("budget_usd"),
                                   # funding for the CHOSEN contract, so the dossier's
                                   # funding field is populated for perp trades too
                                   "market_funding_bps": _chosen_funding(e, base)},
                            usage={"input_tokens": e.get("input_tokens"),
                                   "output_tokens": e.get("output_tokens")},
                            prompt_stats={"n_pairs": e.get("n_pairs")},
                            _perp_side=side))
        elif ev == "perp_no_trade":
            out.append(dict(e, event="no_trade", ccy="USD", why=e.get("why")))
        elif ev == "perp_universe":
            out.append(dict(e, event="universe_scan", ccy="USD",
                            n_eur=e.get("n_perps"),
                            n_candidates=e.get("n_cands"),
                            selected=e.get("selected")))
        elif ev == "perp_gate_reject":
            out.append(dict(e, event="quality_gate_reject", ccy="USD",
                            fails=[e.get("why") or "gate"]))
        elif ev == "perp_entry_unfilled":
            out.append(dict(e, event="entry_unfilled", ccy="USD"))
        elif ev == "perp_cooldown_skip":
            out.append(dict(e, event="cooldown_skip", ccy="USD", min=e.get("mins")))
    return out


def build_round_trips(events, ccy="EUR", capital_base=None):
    """Pair open -> close/close_detected per instrument, chronologically.

    ccy/capital_base let the SAME logic serve both books: spot amounts are EUR
    against LAUNCH_EUR, live perp amounts are USD against PERP_CAPITAL_USD. The
    `*_eur` key names are historical identifiers — read them as "book currency".
    """
    if capital_base is None:
        capital_base = LAUNCH_EUR
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
            # ENTRY FEE — denomination differs by book, and getting this wrong
            # inflates costs by ~10x:
            #   spot  : open realized_fee is in BASE TOKEN units (e.g. -0.34 CT),
            #           NOT EUR. Unusable as a quote amount -> use the rate
            #           estimate (maker 0.1% / taker 0.2%).
            #   perp  : fees are quoted in the settle ccy (USDC) on both legs, so
            #           the logged value IS the true USD cost. Use it.
            et = (o or {}).get("entry_type", "")
            fee_open_logged = (o or {}).get("realized_fee")
            if ccy == "USD" and fee_open_logged is not None:
                fee_open = abs(float(fee_open_logged or 0))
            else:
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
                "side": (o or {}).get("side"),
                "upc": upc,
                "ccy": ccy,
                "capital_base": capital_base,
                "net_pct": round(100 * (gross - fees) / stake, 3) if stake else None,
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


def build_stats(trips, events, ccy="EUR", capital_base=None, capital_base_fn=None):
    """Aggregate a book's closed trades. ccy/capital_base select the book:
    spot = EUR vs LAUNCH_EUR, live perp = USD vs PERP_CAPITAL_USD. The `*_eur`
    key names are historical identifiers, not a currency claim — `ccy` says which
    symbol the UI must print, so the perp book never renders '$131' as '€131'.

    capital_base_fn(ts) -> base at that time. Needed whenever capital was added
    mid-history: dividing early trades by today's larger base understates them.
    """
    if capital_base is None:
        capital_base = LAUNCH_EUR
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

    # cumulative PnL series (chronological) for sparkline.
    # v4: express as PERCENT, not fiat. HONEST BASIS = running realized net as a
    # % of LAUNCH capital (LAUNCH_EUR), NOT sum-of-per-trade-%-of-own-stake
    # (that conflated €18 and €120 stakes into a meaningless -32%). This is the
    # true account return curve and is venue-agnostic (EUR + USDC both count as
    # deployed capital against the one launch figure).
    chrono = sorted([t for t in trips if t.get("close_ts")], key=lambda t: t["close_ts"])
    cum, cum_pct, run = [], [], 0.0
    for t in chrono:
        run = round(run + t["net_eur"], 2)
        cum.append(run)
        base_at = capital_base_fn(t["close_ts"]) if capital_base_fn else capital_base
        cum_pct.append(round(100 * run / base_at, 3) if base_at else 0.0)

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
        # realized P&L as a PERCENT of the book's own CAPITAL BASE (never a sum of
        # per-trade %-of-own-stake: that conflates an $8 and a $131 position).
        "net_all_pct": round(100 * sum(t["net_eur"] for t in trips) / capital_base, 2) if capital_base else None,
        "ccy": ccy,
        "capital_base": capital_base,
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
        "cum_pnl_pct": cum_pct,
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
        m = re.match(r"(?:buy|long|short)_(.+)", choice)
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
    last_scan_n = None      # most recent per-cycle universe size (see below)
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
            # remember the most recent per-cycle universe size (perp logs n_perps
            # via the normalizer's n_eur alias) so the funnel can say "sweeps N
            # contracts/pairs every cycle" for the LIVE book.
            last_scan_n = e.get("n_eur") or last_scan_n
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
        # per-cycle universe size of the LIVE book (perp_universe.json has no
        # count field, so it comes from the scan events themselves)
        "perps_scanned": last_scan_n,
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


def _sym(e):
    """Currency symbol for an event's book. The retired spot book logged EUR; the
    live X-Perp book settles in USD. Formatters must never assume EUR — printing
    '$131' as '€131' is the same class of lie as a wrong P&L sign."""
    return "$" if e.get("ccy") == "USD" else "€"


def _side(e):
    """Direction label for two-way books; empty for the long-only spot book."""
    s = (e.get("side") or "").lower()
    if s == "short":
        return "SHORT "
    if s == "long":
        return "LONG "
    return ""


TAPE_FMT = {
    "open": lambda e: ("OPEN", "%sOPEN %s  qty %s @ %s  notional %s%s  conf %s margin %s noul %s [%s]" % (
        "▲ " if (e.get("side") or "").lower() != "short" else "▼ ",
        e.get("inst"), e.get("qty"), e.get("fill_px"), _sym(e), e.get("stake"),
        e.get("conf"), e.get("margin"), e.get("noul"), e.get("entry_type", "-"))),
    "close": lambda e: ("CLOSE", "▼ CLOSE %s %s %+.2f%%  %s%s  after %sm — %s" % (
        e.get("inst"), _side(e), e.get("upc", 0), _sym(e),
        round((e.get("stake_eur") or 0), 2) if e.get("stake_eur") else "-",
        e.get("age_min", "?"), e.get("reason", ""))),
    "close_detected": lambda e: ("CLOSE", "▼ EXIT %s %s %+.2f%% — server-side OCO fired" % (
        e.get("inst"), _side(e), e.get("upc", 0))),
    "trail_armed": lambda e: ("TRAIL", "◈ TRAIL ARMED %s  hwm +%.2f%%  callback %.3f%%  active @ %s" % (
        e.get("inst"), e.get("hwm_pct", 0), 100 * (e.get("callBackRatio") or 0), e.get("activePx"))),
    "jev": None,  # special-cased
    "no_trade": lambda e: ("JEV", "JEV → NO-TRADE (%s)" % e.get("why", "")),
    "quality_gate_reject": lambda e: ("GATE", "GATE REJECT %s: %s" % (
        e.get("inst"), "; ".join(e.get("fails", [])))),
    "universe_scan": lambda e: ("SCAN", "UNIVERSE SCAN %s %s → %s candidates → top: %s" % (
        e.get("n_eur"), ("X-PERP CONTRACTS" if e.get("ccy") == "USD" else "EUR PAIRS"),
        e.get("n_candidates"), ",".join(str(x)[:24] for x in (e.get("selected") or [])[:5]))),
    "universe_depth_reject": lambda e: ("GATE", "DEPTH REJECT %s — top5 book %s%s < floor" % (
        e.get("inst"), _sym(e), e.get("depth_eur"))),
    # cooldown_skip has TWO field shapes in the spot log (305 events use
    # left_min+after, 75 use min) — the perp normalizer emits `min`. Accept all
    # three or the majority case renders "locked undefinedm".
    "cooldown_skip": lambda e: ("RISK", "COOLDOWN %s — re-entry locked %sm after %s" % (
        e.get("inst"), e.get("left_min") if e.get("left_min") is not None else e.get("min"),
        e.get("after") or "exit")),
    "entry_unfilled": lambda e: ("FILL", "MAKER ENTRY UNFILLED %s — cancelled, no chase" % e.get("inst")),
    "day_roll": lambda e: ("DAY", "═ DAY ROLL %s — opening equity %s%s" % (
        e.get("day"), _sym(e), e.get("start_equity_eur"))),
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


DOSSIER_DIR = os.path.join(STATE_DIR, "dossiers")
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
        "You are the post-mortem voice of OBEKT TERMINAL, the dashboard of a LIVE autonomous crypto "
        "scalper trading REAL capital. "
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


# Bump when the dossier's CONTENT is recomputed differently; a cached dossier
# marked complete=True is otherwise served forever even after a fix. That is
# exactly what stranded the perp dossiers at timeline=0 once build_dossier
# started reading the live log — the cache had the pre-fix empty result.
DOSSIER_SCHEMA = 2


def build_dossier(trip, events, jev_events, debrief_budget=0, candle_budget=0):
    """Full decision replay for one closed trade. Cached: closed trades are immutable."""
    os.makedirs(DOSSIER_DIR, exist_ok=True)
    path = os.path.join(DOSSIER_DIR, trip["tid"] + ".json")
    cached = _load_json(path)
    if cached and cached.get("schema") != DOSSIER_SCHEMA:
        cached = None
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
        "schema": DOSSIER_SCHEMA,
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
            "p_buy": _p_directional(probs, base),
            "p_long": probs.get("long_%s" % base),
            "p_short": probs.get("short_%s" % base),
            "two_way": bool(probs.get("long_%s" % base) is not None
                            or probs.get("short_%s" % base) is not None),
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
    chosen_side = latest.get("_perp_side")
    if choice != "no_trade":
        base = _choice_base(choice)
        if base:
            for inst in pairs:
                if inst.split("-")[0].lower() == base:
                    chosen_inst = inst
                    chosen_noul = (ans.get("p_up_%s" % base) or {}).get("noul")
                    break
    # engine bars: PARSED FROM ENGINE SOURCE (single source of truth — these
    # were hardcoded 0.25/0.45 and drifted from the engine, which actually runs
    # MARGIN_BAR=0.30 + a CONJUNCTIVE CONF_FLOOR since v3.8:
    #   edge_ok = (margin >= MARGIN_BAR or conf >= CONF_BAR) and conf >= CONF_FLOOR
    #   fire    = edge_ok and noul >= NOUL_BAR
    MBAR, CBAR, CFLOOR, NBAR = _engine_bars(
        "okx_perp.py" if latest.get("ccy") == "USD" else "okx_jev.py")
    conf = ba.get("confidence")
    if chosen_inst:
        bar_margin = (margin is not None and margin >= MBAR)
        bar_conf = (conf is not None and conf >= CBAR)
        conf_floor_ok = (conf is not None and conf >= CFLOOR)
        bar_noul = (chosen_noul is not None and chosen_noul >= NBAR)
        fired = (bar_margin or bar_conf) and conf_floor_ok
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
                          "text": "ORDER SENT — %s filled @ %s (%s%s, %s entry)" % (
                              e.get("inst"), e.get("fill_px"), _sym(e), e.get("stake"), e.get("entry_type"))}
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
        "chosen_side": chosen_side or _choice_side(choice),
        "ccy": latest.get("ccy") or "EUR",
        "confidence": conf,
        "margin": margin,
        "noul": chosen_noul,
        "probabilities": probs,
        "pairs": rows,
        "criteria": {"margin_bar": MBAR, "conf_bar": CBAR, "conf_floor": CFLOOR,
                     "noul_bar": NBAR,
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
        m = re.match(r"(?:buy|long|short)_(.+)", choice or "")
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


def build_commentary(jev_latest, stats, equity, news, sentiment, trips, positions=None,
                     perp=None, retired=None):
    """LLM desk note via halogen-qwen3.8-flash-next. Cached + rate-limited.

    `stats`/`equity`/`trips` describe the LIVE book. `retired` carries the retired
    spot book's OWN frozen stats/equity/trips. They must stay separate: passing the
    live values into the retired_spot_* payload keys publishes perp USD numbers as
    spot's final EUR record (it did — "4 closed trades, net -0.59 EUR" instead of
    132 trades / -21.03 EUR), which is a wrong number, not a missing one.
    """
    cache = _load_json(COMMENTARY_CACHE, {}) or {}
    retired = retired or {}
    r_stats = retired.get("stats") or {}
    r_equity = retired.get("equity") or {}
    r_trips = retired.get("trades") or []
    recent = r_trips[-6:] if r_trips else trips[-6:]
    perp = perp or {}
    perp_positions = perp.get("positions") or []
    perp_stats = perp.get("stats") or {}
    # True when the spot book has been decommissioned: the payload then reports the
    # perp book as "today" and labels every spot number as frozen history.
    _spot_retired = SPOT_BOOK_RETIRED
    perp_open = [{
        "book": "X-PERP", "inst": p.get("inst"), "side": p.get("side"),
        "entry": p.get("entry_ref"), "notional_usd": p.get("notional_usd"),
        "upc_pct": p.get("upc"),
    } for p in perp_positions]
    payload_txt = json.dumps({
        "trading_state": {
            "mode": ENGINE["mode"],
            "live_capital": True,
            "halted": os.path.exists(os.path.join(BASE, "STOP")),
            "spot_book_status": ENGINE.get("spot_book", {}).get("status"),
            "spot_book_why": ENGINE.get("spot_book", {}).get("why"),
            "spot_open_positions": len(positions or []),
            "perp_open_positions": len(perp_positions),
            "open_positions": len(positions or []) + len(perp_positions),
            "perp_halted": perp.get("halted"),
            # day_trades = the LIVE book's count once spot is retired (spot's is frozen)
            "day_trades": (perp_stats.get("day_trades") if _spot_retired
                           else equity.get("day_trades")),
            "day_trades_book": "live X-Perp book" if _spot_retired else "spot book",
        },
        # these two are the RETIRED spot book's numbers — relabelled so they are
        # never read as current account state (live_equity above is the truth)
        "retired_spot_equity_series": r_equity,
        "retired_spot_stats": {k: r_stats.get(k) for k in ("trades_all", "win_rate_pct", "net_all_eur",
                                        "profit_factor", "fees_all_eur")},
        "perp_book": {
            "open_positions": perp_open,
            "usdc_collateral": perp.get("usdc_equity"),
            "last_call": perp.get("last_jev") and {
                "ts": perp["last_jev"].get("ts"), "choice": perp["last_jev"].get("choice"),
                "chosen_side": perp["last_jev"].get("chosen_side"),
                "conf": perp["last_jev"].get("conf")},
            "closed_trades": perp_stats.get("trades_all"),
            "realized_pct": perp_stats.get("net_all_pct"),
        },
        # LIVE decision stream = the perp book. This is the only thing to call
        # "the latest decision": the spot engine is retired and its last logged
        # call is a frozen historical artifact that would otherwise be narrated
        # as a current intent (it did exactly that before this split).
        "live_decision": (perp.get("last_jev") or {}) and {
            "book": "X-PERP (LIVE)",
            "ts": (perp.get("last_jev") or {}).get("ts"),
            "choice": (perp.get("last_jev") or {}).get("choice"),
            "inst": (perp.get("last_jev") or {}).get("chosen_inst"),
            "side": (perp.get("last_jev") or {}).get("chosen_side"),
            "conf": (perp.get("last_jev") or {}).get("conf"),
            "margin": (perp.get("last_jev") or {}).get("margin"),
            "noul": (perp.get("last_jev") or {}).get("noul"),
            "probabilities": (perp.get("last_jev") or {}).get("probabilities"),
            "gate_verdict": ((perp.get("last_jev") or {}).get("criteria") or {}).get("fire"),
        },
        # The retired spot book's FINAL logged call — explicitly labelled as
        # historical. Never describe it as the current decision or intent.
        "retired_spot_last_call": jev_latest and {
            "book": "SPOT (RETIRED — historical record only, not trading)",
            "ts": jev_latest["ts"], "choice": jev_latest["choice"],
            "inst": jev_latest["chosen_inst"], "conf": jev_latest["confidence"],
            "margin": jev_latest["margin"], "noul": jev_latest["noul"],
            "probabilities": jev_latest["probabilities"]},
        "live_equity": {"usdc_collateral": perp.get("usdc_equity"),
                        "eur_float": perp.get("eur_float"),
                        "account_total_usd": perp.get("account_total_usd"),
                        "capital_put_in": perp_stats.get("capital_usd"),
                        "realized_usd_all_time": perp_stats.get("net_all_usd"),
                        "realized_pct_all_time": perp_stats.get("net_all_pct"),
                        "today_trades": perp_stats.get("day_trades"),
                        "today_pnl_pct": perp_stats.get("day_pnl_pct")},
        "retired_spot_record": {
            "status": ENGINE.get("spot_book", {}).get("status"),
            "trades_closed": r_stats.get("trades_all"),
            "win_rate_pct": r_stats.get("win_rate_pct"),
            "net_eur": r_stats.get("net_all_eur"),
            "net_pct_of_launch": r_stats.get("net_all_pct"),
            "profit_factor": r_stats.get("profit_factor"),
            "fees_eur": r_stats.get("fees_all_eur"),
        },
        # spot closed trips, labelled as history
        "recent_trades_retired_spot": [
            {k: t[k] for k in ("inst", "upc", "net_eur", "reason", "close_ts") if k in t}
            for t in recent],
        "news": news[:6],
        "fear_greed": (sentiment or {}).get("fear_greed"),
        "funding_bps": jev_latest and jev_latest.get("funding_bps"),
    }, separators=(",", ":"))
    h = hashlib.sha256(payload_txt.encode()).hexdigest()
    age = time.time() - (cache.get("ts") or 0)
    if cache.get("hash") == h and age < COMMENTARY_MAX_AGE_S * 4:
        return cache.get("out")
    # The rate limit must only apply when nothing MATERIAL changed. As written it
    # returned the cached note whenever age < MIN_INTERVAL even with a different
    # hash, so a position opening left the note claiming "flat, no open positions"
    # for up to 5 minutes while the badge directly above it showed the live
    # position — two parts of the same screen contradicting each other. Position
    # count is the material-change signal; everything else (price drift, tokens)
    # still respects the interval so we don't hammer the LLM.
    n_pos_now = len(perp_open or []) + len(positions or [])
    material = (cache.get("n_pos") != n_pos_now) or (cache.get("breaked") != bool(
        (perp or {}).get("breaked")))
    if age < COMMENTARY_MIN_INTERVAL_S and cache.get("out") and not material:
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
        "You are the desk commentator for OBEKT TERMINAL, the Bloomberg-style observation dashboard of a "
        "LIVE autonomous crypto trading system trading REAL capital on OKX Europe (EEA), 5-min cycles. "
        "It runs ONE live book: X-PERPS — USD-settled MiFID expiry perps, two-way (LONG and SHORT), "
        "USDC collateral, 1x isolated. "
        "A second book, SPOT (EUR pairs, long-only), was RETIRED on 2026-10-15: trading_state."
        "spot_book_status says so and spot_book_why gives the reason. Its engine code and full trade "
        "history remain on the dashboard as a record, but it no longer trades — spot_open_positions will "
        "stay 0 permanently and that is EXPECTED, not a malfunction or an idle engine. "
        "Do not describe spot as live, paused, waiting or idle. If you mention it at all, describe it as "
        "retired with its final record. "
        "CRITICAL — read the payload keys by their labels: live_decision, live_equity and perp_book are "
        "the CURRENT system. Keys prefixed retired_spot_ (retired_spot_last_call, retired_spot_record, "
        "retired_spot_stats, retired_spot_equity_series, recent_trades_retired_spot) are FROZEN HISTORY. "
        "Never present a retired_spot_last_call as the latest decision or current intent — that was the "
        "last thing the spot engine logged before it was switched off, and it may be hours or days old. "
        "When you refer to 'the latest decision', mean live_decision (the X-Perp call). "
        "If live_decision has no inst, the model chose no_trade and the engine is standing aside — that is "
        "a real, correct state, not missing data. "
        "The dashboard displays telemetry read-only, but the ENGINE behind it places real orders: it is "
        "NEVER in demo, paper, showcase or read-only trading mode. The trading_state block in the snapshot "
        "is authoritative — halted=true means the kill switch is on; otherwise it is trading live. "
        "open_positions is now effectively perp_open_positions; the perp_book block "
        "lists each open X-Perp position with its side (long/short). "
        "Few or zero trades in a day is normal gate selectivity, not a mode change. "
        "The engine: deterministic Python gates scan the universe, momentum/depth gates filter, "
        "then the Jev System-One model returns TYPED decisions (probabilities + confidence, no free text, "
        "scored in BOTH directions per contract); "
        "exits are server-side OCO + trailing stops. News/sentiment/macro are injected into Jev's state.\n\n"
        "LIVE SYSTEM SNAPSHOT (UTC):\n" + payload_txt + "\n\n"
        "Write for sophisticated visitors who might hire the sysop to build one. Rules: "
        "reference ONLY facts in the snapshot, no invented numbers, no invented modes or states "
        "(never call it showcase/demo/read-only trading), no financial advice, terse terminal voice. "
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
            json.dump({"hash": h, "ts": time.time(), "out": out,
                       # recorded so the next build can tell a MATERIAL change
                       # (position opened/closed, breaker tripped) from noise and
                       # skip the rate limit only in that case.
                       "n_pos": n_pos_now, "breaked": bool((perp or {}).get("breaked"))}, f)
        return out
    except Exception as ex:
        print("LLM commentary failed: %s" % ex, file=sys.stderr)
        return cache.get("out") or {
            "desk_note": "Desk note unavailable — LLM relay offline. Engine telemetry below is live and unaffected.",
            "decision_line": "COMMENTARY FEED DEGRADED — SHOWING RAW TELEMETRY",
            "model": LLM_MODEL, "generated": _now().isoformat()}


def build_perp(shared_trips=None):
    """v4: two-way X-Perp book (separate engine, USDC collateral, LONG+SHORT).
    Reads perp_log.jsonl / perp_positions.json / perp_universe.json from BASE.
    Returns a compact block for a dedicated deck panel; None-safe if the perp
    engine has never run.

    `shared_trips` is the perp round-trip list already built by the shared
    matcher in build(). Passing it in avoids a SECOND independent open/close
    pairing of the same log — two matchers is how the panels end up disagreeing
    with each other, and it already harboured the sum-of-stake-% bug once.
    """
    events = read_perp_log()
    if not events:
        return None

    if shared_trips is not None:
        trips = shared_trips
    else:
        trips = build_round_trips(normalize_perp_events(events), "USD", PERP_CAPITAL_USD)
    # `net_usd` is the canonical key for this panel; the shared matcher names the
    # same value net_eur (a historical identifier meaning "book currency").
    for t in trips:
        t["net_usd"] = t.get("net_usd", t.get("net_eur"))
        t["notional_usd"] = t.get("notional_usd", t.get("stake_eur"))
    longs = [t for t in trips if t.get("side") == "long"]
    shorts = [t for t in trips if t.get("side") == "short"]
    wins = [t for t in trips if (t.get("net_usd") or 0) > 0]
    # Equity curve in % of CAPITAL PUT INTO THE PERP BOOK AS OF EACH TRADE — not a
    # sum of per-trade net_pct (that mixes $8 and $130 notionals and is meaningless).
    # Injections raise the denominator, so the consolidation never shows as a gain.
    cum, run = [], 0.0
    for t in sorted([x for x in trips if x.get("close_ts")], key=lambda x: x["close_ts"]):
        run += (t.get("net_usd") or 0)
        base = _perp_capital_at(t.get("close_ts"))
        cum.append(round(100 * run / base, 3) if base else 0.0)
    # all-time realized % uses the CURRENT capital base (money in the book now)
    net_all_usd_val = sum((t.get("net_usd") or 0) for t in trips)
    net_all_pct = round(100 * net_all_usd_val / PERP_CAPITAL_USD, 2) if PERP_CAPITAL_USD else None

    # TODAY for the live book: realized USD from trades closed today, as a % of
    # the engine's own day_equity_start (re-based whenever capital moves, so this
    # stays meaningful across the consolidation). Unrealized on an open position
    # is NOT included — realized only, matching how the spot book reported.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_trips = [t for t in trips if (t.get("close_ts") or "").startswith(today_utc)]
    day_net_usd = sum((t.get("net_usd") or 0) for t in day_trips)

    # live collateral + open positions from the perp engine's own files
    pmeta = _load_json(os.path.join(BASE, "perp_positions.json"), {}) or {}
    puniv = _load_json(os.path.join(BASE, "perp_universe.json"), {}) or {}
    prank = _load_json(os.path.join(BASE, "perp_risk_state.json"), {}) or {}
    psnap = _load_json(os.path.join(BASE, "perp_status.json"), {}) or {}
    positions = []
    # live rows from perp_status.json carry last/upc/liq_px (engine has creds;
    # the container does not) — prefer them, fall back to raw meta.
    live_by = {}
    for p in (psnap.get("positions") or []):
        if isinstance(p, dict) and p.get("inst"):
            live_by[p["inst"]] = p
    for inst, m in pmeta.items():
        if not isinstance(m, dict):
            continue
        lv = live_by.get(inst, {})
        positions.append({"inst": inst, "side": m.get("side"),
                          "entry_ref": m.get("entry_ref"), "contracts": m.get("contracts"),
                          "notional_usd": m.get("notional_usd") or lv.get("notional_usd"),
                          "bracket": m.get("bracket"),
                          "hwm_pct": m.get("hwm_pct"), "algo_ok": m.get("algo_ok"),
                          "opened_ts": m.get("opened_ts"), "jev_conf": m.get("jev_conf"),
                          "last": lv.get("last"), "upc": lv.get("upc"),
                          "liq_px": lv.get("liq_px"), "upl": lv.get("upl"),
                          "lever": lv.get("lever")})
    last_jev = None
    for e in reversed(events):
        if e.get("event") == "perp_jev":
            last_jev = {"ts": e.get("ts"), "choice": e.get("choice"), "conf": e.get("conf"),
                        "margin": e.get("margin"), "noul": e.get("noul"),
                        "chosen_inst": e.get("chosen_inst"), "chosen_side": e.get("chosen_side"),
                        "probabilities": e.get("probabilities") or {},
                        "pairs": e.get("pairs") or [],
                        "collateral_usdc": e.get("collateral_usdc"),
                        "budget_usd": e.get("budget_usd"),
                        "tokens": {"in": e.get("input_tokens"), "out": e.get("output_tokens")}}
            # Gate verdict for the LIVE book, scored against the PERP engine's own
            # bars (parsed from okx_perp.py source — never hardcoded, and never
            # the spot engine's, whose CONF_FLOOR differs at 0.50 vs 0.40).
            # Same fire rule the engine enforces:
            #   edge = (margin >= MARGIN_BAR or conf >= CONF_BAR) and conf >= CONF_FLOOR
            #   fire = edge and noul >= NOUL_BAR
            pMBAR, pCBAR, pCFLOOR, pNBAR = _engine_bars("okx_perp.py")
            p_conf = e.get("conf")
            p_margin = e.get("margin")
            p_noul = e.get("noul")
            na = not e.get("chosen_inst")
            last_jev["criteria"] = {
                "margin_bar": pMBAR, "conf_bar": pCBAR,
                "conf_floor": pCFLOOR, "noul_bar": pNBAR,
                "margin_ok": None if na else (p_margin is not None and p_margin >= pMBAR),
                "conf_ok": None if na else (p_conf is not None and p_conf >= pCBAR),
                "noul_ok": None if na else (p_noul is not None and p_noul >= pNBAR),
                "confidence": p_conf,
                "fire": False if na else bool(
                    ((p_margin is not None and p_margin >= pMBAR) or
                     (p_conf is not None and p_conf >= pCBAR))
                    and (p_conf is not None and p_conf >= pCFLOOR)
                    and (p_noul is not None and p_noul >= pNBAR)),
                "book": "x-perp",
                "engine_source": "okx_perp.py",
            }
            break
    return {
        # version read from the perp engine source (never hardcoded — it drifted
        # to a stale "v4.0-perp" string the first time this block was written)
        "engine": _perp_engine_version(),
        "venue": "OKX EUROPE X-PERPS (MiFID)",
        "halted": os.path.exists(os.path.join(BASE, "STOP")) or os.path.exists(os.path.join(BASE, "STOP_PERP")),
        "breaked": prank.get("breaked"), "day": prank.get("day"),
        "usdc_equity": psnap.get("usdc_equity"),
        # WHOLE-ACCOUNT truth from the engine snapshot. Before this the deck read
        # cash from the retired spot engine's last jev event, frozen at the
        # pre-conversion 100.73 EUR, and the desk note repeated it as current.
        "eur_float": psnap.get("eur_float"),
        "account_total_usd": psnap.get("account_total_usd"),
        "collateral_ccy": psnap.get("collateral_ccy") or "USDC",
        "status_ts": psnap.get("ts"),
        "universe": puniv.get("pairs", []),
        "universe_selected": puniv.get("selected", []),
        "universe_scanned": puniv.get("scanned"),
        "universe_scanned_iso": (datetime.fromtimestamp(puniv.get("scanned"), timezone.utc).isoformat()
                                  if puniv.get("scanned") else None),
        "positions": positions,
        "last_jev": last_jev,
        "trades": trips[-20:][::-1],
        "stats": {
            "trades_all": len(trips),
            "win_rate_pct": round(100 * len(wins) / len(trips), 1) if trips else None,
            "longs": len(longs), "shorts": len(shorts),
            "net_all_pct": net_all_pct,
            "net_all_usd": round(net_all_usd_val, 2),
            "capital_usd": PERP_CAPITAL_USD,
            "day_trades": len(day_trips),
            "day_net_usd": round(day_net_usd, 2),
            # % of the engine's own day_equity_start (re-based on capital moves)
            "day_pnl_pct": (round(100 * day_net_usd / float(prank.get("day_equity_start")), 2)
                            if prank.get("day_equity_start") else None),
            "day_equity_start": prank.get("day_equity_start"),
            "breaked": prank.get("breaked"),
        },
        "cum_pnl_pct": cum,
    }


def build():
    t0 = time.time()
    # TWO event streams. `events` = the retired spot EUR book (frozen history).
    # `perp_events` = the LIVE X-Perp book, normalized onto the same generic
    # event shapes so one set of tested builders serves both. Every non-retired
    # panel must read the LIVE book: pointing them at the spot log makes the deck
    # present dead-engine data as current (it did — 15 panels were frozen at the
    # moment spot was disabled while claiming to be live).
    events = read_log()
    perp_raw = read_perp_log()
    perp_events = normalize_perp_events(perp_raw)
    live_events = perp_events if (SPOT_BOOK_RETIRED and perp_events) else events
    live_ccy = "USD" if (SPOT_BOOK_RETIRED and perp_events) else "EUR"
    live_base = PERP_CAPITAL_USD if live_ccy == "USD" else LAUNCH_EUR

    trips = build_round_trips(live_events, live_ccy, live_base)
    jev_events = [e for e in live_events if e.get("event") == "jev"]
    # dossiers for the trades shown in the ledger (cached; only new ones rebuild).
    # Debriefs cost one LLM call each — capped per build so 40 cached dossiers backfill
    # gradually without hammering the relay; new closed trades get one immediately.
    debrief_budget = DEBRIEFS_PER_RUN
    candle_budget = DOSSIER_CANDLE_BACKFILL
    for t in trips[-40:][::-1]:
        try:
            dos, used_d, used_c = build_dossier(t, live_events, jev_events, debrief_budget, candle_budget)
            debrief_budget -= used_d
            candle_budget -= used_c
            if dos.get("debrief"):
                t["debrief"] = dos["debrief"]
        except Exception as ex:
            print("dossier failed %s: %s" % (t.get("tid"), ex), file=sys.stderr)
    stats = build_stats(trips, live_events, live_ccy, live_base,
                        capital_base_fn=(_perp_capital_at if live_ccy == "USD" else None))
    equity = build_equity(live_events, trips)
    jev_latest = build_jev_latest(live_events)
    jev_hist = build_jev_history(live_events)
    tape = build_tape(live_events)
    # the LIVE book's universe cache (perp_universe.json when spot is retired)
    universe = _load_json(os.path.join(BASE, "perp_universe.json" if live_ccy == "USD" else "universe.json"), {}) or {}
    funnel = build_funnel(live_events, universe)
    scatter = build_scatter(live_events)
    social = _load_json(os.path.join(BASE, "social.json"), {}) or {}
    catalyst = _load_json(os.path.join(BASE, "catalyst.json"), {}) or {}
    risk = _load_json(os.path.join(BASE, "risk_state.json"), {}) or {}
    positions_raw = _load_json(os.path.join(BASE, "positions.json"), {}) or {}

    positions_raw_keys = [k for k in (positions_raw or {}) if isinstance((positions_raw or {}).get(k), dict)]
    tickers, px_map = fetch_tickers(extra_insts=positions_raw_keys)
    positions = build_positions(positions_raw, px_map)

    # Perp panel reuses the SAME round-trip matcher as every other live panel
    # (live_events are already the normalized perp stream when spot is retired).
    # Passing the shared list means one open/close pairing for the whole deck.
    perp = build_perp(shared_trips=(trips if live_ccy == "USD" else None))

    news = catalyst.get("catalysts", [])
    # MACRO: only the retired spot engine injected econ-calendar events into the
    # model state; the live X-Perp engine does not. Showing spot's last frozen
    # macro snapshot under a live-looking heading would imply context the live
    # book never sees, so publish the source explicitly and let the UI say so.
    macro = {"source": None, "upcoming": [], "surprises": []}
    if SPOT_BOOK_RETIRED:
        macro["source"] = "not-injected-into-live-book"
        macro["note"] = ("The live X-Perp engine injects funding + multi-timeframe momentum only. "
                         "Econ-calendar / macro events were injected by the retired spot engine, "
                         "so there is no live macro context to show.")
    elif jev_latest:
        st = None
        for e in reversed(events):
            if e.get("event") == "jev":
                st = e.get("state") or {}
                break
        macro = {"source": "spot-engine-jev-state",
                 "upcoming": (st or {}).get("macro_events_24h", []),
                 "surprises": (st or {}).get("macro_released_3h_surprises", [])}

    sentiment = {
        "fear_greed": social.get("fear_greed"),
        "trending": social.get("trending", []),
        "coins": social.get("coins", {}),
        "updated": social.get("updated"),
        "funding_bps": jev_latest.get("funding_bps") if jev_latest else None,
    }

    # Retired spot book: publish its FINAL record, derived from the log — not
    # The retired spot book's FINAL record must be computed from the SPOT log.
    # `stats`/`trips` above now describe the LIVE book, so reusing them here would
    # publish perp numbers as spot's final ledger — a wrong attribution, not a
    # missing one. Compute the retired record from its own frozen stream, and give
    # the retired-labelled panels (PERFORMANCE / JEV CALL / universe) their own
    # frozen blocks so they can never render live perp data under a "SPOT RECORD"
    # heading.
    spot_trips = build_round_trips(events, "EUR", LAUNCH_EUR)
    spot_stats = build_stats(spot_trips, events, "EUR", LAUNCH_EUR) if spot_trips else {}
    spot_jev_latest = build_jev_latest(events) if SPOT_BOOK_RETIRED else None
    spot_universe = (_load_json(os.path.join(BASE, "universe.json"), {}) or {}) \
        if SPOT_BOOK_RETIRED else {}
    spot_equity = build_equity(events, spot_trips) if SPOT_BOOK_RETIRED else {}
    ENGINE["spot_book"]["trades_closed"] = spot_stats.get("trades_all")
    ENGINE["spot_book"]["net_eur"] = spot_stats.get("net_all_eur")
    ENGINE["spot_book"]["net_pct"] = spot_stats.get("net_all_pct")
    ENGINE["spot_book"]["profit_factor"] = spot_stats.get("profit_factor")
    ENGINE["spot_book"]["win_rate_pct"] = spot_stats.get("win_rate_pct")
    ENGINE["spot_book"]["fees_eur"] = spot_stats.get("fees_all_eur")

    # One frozen record, used by BOTH the retired panels and the desk-note payload.
    retired_spot = {
        "trades": spot_trips[-40:][::-1],
        "stats": spot_stats,
        "cum_pnl_pct": spot_stats.get("cum_pnl_pct"),
        "jev_latest": spot_jev_latest,
        "universe": spot_universe,
        "equity": spot_equity,
    }

    desk = build_commentary(jev_latest, stats, equity, news, sentiment, trips,
                            positions, perp, retired=retired_spot)

    data = {
        "generated": _now().isoformat(),
        # Heartbeat of the LIVE book. Reading the retired spot log here made the
        # deck permanently flash "FEED STALE — ENGINE MAY BE PAUSED" while the
        # live engine was cycling normally: a false alarm that trains visitors
        # (and me) to ignore real staleness.
        "last_event_ts": live_events[-1].get("ts") if live_events else (
            events[-1].get("ts") if events else None),
        "live_ccy": live_ccy,
        "live_book": "X-PERPS" if live_ccy == "USD" else "SPOT",
        # Live engine constants parsed from source for the ARCHITECTURE panel.
        "params": _live_params(),
        "build_ms": int((time.time() - t0) * 1000),
        "engine": ENGINE,
        "contact": CONTACT_LINE,
        # The LIVE book's cooldown state, windows parsed from okx_perp.py. The
        # `risk` block below is the retired spot book's — kept for the record.
        "live_risk": _perp_cooldowns(),
        "equity": equity,
        "stats": stats,
        # Retired spot book: its own frozen ledger, explicitly namespaced so no
        # panel can mistake it for live state. Same object the desk note was given
        # — one definition, so the two can never disagree.
        "retired_spot": retired_spot,
        "funnel": funnel,
        "scatter": scatter,
        "positions": positions,
        "perp": perp,
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
    tmp = os.path.join(STATE_DIR, "data.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(STATE_DIR, "data.json"))
    tmp = os.path.join(STATE_DIR, "data.js.tmp")
    with open(tmp, "w") as f:
        f.write("window.TT_DATA = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    os.replace(tmp, os.path.join(STATE_DIR, "data.js"))
    # regenerate the OG share card, throttled (numbers don't need 45s churn)
    try:
        og_path = os.path.join(STATE_DIR, "og.png")
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
