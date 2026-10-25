# `engine/` — the autonomous traders

**One live book, three engine files.** As of 2026-10-25 the live X-Perp book is
driven by `okx_mom.py` — a deterministic cross-sectional momentum strategy that
replaced `okx_perp.py`'s LLM-signal decision path after that signal's
pre-committed kill-gate review found no edge (79 closes, net −1.5 USDC — the
full honest record is on the dashboard). The spot EUR engine was retired
2026-10-15 (timer disabled, source and ledger kept). The plumbing is shared:
`okx_mom.py` imports `okx_perp.py` (execution: entries with attached OCO,
realized-fee-capturing closes, state/log files, risk breaker), which imports
`okx_jev.py` (signed API client, market data). Each engine is a one-shot
process called by a scheduler (cron/systemd timer) every 5 minutes — no
daemon needed. They are isolated by design: separate collateral, state files,
kill-switches and daily breakers.

> ⚠️ **This trades real money on real exchanges.** Read [`../DISCLAIMER.md`](../DISCLAIMER.md).
> Start in demo/paper if your venue offers it. Nothing here is advice.

## Files

| file | what it is |
|---|---|
| `okx_mom.py` | **LIVE book — XS MOMENTUM (v5.0-mom, two-way, USD-settled X-Perps).** Deterministic, no LLM in the loop: rank the ≤30 most liquid XPERPs by 14-day return, long the strongest 2 / short the weakest 2 ($25/leg, 1× isolated), full rebalance every 24h, 1.5% protective stop (server OCO + poll backup). Selected from a 100-day OHLC backtest with intrabar stops and an out-of-sample split (+0.86%/d net, both halves positive). ~340 lines, imports `okx_perp`. |
| `okx_perp.py` | **Execution host + retired Jev-signal book (X-PERPS two-way).** Its Jev LLM decision path is retired as the live signal-maker (kill-gate 2026-10-25: 111 closes, −4.47 USDC, no edge); the file stays load-bearing twice over: `okx_mom.py` imports its execution machinery (open/close with attached OCO + realized fees, meta/log/state files, breaker), and its `--dry-run` paper mode carries true shadow bookkeeping (persisted positions, virtual fills/fees, fixed paper bankroll) for validating future candidates without capital. ~1000 lines. |
| `okx_jev.py` | **RETIRED book — SPOT (long-only, EUR pairs).** Kept for two reasons: it is the honest 132-trade record of the first strategy, and `okx_perp.py` imports it as a library (signed API, market data, Jev client, `balances()`). The whole engine: universe scan → deterministic gates → Jev decision → risk veto → execution → position management. ~1600 lines, stdlib only (no pip deps). |
| `news_feed.py` | Keyless RSS catalyst feed (Cointelegraph/CNBC/Yahoo Finance) → `catalyst.json`, 30-min freshness guard. Called by spot `cycle`. |
| `social_feed.py` | Keyless sentiment: alternative.me Fear&Greed (+ streak), CoinGecko trending + per-coin community polls, vs-EUR market context → `social.json`, 15-min cache. Called by spot `cycle`. |

Each engine writes its own log/state/caches next to itself — point
`OKX_TERMINAL_BASE` at that directory if you keep it elsewhere. The perp engine
requires an OKX EEA account with **X-Perps unlocked** (appropriateness assessment
passed → account level 2) and USDC collateral; without that, derivative orders
return error 50124/51010 and it stays flat. See
[`../skills/okx-trading/SKILL.md`](../skills/okx-trading/SKILL.md).

## Credentials

Two sets, both **never hardcoded** — JSON files or env vars (env wins):

```bash
# exchange (OKX EEA — create a key with read + trade perms, IP-whitelist it)
# file: ~/.config/okx/credentials.json  {"api_key":"***","secret":"***","passphrase":"***","base":"https://eea.okx.com"}
# or env:
export OKX_API_KEY=*** OKX_SECRET=*** OKX_PASSPHRASE=***

# decision model (TypeSafe Jev — https://typesafe.ai, ~$0.04/M input tokens)
# file: ~/.config/typesafe/credentials.json  {"api_key":"***"}
# or env:
export TYPESAFE_API_KEY=***
```

Missing creds → the engine exits with a setup message instead of crashing.
EEA accounts **must** use `https://eea.okx.com` (keys are site-scoped).

## Modes

```bash
python3 okx_jev.py status    # account/positions/pending-algos summary, no trades
python3 okx_jev.py cycle     # ONE full cycle — this is what the scheduler runs
python3 okx_jev.py decide    # just the entry-decision path once
python3 okx_jev.py manage    # just position management once (stops/trails/time-stops)
```

A `cycle` is: refresh news+social feeds (with guards) → sweep stables to EUR →
manage open positions → scan the universe → run momentum gates → build the Jev
state → ask Jev → apply the decision bar → re-check the live order book →
maybe open one position. Every step logs a JSONL event to `okx_jev_log.jsonl`
— that file is the terminal's data source and your audit trail.

### Scheduling

The engine is one-shot by design — a scheduler calls `cycle` every 5 minutes.

```cron
*/5 * * * * cd /path/to/engine && /usr/bin/python3 okx_jev.py cycle >> cycle.log 2>&1
```

**Prefer a systemd timer** (this is what we run in production): `Persistent=true`
catches up a run missed while the box was down, journald collects the logs, and
it survives reboot once `enabled`. Full unit files are in the main
[README → Run it → step 4](../README.md#b-the-full-live-system-your-capital-your-keys-your-responsibility).

### Kill switch

```bash
touch engine/STOP     # halts NEW entries immediately; management of open positions continues
rm engine/STOP        # resume (a daily-breaker STOP also auto-clears at UTC midnight)
```

## The strategy surface (tune these)

All at the top of `okx_jev.py`, each with a comment explaining the measured
reasoning behind the value. **These are the live production values** — the deck's
`DECISION GATES` panel parses them from source so the UI can never drift from what
the engine actually enforces.

| constant | default | meaning |
|---|---|---|
| `MAX_JEV_PAIRS` | 5 | how many gate-survivors the model sees (prompt-size bound) |
| `EDGE_MULT` | 4.0 | take-profit ≥ 4× all-in round-trip cost — the fee-wall rule (7× was tried 10-05..08 and reverted: every wide-TP config was net-negative) |
| `MIN_TARGET_PCT` / `MAX_TARGET_PCT` | 1.5 / 3.0 | TP clamp — a scalp must be reachable in ~90 min |
| `SL_COST_MULT` | 1.75 | stop anchored to **cost** (spread+fees), not to TP — decoupling these fixed a 10-07 avg-loss-doubling regression |
| `TRAIL_ARM_RATIO` / `TRAIL_GIVEBACK_RATIO` / `TRAIL_FLOOR_RATIO` | 0.40 / 0.23 / 0.07 | trailing exit: arm at 40% of TP, exit after giving back 23% from HWM, never below 7% |
| `MARGIN_BAR` | 0.30 | p(chosen) − p(no_trade) from Jev's typed probabilities |
| `CONF_BAR` / `CONF_FLOOR` | 0.50 / 0.50 | OR-path certainty bar, and a **hard floor** (v3.8: sub-0.50 conf trades lost €9.89 at 29% WR) |
| `NOUL_BAR` | 0.45 | Jev's P(price rises ≥ trail-arm within 45 min) floor |
| `MAX_OPEN` / `MAX_ENTRIES_PER_CYCLE` | 1 / 1 | v3.9: full bankroll per trade, one quality entry per cycle |
| `COOLDOWN_MIN` / `WIN_COOLDOWN_MIN` | 20 / 10 | per-pair lockout after a loss / after a win (a TP exit marks the local top) |
| `TIME_STOP_MIN` / `STALE_EXIT_MIN` | 30 / 60 | close losers after 30m, flat positions after 60m |
| `UNIVERSE_EXCLUDE` | {NEAR-EUR} | v3.10: the only instrument-level filter that held positive out-of-sample (NEAR bled −€6.09 at 27% WR) |
| `DAILY_LOSS_PCT` | 8.0 | software-failure backstop; auto-clears at UTC roll |
| `MIN_STAKE_EUR` | 10.0 | venue minimum sanity floor |

Sizing: stake = all free EUR / open slots. Entry: maker-first `post_only`; **no
IOC chase** (v3.8 — taker-chasing was the worst cost bucket; unfilled = no trade,
the signal can re-fire next cycle). Exits: server-side OCO is the first line (they
fire even if this machine dies); the poll loop owns trailing, time-stops and the
fallback stop. Unhurried rotation exits (time-stop/stale) sell `post_only` as maker
to halve their fee (v3.10); urgent exits stay market.

### The X-Perp book (`okx_perp.py`)

Same shape, two-way. Key constants at its top:

| constant | default | meaning |
|---|---|---|
| `PERP_LEV` | 1 | isolated leverage; 1× = margin equals notional (liquidation ~99% away) |
| `MAX_NOTIONAL_FRAC` / `MAX_NOTIONAL_USD` | 0.98 / 1000 | fraction of USDC equity deployed per position — **full-capital by operator directive**. Was 0.70: a liquidation buffer that protected against a risk this book doesn't carry at 1× isolated (measured liqPx ~98% away; the stop, not liquidation, defines per-trade risk). 0.98 is the ceiling because posted margin + fees must fit inside equity. The `USD` term is a bug guard against a garbage equity read, not a capital cap. Re-introduce a buffer only if `PERP_LEV` goes above 1. |
| `PERP_MAKER_PCT` / `PERP_TAKER_PCT` | 0.02 / 0.05 | X-Perp fees — ~0.07% round trip vs spot's ~0.3% |
| `MIN_TARGET_PCT` / `MAX_TARGET_PCT` | 0.6 / 2.0 | TP clamp, shrunk because the perp round trip is ~4× cheaper |
| `CONF_FLOOR` | 0.40 | **burn-in value** (10-15): Jev emits lower confidence on these instruments; spot's 0.50 blocked 15/16 calls. Recalibrate from ~30 closed perp trades. |
| `LIQ_BUFFER_PCT` | 15 | force-close if price comes within 15% of the exchange liquidation price |

Two-sided decision: for each contract Jev returns `p(long_k)`, `p(short_k)`,
`p(no_trade)` plus a rise- **and** a fall-probability (`noul`); the engine takes
whichever direction clears `MARGIN_BAR`/`CONF_FLOOR`/`NOUL_BAR`. Shorts close
`reduceOnly`. The engine respects a shared `STOP` and has its own `STOP_PERP`
breaker. `--dry-run` runs the full decision path with zero order placement.

## What the log looks like

One JSON object per line — `universe_scan`, `quality_gate_reject`,
`universe_depth_reject`, `jev` (full state + typed answers + token usage),
`no_trade`, `open`, `trail_armed`, `close`, `close_detected`, `day_roll`,
`cooldown_skip`, `daily_breaker`… The terminal renders this file; you can also
replay/calibrate from it (every Jev call records its probabilities, so
threshold sweeps are offline replays, not guesses).

## Hard-won lessons baked in (details in `../skills/okx-trading/SKILL.md`)

- **Ticker data lies on thin books** — a pair can show real volume and dust
  depth; we got filled 1% off mid once. Hence: top-5 book depth ≥ €1.5k at scan
  AND ≥ 3× stake re-checked live at entry.
- **A live OCO freezes the base balance** — pruning positions on *available*
  balance deletes live positions. Prune on *cash* balance + no live algo.
- **Entry fees come out of the base ccy** — selling the gross quantity fails;
  size sells from live balance.
- **The fee wall is the whole game** — TP at 7× all-in cost or don't trade.
- **Ground truth is the fills ledger**, not logged estimates.
