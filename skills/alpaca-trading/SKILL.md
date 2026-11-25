---
name: alpaca-trading
description: "Use for Alpaca trading: stocks engine, news decisions, API."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [trading, alpaca, stocks, news, api, venue-migration]
---

# Alpaca Trading (US stocks; crypto API available)

Venue of the ObekT live trading platform after the OKX→Alpaca migration
(operator directive: real stocks, decisions from stock news, crypto value
moved over as buying power — NO crypto trading). Live engine:
`engine/alpaca_stocks.py` (v13.0-volbrackets) in this repo, run on the VPS by a
systemd timer (`obekt-stocks.timer`) every 5m. Public deck trader.obekt.com reads its
telemetry; signed transparency chain continues across the venue change.
The OKX era is archived in `*_okx_retired` files — the okx-trading skill covers
the retired venue.

Algorithm history: v11.0-news (11-19, catalyst-only, 48h holds) → v12.0-intraday
(11-20, operator directive "multiple trades a day, flat by the close") →
v13.0-volbrackets (11-25, kill-gate replacement: v12 closed 34 trades at net
−$0.09; fixed LLM-set 0.8% stops sat inside the noise band, 53% stop-outs).

## When to Use

- Operating or extending the stocks engine, any Alpaca API/data question.
- Moving funds between OKX and Alpaca (stablecoin route below).
- Validating a news/catalyst strategy before going live.

## Account + credentials

- Creds `~/.config/alpaca/credentials.json` `{"key","secret"}` on the machine
  that runs the engine (chmod 600); env override `ALPACA_KEY`/`ALPACA_SECRET`.
  Keys are Trading API keys from app.alpaca.markets. Never commit the file; if
  you copy it between machines, verify the checksum matches end to end.
- Margin account, fractional trading enabled. At small equity (<$2k):
  effectively 1x LONG-ONLY — leverage needs $2k under Alpaca's intraday margin
  framework; shorting needs margin approval. PDT rule was retired by FINRA
  (2026): no day-trade count limit, no $25k minimum.
- Stocks commission-free; sells pay ~0.003% SEC/TAF (estimate it on close).
  Crypto pairs (if ever used): 0.15% maker / 0.25% taker, long-only, 24/7 —
  ~5x the OKX perp round-trip cost, never port a crypto fee model blind.
- Regular hours 09:30–16:00 ET (`GET /v2/clock`). The engine is INTRADAY:
  no entries after 15:30 ET, flat everything 15:50 ET — overnight/weekend gap
  risk is designed out, the book is flat every night.

## Standing operator rules

- Platform stays LIVE. Failed algorithms get REPLACED at a pre-registered
  kill-gate (30 closes OR 7 days, net ≤ 0 → replace), never silently
  stopped; no paper trading for live capital. The gate is enforced on the
  REALIZED net of the version's own closes — v12 hit it and was replaced.
- Intraday turnover is the operator directive: multiple trades a day, flat by
  the close — "not aiming long term stocks with that pocket money".
- Strategy edge is venue-specific. Re-validate on the new venue's data, fees
  and leverage before going live; report honest EV ("no demonstrated edge
  yet" is a valid launch state when the kill-gate is pre-registered).
- Trade with all available funds (all-in sizing across slots), but code owns
  risk: the LLM never sizes positions or overrides a gate.
- A venue/platform migration is DONE only when: (a) the SERVED public UI is
  audited panel-by-panel (old-venue strings, empty states, data-source
  claims, stale retired-book state files) — engine + header checks are not
  enough; (b) the public repo is re-released in the same change (engine,
  deck, skills, README, sample data — the signed-chain anchor auto-push is
  NOT a repo update); (c) the page is live-venue-first: retired history
  lives in ONE collapsed block at the bottom with a one-line final-stats
  summary, never as big mid-page sections. The operator checks the live
  page and the repo, and will call the migration unfinished if either lags.

## Engine architecture (manage/decide split)

News signal (Alpaca News API + Yahoo RSS, free) → self-hosted halogen via
chat.obekt.com/v1 ($0 marginal) returns TYPED JSON (action/confidence/impact/
horizon/rationale — catalyst classification ONLY) → deterministic gates →
fractional market orders → engine-managed exits: TP/SL computed BY CODE from
the symbol's 14-session ATR (SL = 0.35×ATR% clamped 0.6–2.0, TP = 1.5×SL
clamped 1.0–3.0) + 6h time exit + flat 15:50 ET (fractional orders cannot carry
server brackets). Any LLM transport/parse/schema failure FAILS CLOSED to
no_trade.
Full gate list, LLM contract, telemetry schema and the validation recipe:
`references/news-engine.md`. Endpoint/feed/wallet quirks and the fund-move
procedure: `references/api-and-data.md`.

## Top pitfalls (always-on)

- Market data lives on `data.alpaca.markets`, trading on `api.alpaca.markets`
  — snapshots/bars/news on the trading host return 404.
- Alpaca timestamps carry nanoseconds: truncate the fraction to 6 digits
  before `fromisoformat` (older Python fails → every quote reads stale →
  gates silently reject the whole universe).
- IEX BBO is a noisy NBBO proxy: gate execution cost on ask-vs-last-trade
  deviation (≤2.5% ghost guard), not raw bid/ask spread — mega-cap IEX spreads
  routinely read 2–4% while the real spread is ~0.01%, and Alpaca routes to
  NBBO anyway. A 10% spread disaster-guard is effectively off; tight IEX
  gates starve the book of legitimate candidates.
- Free data plan: long-range historical SIP bars 403; IEX historical works
  for multi-month studies (prices near-identical for drift measurement).
- LLM JSON breaks in the rationale string: parse tolerantly (direct → brace
  block → field regex), forbid double-quote chars inside rationale in the
  prompt.
- Never let the LLM set exit geometry: v12's model templated tp 1.2 / sl 0.8
  on 32/39 trades (confidence anchored at 0.62 on 30/39). Brackets belong to
  code, scaled to measured per-symbol volatility; ask the model only to
  classify the catalyst, and prompt for calibrated confidence across the full
  range.
- Symbol-keyed position meta + re-entry while held = overwritten exit
  management (v12 bug): never pyramid into a held symbol; clamp the leg
  budget to actual cash and fail-soft entry errors, else an Alpaca 40310000
  (cash spent) kills the cycle before the status write.
- Unit-test the decision contract with a synthetic strong catalyst AND a
  noise item before trusting live cycles — live news may all gate-reject,
  masking a broken LLM path.

## Ops anchors

- Keep a staging copy of the engine (source, systemd units, signer, health
  script, staged deck) separate from the live dir so you can compile-gate and
  dry-run before promoting. Dry-run: `PAPER_EQUITY_USD=120 python3
  alpaca_stocks.py cycle --dry-run` — full decision path, zero orders.
- Watchdog: a scheduler runs `engine/stocks_health.py` on the VPS and alerts on
  the JSON it prints; health = timer active + log/status freshness + STOP files
  + equity sanity + err-rate.
- Transparency signer `engine/verify_snapshot.py` (daily timer) reads Alpaca
  equity/positions + log counts; chain key/seq unchanged across venues.
