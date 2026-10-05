#!/usr/bin/env python3
"""Keyless social/regime sentiment feed -> social.json (15-min disk cache).

Why this exists: OKX's orbit news + currency-sentiment endpoints (the data behind
`okx news coin-sentiment`) are NOT deployed for EEA accounts — verified 09-30 by
extracting the real paths from @okx_ai/okx-trade-mcp 1.4.8 dist/index.js
(/api/v5/orbit/news-search, /currency-sentiment-query, /currency-sentiment-ranking):
eea.okx.com -> 404 (module absent), www.okx.com -> 401 code 50119 (EEA API key
rejected), and the okx CLI's own log shows the same 404 via OAuth. So we replace
that layer with keyless equivalents that are strictly more usable here: per-coin
numeric sentiment (CoinGecko community votes = analogue of OKX bullishRatio),
attention/trending rank, and market-wide Fear & Greed.

UNIVERSE IS DYNAMIC (09-30): okx_jev.py re-selects its tradeable pairs each cycle
from all ~273 EUR pairs and writes them to universe.json. This feed reads that
file, so sentiment always covers what the engine is actually considering — no
stale hardcoded pair list. Falls back to FALLBACK_PAIRS if universe.json is
missing/unreadable (engine offline, first run).

Design rules (this runs inside a 5-min trading cycle):
  - NEVER raise, NEVER block: every fetch wrapped, total budget SOCIAL_TIMEOUT_S.
  - 15-min cache: CoinGecko free tier is rate-limited and this data moves slowly.
  - Per-coin sentiment needs one call per coin (the bulk /coins/markets endpoint
    does NOT return sentiment_votes_* — verified 09-30), so cap at MAX_COINS.
  - symbol -> coingecko-id map from ONE /coins/markets call, cached 12h in
    cg_symbol_map.json (verified 09-30: resolves 16/17 liquid EUR movers).
  - Output is TINY by design (<900 chars) — it goes into the Jev prompt where
    ~1.9 chars = 1 token.
  - No LLM in this path: numbers/rankings only, same policy as news_feed.py.
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "social.json")
UNI = os.path.join(HERE, "universe.json")
MAPFILE = os.path.join(HERE, "cg_symbol_map.json")
COINCACHE = os.path.join(HERE, "cg_coin_cache.json")
BULKCACHE = os.path.join(HERE, "cg_bulk_cache.json")
MAX_AGE_MIN = 15
MAP_AGE_H = 12
BULK_AGE_MIN = 12              # bulk market cache; poll cache is COIN_AGE_MIN
SOCIAL_TIMEOUT_S = 25          # hard budget for the whole refresh
MAX_POLL_CALLS = 3             # per-run cap on the expensive /coins/{id} poll calls
COIN_AGE_MIN = 60              # poll cache: sentiment votes are a slow-moving poll
UA = {"User-Agent": "Mozilla/5.0 (jev-pilot social feed)"}

# Used only if universe.json is missing (engine offline / very first run).
FALLBACK_PAIRS = ["NEAR-EUR", "ZEC-EUR", "SUI-EUR", "LINK-EUR", "BTC-EUR", "ETH-EUR"]
# Hand-verified ids where the top-250 market-cap guess is wrong or absent.
ID_OVERRIDES = {"NIGHT": "midnight-3", "PUMP": "pump-fun", "XPL": "plasma"}
TRENDING_CAP = 10


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def read_universe():
    """Pairs the engine is currently considering (written by okx_jev.py)."""
    try:
        d = json.load(open(UNI))
        p = d.get("pairs") if isinstance(d, dict) else d
        if isinstance(p, list) and p:
            return [x for x in p if isinstance(x, str)]
    except Exception:
        pass
    return FALLBACK_PAIRS


def symbol_map(pairs):
    """instId symbol -> coingecko id. One cached call; never fatal."""
    syms = sorted({p.split("-")[0] for p in pairs})
    cache = {}
    try:
        c = json.load(open(MAPFILE))
        if time.time() - c.get("fetched", 0) < MAP_AGE_H * 3600:
            cache = c.get("map", {})
    except Exception:
        pass
    if [s for s in syms if s not in cache and s not in ID_OVERRIDES]:
        try:
            rows = _get("https://api.coingecko.com/api/v3/coins/markets"
                        "?vs_currency=eur&order=market_cap_desc&per_page=250&page=1",
                        timeout=15)
            for r in rows:
                s = (r.get("symbol") or "").upper()
                if s and s not in cache:
                    cache[s] = r.get("id")
        except Exception as e:
            print(f"symbol map refresh failed: {str(e)[:90]}", file=sys.stderr)
        if cache:
            try:
                json.dump({"fetched": time.time(), "map": cache}, open(MAPFILE, "w"))
            except Exception:
                pass
    m = {}
    for s in syms:
        cg = ID_OVERRIDES.get(s) or cache.get(s)
        if cg:
            m[s] = cg
    return m


def fear_greed():
    """0-100 market mood + 7-day context. A long streak in one band is itself a
    contrarian signal (a week pinned in 'Greed' = crowded, squeeze-prone)."""
    d = _get("https://api.alternative.me/fng/?limit=8&format=json")
    rows = d.get("data", [])
    if not rows:
        return None
    vals = [int(r["value"]) for r in rows]

    def band(v):
        return "greed" if v >= 55 else ("fear" if v <= 45 else "neutral")

    b0 = band(vals[0])
    streak = 0
    for v in vals:
        if band(v) == b0:
            streak += 1
        else:
            break
    return {"value": vals[0], "label": rows[0].get("value_classification"),
            "d1": vals[1] if len(vals) > 1 else None,
            "d7_avg": round(sum(vals) / len(vals), 1), "streak_days": streak}


def trending():
    """CoinGecko trending = attention spike detector; rank order matters."""
    d = _get("https://api.coingecko.com/api/v3/search/trending")
    out = []
    for c in d.get("coins", [])[:TRENDING_CAP]:
        sym = (c.get("item", {}).get("symbol") or "").upper()
        if sym:
            out.append(sym)
    return out


def bulk_market(cg_ids):
    """ONE call for all coins: chg24h, volume, ath distance, market-cap rank.
    CoinGecko's bulk /coins/markets accepts an ids list and returns everything
    except sentiment_votes_* — so this covers the numeric context cheaply and
    the expensive per-coin poll is only needed for the vote percentages.
    Result cached BULK_AGE_MIN (free tier 429s on back-to-back calls — verified
    09-30). Returns {cg_id: {...}} (never raises)."""
    if not cg_ids:
        return {}
    try:
        c = json.load(open(BULKCACHE))
        if (time.time() - c.get("ts", 0) < BULK_AGE_MIN * 60
                and set(cg_ids) <= set(c.get("data", {}))):
            return c["data"]
    except Exception:
        pass
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=eur&ids="
           + ",".join(cg_ids) + "&order=market_cap_desc&price_change_percentage=24h")
    try:
        rows = _get(url, timeout=15)
    except Exception as e:
        print(f"bulk_market failed: {str(e)[:90]}", file=sys.stderr)
        # stale cache still beats nothing (429 recovery)
        try:
            return json.load(open(BULKCACHE)).get("data", {})
        except Exception:
            return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        cid = r.get("id")
        if not cid:
            continue
        d = {}
        chg = r.get("price_change_percentage_24h_in_currency")
        if chg is None:
            chg = r.get("price_change_percentage_24h")
        if chg is not None:
            d["chg24h_pct"] = round(float(chg), 2)
        vol = r.get("total_volume")
        if vol:
            d["vol24h_meur"] = round(float(vol) / 1e6, 1)
        ath = r.get("ath_change_percentage")
        if ath is not None:
            d["off_ath_pct"] = round(float(ath), 1)
        mcr = r.get("market_cap_rank")
        if mcr:
            d["mcap_rank"] = int(mcr)
        hi, lo = r.get("high_24h"), r.get("low_24h")
        px = r.get("current_price")
        if hi and lo and px and hi > lo:
            # where in today's range we sit: 0=at low, 100=at high
            d["day_range_pos"] = round((float(px) - float(lo)) / (float(hi) - float(lo)) * 100)
        if d:
            out[cid] = d
    if out:
        try:
            json.dump({"ts": time.time(), "data": out}, open(BULKCACHE, "w"),
                      separators=(",", ":"))
        except Exception:
            pass
    return out


def coin_social(cg_id):
    """Per-coin community poll (up% ~= OKX bullishRatio) + context.
    off_ath = overhead supply a rally must absorb; vol24h = whether the move is
    backed by real turnover or is a thin-book artifact."""
    d = _get("https://api.coingecko.com/api/v3/coins/" + cg_id +
             "?localization=false&tickers=false&market_data=true"
             "&community_data=false&developer_data=false&sparkline=false", timeout=10)
    md = d.get("market_data", {})
    out = {}
    up = d.get("sentiment_votes_up_percentage")
    if up is not None:
        out["sent_up_pct"] = round(float(up), 1)
    dn = d.get("sentiment_votes_down_percentage")
    if dn is not None:
        out["sent_dn_pct"] = round(float(dn), 1)
    wl = d.get("watchlist_portfolio_users")
    if wl:
        out["watchlists_k"] = round(int(wl) / 1000)
    chg = (md.get("price_change_percentage_24h_in_currency") or {}).get("eur")
    if chg is None:
        chg = md.get("price_change_percentage_24h")
    if chg is not None:
        out["chg24h_pct"] = round(float(chg), 2)
    ath = (md.get("ath_change_percentage") or {}).get("eur")
    if ath is None:
        ath = md.get("ath_change_percentage")
    if ath is not None:
        out["off_ath_pct"] = round(float(ath), 1)
    vol = (md.get("total_volume") or {}).get("eur")
    if vol:
        out["vol24h_meur"] = round(float(vol) / 1e6, 1)
    return out


def build(pairs):
    """Fetch everything, tolerate per-source failure, return the compact dict."""
    t0 = time.time()
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    try:
        fg = fear_greed()
        if fg:
            out["fear_greed"] = fg
    except Exception as e:
        print(f"fear_greed failed: {str(e)[:90]}", file=sys.stderr)
    try:
        tr = trending()
        if tr:
            out["trending"] = tr
    except Exception as e:
        print(f"trending failed: {str(e)[:90]}", file=sys.stderr)
    smap = symbol_map(pairs)
    unmapped = [p.split("-")[0] for p in pairs if p.split("-")[0] not in smap]
    if unmapped:
        out["coins_unmapped"] = unmapped[:8]
    # Coverage strategy (CoinGecko free tier 429s after ~5-15 calls/min —
    # learned 09-30): ONE bulk /coins/markets call gives every coin its numeric
    # context; the per-coin poll (the only source of sentiment_votes_*) is
    # budget-capped per run and cached for COIN_AGE_MIN, refreshing a few coins
    # at a time so all pairs converge to full coverage over successive cycles.
    coins = {}
    cache = {}
    try:
        cache = json.load(open(COINCACHE))
    except Exception:
        cache = {}
    tr_list = out.get("trending", [])
    ids = [smap[p.split("-")[0]] for p in pairs if p.split("-")[0] in smap]
    bulk = bulk_market(ids)
    polls = 0
    for inst in pairs:
        sym = inst.split("-")[0]
        cg = smap.get(sym)
        if not cg:
            continue
        s = dict(bulk.get(cg, {}))          # numeric context: every coin, 1 call
        ent = cache.get(cg)
        fresh = ent and (t0 - ent.get("ts", 0)) < COIN_AGE_MIN * 60
        if not fresh and polls < MAX_POLL_CALLS and time.time() - t0 < SOCIAL_TIMEOUT_S:
            try:
                poll = coin_social(cg)       # adds sent_up_pct / watchlists_k
                if poll:
                    cache[cg] = {"ts": t0, "data": poll}
                    polls += 1
            except Exception as e:
                print(f"poll {cg} failed: {str(e)[:90]}", file=sys.stderr)
                # 429 = stop polling this run; cached values still usable below
                if "429" in str(e):
                    polls = MAX_POLL_CALLS
            time.sleep(1.5)
        ent = cache.get(cg)
        if ent and ent.get("data"):
            s.update({k: v for k, v in ent["data"].items() if k not in s or k in ("sent_up_pct", "watchlists_k")})
        if sym in tr_list:
            s["trending_rank"] = tr_list.index(sym) + 1
        if s:
            coins[inst] = s
    try:
        json.dump(cache, open(COINCACHE, "w"), separators=(",", ":"))
    except Exception:
        pass
    if coins:
        out["coins"] = coins
    return out


def main():
    force = "--force" in sys.argv
    if not force and os.path.exists(OUT):
        try:
            if (time.time() - os.path.getmtime(OUT)) / 60 < MAX_AGE_MIN:
                return   # fresh enough
        except OSError:
            pass
    pairs = read_universe()
    data = build(pairs)
    # Never publish a useless file: keep the old one if we got nothing new
    if not data.get("fear_greed") and not data.get("coins") and not data.get("trending"):
        print("social refresh produced nothing, keeping old file", file=sys.stderr)
        return
    tmp = OUT + ".tmp"
    json.dump(data, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"social refreshed: universe={len(pairs)} fg={bool(data.get('fear_greed'))} "
          f"trending={len(data.get('trending', []))} coins={list(data.get('coins', {}))}")


if __name__ == "__main__":
    main()
