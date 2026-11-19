# `engine/` — the autonomous trader

**One live book, several engine files.** As of the 2026-11-19 venue change the
live book is `alpaca_stocks.py` — a **news-driven, long-only US-equity engine on
Alpaca** (v11.0-news). Fresh Alpaca News API headlines on a fixed 30-name liquid
universe become candidates; a **self-hosted halogen** model returns a typed JSON
verdict; deterministic code gates own all risk; fractional market orders enter
long-only and the engine manages its own TP/SL + 48h time exit. The OKX era is
**retired history**: the X-Perp book (`okx_perp.py` + `okx_mom.py`, final engine
v10.0-fade) retired 2026-11-19 with **426 closes, 40.8% WR, −$26.68 net of all
fees on $134.19 capital**; the spot EUR book (`okx_jev.py`) retired 2026-10-15
with **132 trades, −€21.03**. Their source and ledgers stay in the tree — the
deck still renders their frozen records, and `okx_perp.py`/`okx_jev.py` remain a
library the retired panels read. Each engine is a one-shot process called by a
scheduler (cron/systemd timer) every 5 minutes — no daemon needed.

> ⚠️ **This trades real money on a real broker.** Read [`../DISCLAIMER.md`](../DISCLAIMER.md).
> Rehearse with `--dry-run` (zero orders) first. Nothing here is advice. The live
> stocks book has **no demonstrated edge yet**; its kill-gate is pre-registered
> (30 closes OR 14 days, net ≤ 0 → replace).

## Files

| file | what it is |
|---|---|
| `alpaca_stocks.py` | **LIVE book — US STOCKS, news-driven (v11.0-news, long-only).** Alpaca News API headlines (≤90m, widened to 18h on the closed→open transition) on a fixed 30-name universe → deterministic pre-gates (ask-vs-last ≤2.5%, quote ≤90s, spread disaster-guard, chase ≤+7%, knife ≥−9%) → self-hosted halogen typed JSON (`action buy\|no_trade`, confidence, impact, target/stop, horizon, rationale; fails closed) → post-gates (conf ≥0.60, impact ≠ low, MAX_OPEN 2, one entry/symbol/day) → fractional market order, all-in sizing (equity×0.98/2 per leg) → engine-managed TP/SL (TP 1–5%, SL 0.8–3%, TP ≥1.5×SL) + 48h time exit + −8% daily breaker (UTC auto-clear) + STOP/STOP_PERP kill files + orphan reconciliation. Stdlib only. |
| `stocks_health.py` | Compact JSON health snapshot of the live engine (timer active, log/status freshness, STOP files, equity sanity, 60m error/warn/call rates). Read-only; printed fields feed a watchdog. |
| `verify_snapshot.py` | Daily signed equity snapshot (GET-only, isolated; never imported by the trader). Reads Alpaca account/positions + log counts, appends to the ed25519 hash chain, anchors the head to GitHub. See [`../verify/README.md`](../verify/README.md). |
| `okx_perp.py` | **RETIRED X-Perp execution host + Jev-signal book (two-way).** Kept as history and as a library: its execution machinery and `--dry-run` paper bookkeeping are reused by the retired fade engine. |
| `okx_mom.py` | **RETIRED deterministic cross-sectional fade engine (v10.0-fade).** Ranks liquid XPERPs by 48h return and fades the extremes. Final live book of the OKX era; retired at the venue change. |
| `okx_jev.py` | **RETIRED spot book (long-only, EUR pairs).** The honest 132-trade record of the first strategy, and a library (`okx_perp.py` imports its signed API / market data / Jev client). |
| `news_feed.py` / `social_feed.py` | Retired spot-era keyless feeds (RSS catalysts; Fear&Greed + CoinGecko sentiment). The live stocks book gets its news from the Alpaca News API instead. |

The live engine writes its log/state/caches **next to itself** (`perp_log.jsonl`,
`perp_positions.json`, `perp_status.json`, `perp_risk_state.json`, `news_state.json`,
`stocks_news.json`). These filenames and the `perp_*` event vocabulary are
**historical identifiers reused on purpose** so the deck adapter and signed chain
keep working across the venue change; all money is **USD** (the `usdc_equity` key
carries Alpaca USD equity). Point the deck's `OKX_TERMINAL_BASE` (historical env
name) at this directory. The live engine requires an **Alpaca US-stocks account**
with fractional trading enabled; at small equity it is long-only 1× (leverage needs
$2k, shorting needs margin approval). See
[`../skills/alpaca-trading/SKILL.md`](../skills/alpaca-trading/SKILL.md).

## Credentials

**Never hardcoded** — a JSON file or env vars (env wins):

```bash
# broker (Alpaca Trading API keys — https://app.alpaca.markets, live)
# file: ~/.config/alpaca/credentials.json  {"key":"***","secret":"***"}
# or env:
export ALPACA_KEY=*** ALPACA_SECRET=***

# decision model (any OpenAI-compatible chat endpoint; self-hosted halogen by default)
export LLM_URL="https://chat.obekt.com/v1/chat/completions"   # or your own relay
export LLM_MODEL="halogen-qwen3.8-flash-next"
export LLM_KEY=***            # or LITELLM_MASTER_KEY; falls back to ~/.config/litellm/credentials.json
```

Missing Alpaca creds → the engine exits **code 2 with a setup message**, not a
traceback. Missing LLM key → the decision path **fails closed to `no_trade`** (the
engine still runs). Alpaca keys are account-scoped; the engine targets the live
hosts (`api.alpaca.markets` trading, `data.alpaca.markets` data) — adapt the base
URLs for paper.

## Modes

```bash
python3 alpaca_stocks.py status    # account/positions summary, no trades (read-only)
python3 alpaca_stocks.py scan      # news scan + universe day-movers, no trades (read-only)
python3 alpaca_stocks.py cycle     # ONE full cycle — this is what the scheduler runs (REAL orders)
PAPER_EQUITY_USD=123.53 python3 alpaca_stocks.py cycle --dry-run   # full path, ZERO orders
```

A `cycle` is: check breaker/kill files → read equity → manage open positions
(reconcile orphans, TP/SL/time-exit, defer exits if market closed) → scan news +
universe → pre-gate candidates → ask halogen → apply conf/impact/slot gates →
maybe open one fractional long → heartbeat → write status. Every step logs a JSONL
event to `perp_log.jsonl` — that file is the terminal's data source and your audit
trail.

### Scheduling

The engine is one-shot by design — a scheduler calls `cycle` every 5 minutes.

```cron
*/5 * * * * cd /path/to/engine && /usr/bin/python3 alpaca_stocks.py cycle >> cycle.log 2>&1
```

**Prefer a systemd timer** (this is what we run in production): `Persistent=true`
catches up a run missed while the box was down, journald collects the logs, and it
survives reboot once `enabled`. Example unit files are in the main
[README → Run it → step 4](../README.md#b-the-full-live-system-your-capital-your-keys-your-responsibility).

### Kill switch

```bash
touch engine/STOP        # halts NEW entries immediately; management of open positions continues
touch engine/STOP_PERP   # same effect (historical second kill file)
rm engine/STOP           # resume (a daily-breaker STOP also auto-clears at UTC midnight)
```

## The strategy surface (tune these)

All at the top of `alpaca_stocks.py`, each with a comment. **These are the live
production values** — the deck's `DECISION GATES` panel parses them from source so
the UI can never drift from what the engine actually enforces.

| constant | default | meaning |
|---|---|---|
| `UNIVERSE` | 30 names | fixed liquid US-stock universe (AAPL…SQ); news, not liquidity ranking, picks candidates |
| `NEWS_WINDOW_MIN` | 90 | headline freshness window (widened to 1080 on the closed→open transition) |
| `MAX_LLM_CALLS` | 4 | per-cycle cap on halogen calls |
| `ASK_VS_LAST_PCT` | 2.5 | ghost-quote guard: IEX ask >2.5% off the last trade = broken/stale quote |
| `QUOTE_STALE_S` | 90 | max quote age |
| `MAX_SPREAD_PCT` | 10.0 | disaster guard ONLY — IEX BBO is not the NBBO; Alpaca routes to the inside market |
| `CHASE_CAP_PCT` / `KNIFE_FLOOR_PCT` | +7.0 / −9.0 | don't chase a name already up big; don't catch a collapsing one |
| `CONF_FLOOR` | 0.60 | burn-in confidence bar; recalibrate after ~30 calls |
| impact gate | ≠ low | low-impact news never trades |
| `MAX_OPEN` | 2 | open-position slots |
| `MAX_NOTIONAL_FRAC` / `MAX_NOTIONAL_USD` | 0.98 / 500 | all-in sizing fraction (equity×0.98/2 per leg); the USD term is a bug guard |
| `TP_MIN`/`TP_MAX` · `SL_MIN`/`SL_MAX` | 1–5% · 0.8–3% | bracket clamps, enforced in code |
| `TP_SL_RATIO` | 1.5 | enforce TP ≥ 1.5×SL |
| `MAX_HOLD_H` | 48 | news drift decays; time exit |
| `DAILY_LOSS_PCT` | 8.0 | software backstop breaker; auto-clears at UTC roll |
| `SELL_REG_FEE_RATE` | 0.00003 | SEC+TAF estimate on sells (commission-free venue) |

Sizing: budget/leg = equity × 0.98 / MAX_OPEN; fractional qty = budget/ask, min $1
notional. Entry: market order in regular hours (09:30–16:00 ET). Exits:
**engine-managed** — fractional orders cannot carry server-side brackets, so the
engine polls every 5m and exits at TP/SL/time; exits defer to the next open when
the market is closed. `--dry-run` runs the full decision path with zero order
placement and a paper equity (`PAPER_EQUITY_USD`).

## What the log looks like

One JSON object per line — `mom_scan` (signal:"news", day movers), `perp_universe`,
`perp_gate_reject`, `perp_jev` (the halogen call: choice, conf, impact, pairs,
headline, rationale, token usage), `perp_no_trade`, `perp_open`, `perp_close`,
`perp_close_detected`, `stocks_exit_signal` / `stocks_exit_deferred`, `news_llm_err`
(fail-closed), `breaker_fire` / `breaker_clear`, `stocks_orphan_adopted`,
`mom_heartbeat`… Every record carries `ts`, `engine:"stocks"`, `strategy:"news"`,
`dry`. The terminal renders this file; you can also replay/calibrate from it.

## Hard-won lessons baked in (details in `../skills/alpaca-trading/`)

- **IEX BBO is a noisy NBBO proxy** — gate execution cost on ask-vs-last deviation
  (≤2.5% ghost guard), not raw bid/ask spread (routinely 2–4% on liquid names while
  the real spread is ~0.01%). Tight IEX spread gates starve the book of legitimate
  candidates.
- **Alpaca timestamps carry nanoseconds** — truncate the fraction to 6 digits before
  `fromisoformat`, else every quote reads stale and the gates reject the whole universe.
- **The news API `start` param is unreliable** — fetch newest pages without it and
  filter `created_at` client-side.
- **LLM JSON breaks in the rationale string** — parse tolerantly (direct → brace
  block → field regex), require brackets only for `buy`, forbid double-quotes inside
  rationale in the prompt; fail closed on anything unparseable.
- **Fractional orders can't carry server brackets** — the engine must manage TP/SL
  itself, and document the poll-granularity slippage + overnight gap risk honestly.
- **No demonstrated edge yet** — raw news has no blind edge (measured); the LLM
  catalyst filter is the hypothesis. Launch with a pre-registered kill-gate, not a
  fake backtest number.