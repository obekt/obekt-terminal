# `engine/` — the autonomous trader

The strategy, in code. One process, four modes, no daemon needed — a scheduler
(cron/systemd timer/whatever) calls it every 5 minutes.

> ⚠️ **This trades real money on real exchanges.** Read [`../DISCLAIMER.md`](../DISCLAIMER.md).
> Start in demo/paper if your venue offers it. Nothing here is advice.

## Files

| file | what it is |
|---|---|
| `okx_jev.py` | The whole engine: universe scan → deterministic gates → Jev decision → risk veto → execution → position management. ~1450 lines, stdlib only (no pip deps). |
| `news_feed.py` | Keyless RSS catalyst feed (Cointelegraph/CNBC/Yahoo Finance) → `catalyst.json`, 30-min freshness guard. Called by `cycle`. |
| `social_feed.py` | Keyless sentiment: alternative.me Fear&Greed (+ streak), CoinGecko trending + per-coin community polls, vs-EUR market context → `social.json`, 15-min cache. Called by `cycle`. |

Everything the engine writes (log, state, caches) lands next to `okx_jev.py`
in its own directory — point `OKX_TERMINAL_BASE` there if you keep it elsewhere.

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

```cron
*/5 * * * * cd /path/to/engine && /usr/bin/python3 okx_jev.py cycle >> cycle.log 2>&1
```

### Kill switch

```bash
touch engine/STOP     # halts NEW entries immediately; management of open positions continues
rm engine/STOP        # resume (a daily-breaker STOP also auto-clears at UTC midnight)
```

## The strategy surface (tune these)

All at the top of `okx_jev.py`, each with a comment explaining the measured
reasoning behind the value:

| constant | default | meaning |
|---|---|---|
| `MAX_JEV_PAIRS` | 5 | how many gate-survivors the model sees (prompt-size bound) |
| `EDGE_MULT` | 4.0 | take-profit ≥ 4× all-in round-trip cost — the fee-wall rule |
| `MIN_TARGET_PCT` / `MAX_TARGET_PCT` | 1.5 / 3.0 | TP clamp — a scalp must be reachable in ~90 min |
| `SL_RATIO` | 0.47 | stop = 0.47 × TP |
| `TRAIL_ARM_RATIO` / `TRAIL_GIVEBACK_RATIO` / `TRAIL_FLOOR_RATIO` | 0.40 / 0.23 / 0.07 | trailing exit: arm at 40% of TP, exit after giving back 23% from HWM, never below 7% |
| `MARGIN_BAR` | 0.25 | p(chosen buy) − p(no_trade) from Jev's typed probabilities |
| `CONF_BAR` | 0.50 | OR-path: high certainty also passes |
| `NOUL_BAR` | 0.45 | Jev's P(price rises ≥ trail-arm within 45 min) floor |
| `MAX_OPEN` / `MAX_ENTRIES_PER_CYCLE` | 2 / 1 | concentration + one quality entry per cycle |
| `COOLDOWN_MIN` / `WIN_COOLDOWN_MIN` | 30 / 15 | per-pair lockout after a loss / after a win (a TP exit marks the local top) |
| `TIME_STOP_MIN` / `STALE_EXIT_MIN` | 30 / 90 | close losers after 30m, flat positions after 90m |
| `DAILY_LOSS_PCT` | 8.0 | software-failure backstop; auto-clears at UTC roll |
| `MIN_STAKE_EUR` | 10.0 | venue minimum sanity floor |

Sizing: stake = all free EUR / open slots (configurable philosophy — change
`decide()` if you want fixed sizing). Entry: maker-first `post_only`, IOC chase
only if the ask is within `CHASE_MAX_PCT` of the bid. Exits: server-side OCO is
the first line (they fire even if this machine dies); the poll loop owns
trailing, time-stops and the fallback stop.

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
- **The fee wall is the whole game** — TP at 4× all-in cost or don't trade.
- **Ground truth is the fills ledger**, not logged estimates.
