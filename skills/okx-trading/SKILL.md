---
name: okx-trading
description: "Use when trading crypto on OKX Europe (EEA) spot."
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [trading, okx, crypto, eea, api]
---

# OKX Europe (EEA) Crypto Trading

OBSERVATION TERMINAL: the read-only web deck that renders this engine's log
lives in `terminal/` (see its README). It is generated from the engine's JSONL
telemetry + public market data only and never touches exchange credentials.

Venue: eea.okx.com (EEA), v5 REST. Spot, EUR-quoted pairs, long-only scalping.
The pilot is `engine/okx_jev.py` — Jev decides, code acts (decision-model
rules in the `system-one-decisions` skill; venue specifics here). Run it from
cron every 5 minutes: `*/5 * * * * python3 engine/okx_jev.py cycle`.
Strategy directive: fast trades, take profit immediately, volume regime.

## 0. Auth — TWO independent paths

- **API key (trading + market data + econ calendar)**: creds `~/.config/okx/credentials.json`
  (mode 600: api_key/secret/passphrase/base=https://eea.okx.com). CLI profile lives in
  `~/.okx/config.toml` `[profiles.hermes]` with `site="eea"` + `demo=false`
  (`okx config add-profile AK=.. SK=.. PP=.. site=eea` then hand-edit demo=false —
  add-profile defaults demo=true and `okx config set demo false` is NOT a valid key).
  Key perms: read_only,trade. EEA users MUST use eea.okx.com — www.okx.com returns
  `50119 API key doesn't exist` for EEA keys.
- **OAuth 2.1 — DONE for EEA (09-28), but `okx news` is DEAD ON EEA ANYWAY.**
  Login flow: strip the [profiles.hermes] block from config.toml first (CLI refuses
  OAuth while ANY profile has api_key: `{"status":"skipped","reason":"api_key_configured"}`),
  then `okx auth login --site eea --manual` → device code +
  https://my.okx.com/account/oauth?flow=device (10-min window; user approves in
  browser), then RESTORE the profile block — both auth methods coexist.
  Token: scopes live:trade/read/earn/asset_transfer, ~1h ttl, auto-refresh via
  ~/.okx/bin state. BUT: `/api/v5/orbit/*` (news/sentiment) endpoints exist ONLY on
  www.okx.com (global): eea.okx.com and my.okx.com 404 (module not deployed), and
  www.okx.com 401s an EEA-site OAuth token even with OKX_SITE=global. Conclusion:
  orbit news/sentiment is unusable for EEA accounts — don't retry. News input =
  keyless RSS catalyst.json (news_feed.py, 30-min guard, called from cycle mode) +
  PUBLIC /api/v5/public/economic-calendar (works on eea with key auth).
- Signing: `OK-ACCESS-SIGN` = base64(HMAC-SHA256(secret, timestamp + method +
  path+query + body)); headers OK-ACCESS-KEY/-TIMESTAMP (ISO ms Z)/-PASSPHRASE.
  Public endpoints need no auth. urllib 403s without a User-Agent header.
- MCP (https://www.okx.com/api/v1/mcp/trading-oauth) exists but the CLI + raw REST
  cover everything; MCP adds an OAuth dependency for no gain here.

## 1. CLI (`okx`, npm @okx_ai/okx-trade-cli)

- `okx --live account balance|positions|fees`, `okx market ticker|candles|orderbook`
  (public, no auth), `okx spot|swap ...` orders. Subcommands use SPACES not hyphens.
- `okx config show --json` = authoritative for API-key profiles; `okx auth status
  --json` for OAuth state. Use `--json` everywhere in scripts.
- Skills installed via `npx skills add okx/agent-skills` → ~/.agents/skills/okx-*
  (auth, market, trade, portfolio, news/sentiment-tracker, bot, earn, smartmoney).

## 2. Venue specifics (verified live 09-28)

- Account: EEA MiCAR, settle ccys USDC/USD/USDG — **trade EUR-quoted pairs**
  (BTC-EUR, ETH-EUR, SOL-EUR, XRP-EUR, LINK-EUR). EUR books are TIGHT (0.004-0.06%
  spread) but thinner than USDT: DOGE/ADA/AVAX-EUR spread 0.09-0.24% + low volume —
  excluded from PAIRS. No USDT pairs for EEA keys.
- Fees Lv1: 0.1% maker / 0.2% taker (fiat EUR same). ~0.4% round trip on market
  orders. The `fees` endpoint reports taker as "-0.002" (negative = rebate notation)
  — do NOT read that as getting paid; realized fills charge ~0.2% (fee field on
  fills is negative in the paying direction per ccy).
- **Entry fee is deducted in BASE ccy** → after a buy, availBal < bought qty.
  Selling the gross qty fails `51008 insufficient balance`. Always size sells from
  live availBal. An attached OCO with sz=gross qty fails `51020 minimum order
  amount` — size the OCO sz net (qty*(1-0.0025), round down to lotSz).
- **algo_fallback_err OKX 50014 "Parameter side can not be empty"** on OCO attach (observed 09-30 CT-EUR): the attached-OCO fallback POST sends a leg without side. Entry still fills; stop is then owned ONLY by the script's manage() poll, not server-side — do not assume a live OCO protects the position when algo_ok=false. Watch for this event in the open record.
- Attach field is **`attachAlgoOrds`** (PLURAL, list) on POST /api/v5/trade/order.
  `attachedAlgoOrd` (the name in some docs) is SILENTLY IGNORED — entry fills, no
  protection, no error. Verify via order detail `attachAlgoOrds[].failCode` AND
  GET /api/v5/trade/orders-algo-pending?ordType=oco&state=live.
- Spot orders: `{instId, tdMode:"cash", side, ordType:"market", sz, tgtCcy:"base_ccy"}`.
  sz is in BASE ccy with tgtCcy=base_ccy. Round sz/px to lotSz/tickSz (strings).
  minSz matters: dust below minSz is UNSSELLABLE without topping up (or Convert).
- Econ calendar `/api/v5/public/economic-calendar` is PUBLIC on eea with key auth —
  inject next-24h macro events (importance 2-3) into Jev state.
- Order detail after fill: avgPx, fee+feeCcy, state. Cancel OCO: POST
  /api/v5/trade/cancel-algos [{algoId, instId}]. Fills: /api/v5/trade/fills.

## 2b. Sentiment/news reality for EEA (FINAL, re-verified 09-30)

- OKX orbit news+sentiment (behind `okx news coin-sentiment` etc.) is UNUSABLE for
  EEA accounts. Real endpoint paths extracted from @okx_ai/okx-trade-mcp 1.4.8
  dist/index.js: /api/v5/orbit/{news-search,news-detail,news-platform,
  currency-sentiment-query,currency-sentiment-ranking}, journal/smartmoney/*,
  aigc/mcp/*. On eea.okx.com: 404 (module absent) incl. via CLI OAuth. On
  www.okx.com: 401 code 50119 with the EEA API key (site-scoped). The MCP server
  would hit the same walls — don't retry. WORKING on eea with key auth:
  rubik long-short-account-ratio + taker-volume, public funding-rate,
  public economic-calendar (needs OK-ACCESS-KEY header on eea, 401 keyless).
- REPLACEMENT STACK (all keyless, wired 09-30):
  - social_feed.py -> social.json (15-min cache): alternative.me Fear&Greed
    (value/label/d1/d7_avg/streak_days), CoinGecko /search/trending (top-10
    attention), per-coin context via ONE bulk /coins/markets?ids=... call
    (chg24h, vol, off-ATH, mcap rank, day_range_pos) cached 12min in
    cg_bulk_cache.json, plus per-coin sentiment_votes_up% (the bullishRatio
    analogue) via /coins/{id} polls — ONLY source of the votes, capped
    MAX_POLL_CALLS=3/run + 60min cache (cg_coin_cache.json) because free tier
    429s after ~5-15 calls/min. Coverage converges over cycles. Reads
    universe.json (written by okx_jev.py) so sentiment always matches the
    dynamic universe. Symbol->id map: /coins/markets top-250, 12h cache
    (cg_symbol_map.json); ID_OVERRIDES for NIGHT/PUMP/XPL; XDP unmapped.
  - news_feed.py -> catalyst.json (RSS titles, 30-min cache) unchanged.
  - okx_jev.py load_social(): stale >45min -> None; social is OPTIONAL context,
    never a hard gate.

## 2c. v3.1 engine (09-30): dynamic universe + cost-aware brackets

- WHY: v3 (fixed 4-pair list + 0.12% spread gate) traded 0 times in 24h. Live
  scan of all 273 EUR pairs: only ~14-22 clear liquidity; movers (PUMP/AAVE/
  AVAX/XDP, 15-25% day ranges) carry 0.14-0.20% spreads; majors are tight but
  flat. On EUR books, tight spread and volatility are mutually exclusive.
- select_universe(): keyless /market/tickers (ALL spot, 1 call, 10-min cache ->
  universe.json) -> filter vol24h>=€200k, spread<=0.20%, 24h range>=2% -> rank
  by day range -> top 10 (UNIVERSE_SCAN) -> momentum gates -> top 5 to Jev
  (MAX_JEV_PAIRS, ranked by chg30*vol30; jev_pair_cap event logs drops).
- bracket_for(spread): TP = EDGE_MULT x all-in cost (spread+0.1 maker+0.2 taker),
  clamped [1.5, 3.0]. SL=0.47xTP, trail arm 0.40xTP, giveback 0.23xTP, floor
  0.07xTP, min_vol30 = 0.6xTP (reachability gate replaces fixed 0.9%). Bracket
  stored in positions.json meta at entry; manage() reads it per-position (old
  positions derive from live spread + bracket_derived event). OCO TP/SL and the
  Jev noul question ("rise >= trail_arm% in 45min") both use the pair's numbers.
- PROMPT BUDGET (user requirement): measured 1.92 chars/token on 204 logged
  calls. MAX_STATE_CHARS=6200 (~3.2k tok); trim_state() cuts news->macro->
  secondary pair fields->pairs(3) in priority order; every jev event logs
  prompt_stats {state_chars, est_tokens, input_tokens, trimmed}; prompt_budget_warn
  event above MAX_JEV_INPUT_TOKENS=3400. Verified live: 4-pair state with
  social+fear_greed+trending+macro = 4,929 chars / 2,916 real input tokens
  (v3 was 4,118 chars / ~2,100 tok — +800 tok for full social coverage, under
  budget). Context prose trimmed ~40% to pay for it.
- macro_events_24h: importance>=2 only, UPCOMING only. **economic-calendar
  before/after are INVERTED** (verified live 09-30): before=<ts> = events NEWER
  than ts, after=<ts> = events OLDER than ts (defaults to now). Upcoming 24h =
  before=now & after=now+24h; recent 3h = after=now & before=now-3h. A naive
  after=now + date>now filter silently returns NOTHING (cost us the 12:30 UTC
  PCE surprise — BTC +1.7% — with zero macro context in the prompt).
- macro_released_3h_surprises: recent imp>=2 releases where actual != forecast
  (PCE 3.4% vs fc 3.7% = soft inflation print = risk-on tape fuel). Compact
  fields {e, imp, act, fc, ago_h}. Trim priority: upcoming shrinks before
  surprises (surprises are rarer and more decision-relevant).
- CoinGecko 429 lesson: never fan out per-coin calls in one run; bulk-first,
  polls budgeted + long-cached, stale-cache fallback on 429.

- DEPTH GATES (v3.2, after CT-EUR incident 09-30): ticker spread/vol24h LIE on
  thin EUR listings — CT-EUR passed all scan filters, Jev picked it at conf
  0.76, but the real book had €662 top-5 bid depth; our €70 IOC filled ~1% off
  mid, OCO attach failed, stopped at -4.4% = -€3.6, tripped the daily breaker.
  Fixes: (1) scan checks book_depth_eur() top-5 >= €1,500 for ranked candidates
  (universe_depth_reject event); (2) AT ENTRY re-read live book, require depth
  >= 3x stake AND >= €1,500, and re-price the bracket from the LIVE spread
  (scan data is up to 10 min stale); (3) standalone-OCO fallback body needs
  side:"sell" (was missing -> 50014, position ran unprotected except poll-stop).
  manage()'s poll stop-loss saved the trade at -4.4% — backup layers matter.
- Daily breaker STOP auto-clears at UTC midnight day_roll ONLY if the file
  content starts with "daily breaker" (human STOP files untouched).

## 2d. v3.3 (10-01): multi-timeframe state + margin-based decision bar

- USER CHALLENGE "is Jev's data good enough? market moves but no trades" ->
  measured replay: market WAS moving (NEAR +25%/7d, PUMP +38%/7d, avg 24h
  range 2.7%+); gates passed often (75 Jev calls/27h, 38 buy picks) but the
  conf>=0.50 bar vetoed 35/38. Jev `confidence` is a certainty metric that
  saturates <0.45 on 45-min calls even with clear preference (buy_ct p=0.74 vs
  0.26 -> conf 0.48 vetoed; same pair later conf 0.76 -> traded, lost on thin
  book). The metric was noise on the decision edge.
- NEW BAR: margin = p(chosen_buy) - p(no_trade) from best_action.probabilities.
  Trade if (margin >= MARGIN_BAR OR conf >= 0.50) AND noul >= NOUL_BAR
  0.45. Replay over 38 real picks: margin>=0.25 & noul>=0.45 = 9 would-fire
  (vs 3 for conf bar); after depth gates ~6. Logged as `margin` on open/no_trade.
- NEW STATE (multi-timeframe, was 30-min-only keyhole): chg_4h_pct (1H candles),
  chg_7d_pct + pos_7d_range_pct + days_up_of_7 (1D candles) per pair. Strategy
  premise is "pullback in a strong uptrend" — the multi-day trend was invisible
  to Jev before this. Degrades to null for young listings (<8 daily candles).
  Context text teaches: strong 7d + mild 30m dip = ideal; negative 7d = bear
  rally, extra caution. Prompt still ~2.0-2.6k tokens (under 3.4k alarm).
- Momentum gates replay (3 days of 5m bars, 09-28..10-01): pass rates 6-23% on
  movers (NEAR 22.7%, SUI 15.5%, PUMP 16%, BTC 0.9%) — gates are NOT the
  bottleneck; the Jev bar was. Dominant reject reason chg30 (~62-72% of bars).
- Daily loss breaker set at -8%: quality gates decide whether to trade, not a
  loss counter; -8% is only a software-failure backstop.
  Breaker STOP auto-clears at UTC day roll (content must start "daily breaker").

- v3.5 (10-03) WIN COOLDOWN: COOLDOWN_MIN=30 (losses) + WIN_COOLDOWN_MIN=15
  (profitable exits). Measured leak: Oct 2 ran 8 CT-EUR round trips — winners
  interleaved with re-entry stops bought right after an exit high (net +€0.20 on
  €2.12 fees; would have blocked ~4 of the re-entries). A TP exit marks the
  local top; immediate re-entry is the worst entry in the book. in_cooldown()
  now returns minutes-left (0=clear) and logs cooldown_skip with after=win/loss.

- v3.6 (10-05) REPLAY-TUNED PARAMETERS: EDGE_MULT 4->7, MARGIN_BAR 0.25->0.30.
  Method: offline simulator replayed 54 logged trades candle-by-candle on real 5m
  history-candles using each trade's LOGGED bracket (conservative intrabar order:
  SL before TP; poll-based trail/time-stop on candle close). Validation caveat:
  replaying the exact logged brackets gave +5.67 EUR vs -4.14 actual -> the
  simulator is ~0.18 EUR/trade optimistic (slippage, 5-min poll latency, taker
  chases). Trust variant DELTAS, never absolutes. Findings: TP 4x->7x cut hard
  SL exits 16->7 and moved wins to the trail (14->23); fixed-TP hits became rare
  (16->6) - the trailing exit owns wins by design. MARGIN_BAR 0.30 raised win
  rate 53.7->57.4% by dropping ~5 weak trades; 0.35+ cuts too deep. Time-stop ON
  beat OFF by ~2 EUR - it is not the leak. Root cost is EXECUTION (0.32%/trade
  fee+slippage drag vs 0.22% gross edge), not bracket shape; the signal itself
  tests effective (WR 47.5% vs 37.7% breakeven, payoff 1.65:1). Phase-2
  candidate if still sub-water after ~50 trades: post-only (maker) exits on
  trail/time-stop legs instead of market sells.

- v3.6.1 (10-06) GATE COUPLING FIX + desk-note grounding.
  MIN_VOL30_MULT 0.6 -> 0.40. BUG: the vol30 reachability gate is defined as
  MIN_VOL30_MULT x TP, so raising EDGE_MULT to 7 (v3.6) silently scaled the
  entry filter too — TP ~2.3-3.0% made the gate demand 1.4-1.8% realized 30min
  range against a tape median of 0.49%. Throughput collapsed to 1 trade in 8.5h
  (cycles reaching a Jev decision 20% -> 9%): the wide bracket was fine, but it
  choked the front door. LESSON: any parameter expressed as a MULTIPLE of TP
  couples entry throughput to the fee wall; the v3.6 replay simulator only
  scored trades ALREADY TAKEN, so it structurally could not see a gate change's
  effect on trade COUNT — always re-measure gate pass-rate separately after
  moving any bracket parameter. 0.40 x ~2.5% TP ~= 1.0% absolute vol30
  requirement: restores pre-v3.6 flow while keeping the wide bracket intact.
  The gate should ask a market-property question ("can this pair move ~1% in
  30min?"), never "60% of my target?".
  DESK-NOTE GROUNDING: the observation terminal's commentary LLM prompt
  self-described the system as a "read-only showcase" and the data snapshot
  carried no trading-state facts, so the model invented "engine in read-only
  showcase mode, not placing live orders" on a LIVE-capital public page. Fix:
  add a trading_state block to the prompt payload (mode / live_capital /
  halted=STOP-file-exists / open_positions / day_trades) and state plainly that
  the engine trades REAL capital and is never demo/paper/showcase; low trade
  counts are gate selectivity, not a mode change; rules forbid invented modes.
  Same correction in the per-trade debrief prompt. NOTE: the desk-commentary
  model is served over a separate path from the decision model — a desk-LLM
  outage only freezes the site's commentary (cache fallback shows the last good
  note); trading decisions and the whole engine are fully unaffected.

## 2e. X-Perps — the shorting path for EEA (verified live 10-15)

**EEA spot accounts cannot short.** Spot margin and all USDT pairs return 51155
("local compliance restrictions"); there are ZERO EUR-quoted margin/futures
instruments. The only compliant short path is **X-Perps**: MiFID-regulated
USD-margined expiry-perps (OKX Europe Markets Ltd, Malta), long AND short,
10x max retail, behaving like perpetuals with a ~60-month settlement date.

- **Unlock path** (all three required, in order): (1) pass the X-Perp
  appropriateness assessment in the web UI (linked from the X-Perps FAQ page) —
  this auto-bumps `acctLv` 1→2; (2) enable trading permission for the market on
  the API key (API Management) — without it every derivative order returns
  **50124** "API Key does not have trading permission for the market"; (3) hold
  USDC in the TRADING account (account-mode changes sweep balances to Funding —
  transfer back manually; the API transfer may return 50120 without transfer perm).
- **Discovery**: use PRIVATE `/api/v5/account/instruments?instType=FUTURES` —
  account-scoped, populates only after unlock (242 `*_XPERP` contracts). The
  PUBLIC catalog is the global list and is misleading in both directions (shows
  USDT margin pairs you can't trade; the private feed is the truth).
- Naming: `<BASE>-USD_UM_XPERP-<yymmdd>`; ctVal e.g. 0.0001 BTC; **sz is in
  CONTRACTS, not base ccy**; tdMode=isolated; close with `reduceOnly`.
- **Fees 0.02% maker / 0.05% taker** ≈ 0.07% round trip vs EUR spot's ~0.3% —
  the fee wall that made the spot book net-negative (-21 EUR on gross +3.8 over
  128 trades) does not exist here.
- Error map: 50124 = key not enabled for market; 51010 = account mode still 1;
  51155 = compliance (USDT/spot-margin, permanent for EEA retail); 54000 =
  "Margin trading is not supported" (spot margin product absent); 51008 =
  genuine collateral shortfall (order path works).
- **Probe pitfall**: a too-small probe order returns 51020 (min size), MASKING
  the real permission error — always probe at ≥ minSz, far-off post_only, then
  cancel.
- 1x isolated on BTC short: exchange liqPx was 98.6% away — liquidation is a
  non-event for scalps, but keep a liq-distance guard if leverage ever rises.
- Two-sided Jev: per contract noul p_up AND p_dn + choice {long_k, short_k,
  no_trade}; direction-specific noul must clear NOUL_BAR. Jev emits LOWER
  confidence on perps than spot (avg 0.32 vs 0.5-0.8 over first 16 calls) —
  spot-calibrated CONF_FLOOR 0.50 blocked 15/16; burn-in floor 0.40 at tiny
  notional until ~30 trades provide evidence.

## Version catch-up (v3.8 → v4.1, all replay-driven)

- **v3.8 (10-12)**: maker-only entries (IOC chase DELETED — taker_chase was the
  worst bucket: 23 trades, -7.83 EUR) + hard CONF_FLOOR 0.50. Zero-fee replay of
  105 trades: gross edge +41.7% WR / PF 1.19 is REAL but thin; fees are the enemy.
- **v3.9 (10-14)**: aggressive mode — MAX_OPEN 1 (full bankroll/trade), looser
  momentum gates (chg30 0.15, bars_up 2/6, vol30 0.75), faster rotation
  (cooldowns 20/10, stale 60m).
- **v3.10 (10-15)**: NEAR-EUR banned from universe (only filter positive in BOTH
  out-of-sample halves; NEAR: -6.09 EUR, 27% WR — and 15% WR at conf>=0.60, its
  WORST band); unhurried exits (time-stop/stale) sell post_only maker + 45s market
  fallback. **OCO TP legs can NEVER be maker** (trigger fires at/through the level
  → any limit there crosses the book). conf>=0.60 floor FAILED out-of-sample
  (+5.64 / -4.46 halves) — no entry-filter change was adopted.
- **v4.0/v4.1-perp (10-15)**: two-way X-Perp book live (see §2e). Isolated
  engine importing the spot plumbing; USDC collateral; USDC REMOVED from the spot
  engine's STABLES sweep list (it would have sold the perp collateral every cycle).
  v4.1: perp CONF_FLOOR 0.50→0.40 burn-in after 15/16 calls were blocked.
- **v4.5/v4.6-perp (10-15)**: full-capital sizing (98% of collateral) was tried,
  then CONTAINED back to a $25/trade burn-in cap after 1W/8L (-2.5% in 10h) at
  full size. Kill-gate written into the engine: at n>=30 closed perp trades run a
  P&L replay; if no edge, close the book and park the collateral in savings —
  pre-committed, no re-litigating on a losing streak.
- **v4.7-perp (10-16)**: Jev state enrichment — the perp state was THINNER than
  the retired spot state. Added: BTC regime (alts are beta to BTC; measured on
  our own ledger: BTC-aligned entries +0.07%/trade vs -0.17% against), own
  recent-trade memory (the engine re-entered the same instrument 12x overnight
  into identical stops because the code-level cooldown was invisible to the
  model), news catalysts, macro calendar (economic-calendar before/after params
  are INVERTED — naive filters silently return nothing), per-pair rubik
  ls_ratio + taker_buy_share. No thresholds touched.
- **v4.8/v4.9-perp (10-16/18)**: the enrichment shifted Jev's conf distribution
  down (pick avg 0.45->0.28, max 0.85->0.55 over 700+ calls) while directional
  noul held — a floor calibrated on the OLD distribution became unreachable
  (36/36 blocked; 10 attempts in 60h). Gates re-baselined on the measured new
  distribution (CONF_FLOOR 0.20, MARGIN_BAR 0.15, MIN_VOL30_PCT 0.30 for quiet
  tape) + IOC-taker fallback for unfilled maker entries, OCO re-anchored to the
  real fill, entry_type logged. LESSON (third instance): never keep a threshold
  calibrated on a different decision-metric distribution — re-measure the
  distribution, then re-derive the gate; distribution re-calibration is NOT the
  same as outcome-mining. The maker-only rule was a SPOT-fee lesson (0.2%
  taker); at 0.05% perp taker, 80% of signals dying unfilled costs more than
  crossing. Calibration sample restarts at each such change.
- **v4.9.1-perp hotfix (10-20)**: the v4.9 IOC fallback NEVER FIRED — every
  attempt logged `perp_entry_ioc_err err="'ask'"` (22 in 2.5 days). The helper
  returns the RAW venue ticker row (`askPx`/`bidPx`); the new code read
  `t["ask"]` → KeyError, swallowed by the surrounding try/except into a log
  event nobody watched, silently degrading to the old unfilled=no-trade path.
  TWO RULES: (1) an exception handler that LOGS instead of raising hides a dead
  code path — after deploying any new fallback, grep the log for its error
  event within the first hours; "no errors" and "every error the same KeyError"
  are different states. (2) Verify raw-vs-normalized dict keys when reusing a
  fetch helper: the engine carried BOTH shapes (the entry path normalizes to
  bid/ask at fetch time; the raw ticker call does not), and the new block
  assumed the wrong one. Fixed to askPx/bidPx + a ref<=0 guard.
- **v4.9.2-perp (10-21)**: one instrument excluded from the universe
  (`UNIVERSE_EXCLUDE`, filtered at the top of the scan loop OUTSIDE the
  try-block so a bad name can never be swallowed by the scan's error handler).
  Evidence: it was ~half the book's closes at 31% WR and carried over half the
  total loss; four same-day re-entries into one stop despite the model seeing
  its own recent exits in the prompt. Precedent: the same instrument was the
  only out-of-sample-robust exclusion in the spot replay. HONEST CAVEAT stated
  to the operator: containment, not edge — the remaining book was still
  negative per-trade, only slightly less bad than the excluded name. A
  universe filter is not a decision-metric change, so the calibration window
  and review gate were NOT restarted; expect slower sample accrual, making the
  DATE gate bind before the n gate.
- **v4.9.3-perp (10-21)**: `MAX_JEV_PAIRS` 4→6 (operator: "more Jev calls,
  this is cheap") — widen the opportunity set per model call. Diagnosis behind
  it: polling RATE is not the throughput bottleneck (state features come from
  5-min candles, so a faster timer mostly re-sends identical state; the model
  itself says no_trade ~88%); pairs-per-call is the lever that changes how
  many opportunities are SEEN. Measured immediately: on a quiet tape the
  day-range floor admits ~3 candidates, so the 6-cap only pays off in lively
  tape. Token math: input ~2.1-2.4k at 2 pairs, ~3.5k projected at 6 — under
  budget; an overnight watchdog alarms above 5k and is authorized to roll the
  cap back without asking.
- **v4.9.4-perp (10-23)**: UNIVERSE-SCAN UNIT BUGS — the real root cause of the
  "no trades" complaints (40h / 184 model calls with zero direction picks).
  (1) The 24h-turnover field is BASE-CCY on this venue's perp tickers, not USD
  as the code comment claimed: the BTC perp showed 4,066 (= 4,066 BTC ≈ $330M)
  and failed the $300k floor — every expensive coin was structurally invisible
  since launch while cheap coins passed 10-12x overstated. (2) Order-book size
  is in CONTRACTS: top-5 depth = px×sz missed ×ctVal, reading BTC's real
  $28.8k book as $288M and under-reading 10-ctVal books 10x — the phantom-depth
  guard measured noise. Fixes: vol_usd = volCcy24h × last; depth ×= ctVal.
  Result: candidates 2-3 → 11/17, first-ever LTC/AVAX/LINK selections, and the
  depth floor finally real (SUI passes at $1,313). FOURTH instance of the
  unit-denomination bug class on this project (fee ccy per book, P&L label
  mixing, now volume+depth). RULE: when a liquidity filter rejects an obviously
  liquid instrument, suspect UNIT MISMATCH before market structure — verify one
  raw API row (vol24h=contracts, volCcy24h=base ccy, book sz=contracts, ctVal
  per instrument). Known gap noted: the entry-time depth re-check constant
  exists but is unwired on the perp side; scan depth can be 10 min stale.
- **v4.9.5-perp (10-24)**: PROMPT REFRAME — operator: "why is jev always
  saying no? can it be the way you ask jev?" Partly, yes. METHOD (reusable):
  monkeypatch the decision-API call inside the engine to capture the exact
  live (state, questions) and raise an abort exception — decide() can never
  reach order placement — then replay the same captured state offline through
  context variants × N reps and compare p(no_trade). PITFALL: the engine
  module and the harness's library import are the SAME module object; save
  the real function before patching and restore it in a finally, or every
  replay arm hits the fake. FINDINGS on a quiet-tape state: control
  p(no_trade) 0.53-0.55; neutralizing the "recent trades … else no_trade"
  suppression clause −0.05; adding an active-EV framing ("find the highest-
  expected-value direction; no_trade ONLY if both sides are negative-EV net
  of costs") −0.20 (0.28-0.36); correcting the chop-cost line did nothing
  (the number was accurate — verify a prompt line against the real bracket
  before "fixing" it). Even the most aggressive arm picked 0/3 on a genuinely
  quiet tape: TAPE dominates, framing modulates. Historical corroboration:
  the no_trade rate was 14% before the v4.7 enrichment and 89% two days
  after — the suppressor stack (regime warnings + own-stop memory + headline
  risk + "when unsure pick no_trade") bought that collapse. DEPLOY: exact-
  string pre-validation on the captured state, then an AST check that the
  patched source's folded context literal is byte-identical to the tested
  string (adjacent string literals fold to one ast.Constant — verification
  without running decide()), then one supervised cycle. First supervised
  cycle traded after 11h idle: margin 0.35 vs a 0.15 bar, all gates cleared
  with room. Thresholds untouched; calibration window restarts (prompt
  change = new decision-metric distribution).
- **v4.9.6-perp (10-24)**: MULTI-SLOT — operator: "can we have multiple open
  positions at once?" Two things blocked it: MAX_OPEN=1, and a dead loop —
  cycle()'s `for _ in range(MAX_ENTRIES_PER_CYCLE)` broke after the first
  iteration in BOTH branches (fill and no-trade), so the constant never
  mattered. Fixes: MAX_OPEN 1→4 (4 × per-trade cap keeps equity headroom —
  the local budget check reads TOTAL equity, not available margin, so too
  many slots would pass locally and bounce exchange-side); break→continue ON
  FILL only (a no-trade still breaks: same state, same answer);
  MAX_ENTRIES_PER_CYCLE=2; service timeout raised (two decide passes + maker
  waits can exceed the old budget). Position metadata, per-instrument
  cooldowns and the manage loop were already multi-position (inherited from
  the multi-slot spot design) — only the entry path was single-slot.
  Verified end to end in ONE supervised cycle: trail-closed a winner, opened
  a first-ever instrument (visible only since the v4.9.4 unit fix made
  expensive coins pass the turnover floor), then the second decide() in the
  same cycle ran and honestly gated itself below the margin bar.
- **Version freshness needs LAZY reads, not just source-parsing (10-22)**: the
  deck parsed ENGINE_VERSION from engine source (single-source rule) but stored
  it in a MODULE-LEVEL constant; the server keeps the module imported across
  its 45s rebuilds, so the header froze at the container-start version — it
  displayed a version three releases old while the panel fed at build time
  showed the current one (self-contradicting deck). Any "read from source of
  truth" value inside a long-lived process must be re-read per build (cheap TTL
  cache), never snapshotted at import. The SOURCE was live; the READER was
  frozen. Corollary from the same session: when a displayed number looks
  wrong, check the semantic frame too — the "0 trades today" that prompted the
  audit was CORRECT (the day counter is UTC, the operator's midnight had
  passed; the hero cell now says "· UTC day"), and it led to the panel that
  was actually lying. FOLLOW-UP (10-24): the TTL fix was INCOMPLETE and the
  bug recurred — the readers went lazy, but the module-level ENGINE dict
  still evaluated them at import, so the header froze again at the
  container-start version (v4.9.3 beside a live v4.9.6 panel) while passing
  every "the fix is deployed" check. A lazy reader is necessary but not
  sufficient: every CALL SITE that snapshots it into a module-level constant
  must move inside the per-build path. And verify freshness on the RENDERED
  artifact (curl the published data.js, compare the header field against the
  engine's own status snapshot), not in the code — two fields disagreeing in
  one payload is the tell.
- **Observability lesson (10-15)**: the deck hardcoded gate thresholds
  (margin≥0.25, cooldowns 15/30) and drifted from the engine twice. UI must PARSE
  thresholds from engine source, never hardcode. Also: account-level P&L must be
  % of LAUNCH capital, not sum-of-per-trade-%-of-stake (stakes varied 18→120 EUR;
  the naive sum showed -32% vs the true -14.2%).
- **v5.0-mom (10-25) — LLM SIGNAL RETIRED, DETERMINISTIC MOMENTUM LIVE**: the
  kill-gate on the Jev perp signal fired exactly as pre-committed (n≥30 closes
  since v4.9 OR the review date): 79 closes, 39% WR, net −1.47 USDC, gross ≈ 0.
  The best window — after the v4.9.5 prompt reframe — grossed +0.03%/trade
  against a 0.086% round-trip cost: the signal covered a third of its own fees
  and sat within ~1 SE of zero. Verdict: NO EDGE → close the book and park the
  collateral in flexible savings, executed under the standing pre-authorization
  (flat verified on-exchange; the final trade closed on its server-side
  take-profit). The operator then overruled the shutdown ("i gave you about 130
  dollars to experiment live — that's the idea of the public website"), so the
  book relaunched the same day on a NEW DETERMINISTIC ALGORITHM, not the old
  signal. v5.0-mom (`okx_mom.py`): rank the ≤30 most liquid XPERPs (turnover
  floor; vol_usd = volCcy24h × last — the unit fix carried over) by 14-day
  return via 4H candles, long the strongest 2 / short the weakest 2, capped
  notional per leg at 1× isolated, 24h-ANCHORED full rebalance (no mid-period
  refills after stops = the backtest's semantics), 1.5% protective stop
  (server OCO + poll backup), zero LLM calls per cycle. Selected from a 100-day
  1H OHLC backtest (25 instruments, taker costs per leg, intrabar stops with
  gap-through conservatism, out-of-sample time split): +0.857%/d net, halves
  +0.440/+1.285 — positive in BOTH — and robust at every stop level 1–3%
  (stops IMPROVED the raw +0.779 signal). Honest biases declared at deploy:
  the universe was selected by CURRENT turnover (selection bias inflates the
  second half — the live expectation is the first half's +0.44%/d), funding
  was excluded from the simulation (a carry tailwind for the short leg on
  premium-funding names), and 100 days is one regime. New kill-gate: 30
  rebalances OR 14 days, live ≤ 0 → close again under the same
  pre-authorization. The first live book proved the stop path within 9 minutes
  (the top-ranked +188%/14d instrument stopped at −1.63%, server-side).
- **NEVER str() A PRICE FLOAT (10-25)**: two launch legs died with 51000
  "Parameter px error" — `str(5.9e-06)` renders `'5.9e-06'` and the venue
  rejects scientific notation. Every px-string site in the shared entry path
  (maker body/TP/SL + IOC body/TP/SL — six in total) now goes through a
  plain-decimal formatter (12dp, trailing zeros stripped), unit-tested on
  sub-1e-4 values. Related: a 99% take-profit stub produced absurd trigger
  prices on cheap coins; a far-but-sane stub (25%) keeps the OCO shape valid.
- **A QUIET BOOK STILL NEEDS A HEARTBEAT (10-25)**: the momentum engine logs
  only on scan/rebalance/exit, so a clean 24h hold emits ZERO events — and a
  public deck's liveness strip reads the last log event's timestamp, so a
  perfectly healthy engine reads as paused within an hour. One throttled
  heartbeat event (30-min) keeps the tape honest without log bloat. General
  form: after swapping engines, re-derive every freshness/health indicator
  from the NEW engine's actual event cadence, never the predecessor's.
- **PAPER MODE (v4.9.7, 10-25)**: `--dry-run` became true shadow bookkeeping —
  positions persist in meta (the manage loop runs the full bracket/trail/
  time-stop lifecycle against live prices), a fixed paper bankroll replaces the
  real balance read (which is zero when collateral is parked), virtual fees are
  conservative, and every event is flagged `dry:true`. Built for the kill-gate
  transition; the operator preferred small live capital over paper validation,
  but it remains the zero-capital lane for validating future candidates.
  Lesson either way: a dry-run that logs intent WITHOUT persisting state never
  exercises the exit side — the half where the bugs live.
- **RETIRING A SIGNAL ENGINE MEANS RE-LABELING EVERY PUBLIC SURFACE (10-25)**:
  the swap required suppressing the retired engine's frozen decision event so
  it could not present itself as the "latest decision" (it was rendering in
  the gates panel through a fallback path), a strategy-conditional desk-note
  prompt (hardcoded lines asserting "the Jev model returns typed decisions"
  and "never in paper mode" both became false), a `strategy` field threaded
  through the payload into trading_state, and momentum-native rows in the
  gates panel (rank window, current legs, rebalance countdown, stop policy).
  Checklist for any engine swap: header version source, decision-panel feeds,
  desk-note prompt claims, staleness/liveness feeds, per-trade replay
  assumptions. Deploy-time discipline that saved this one: patch scripts must
  compile-gate a TEMP COPY before writing production (a write-then-compile
  ordering briefly put a syntactically broken generator on disk), and an
  inserted definition whose else-branch contains a later anchor's literal
  makes that anchor non-unique — order the replacements so the swap precedes
  the definition.

## 3. Pilot architecture (v3 momentum regime, 09-29; v3.1 §2c supersedes universe+bracket bits)

- v3 rebuilt after 24h live post-mortem: 29 opens, ledger-verified ~3W/13L,
  −€5.3 (over half was fees). v2 failures: +0.8%TP/−0.5%SL needs 58% win rate
  to beat ~0.3% round-trip cost (got ~20%); pure-maker entries missed ~45%
  (adverse selection — fills only came on moves that reversed); instant
  re-entries after stops churned fees; close_detected matched stale fills.
- v3 rules (all in okx_jev.py constants):
  - Universe: NEAR, ZEC, SUI, LINK EUR only (fee-wall clearers).
  - Bracket: SL −0.7% (server OCO) / backstop TP +1.5%; TRAILING exit owns wins:
    arm at HWM +0.6%, exit on 0.35% giveback, floor +0.1% (HWM persisted in
    positions.json; updated each manage()).
  - Entry quality gates IN CODE before Jev: vol30 ≥0.9%, chg30 >+0.3%,
    bars_up ≥3/6, range30_pos 30–92, taker_buy_share ≥0.50; CONF_BAR 0.50,
    NOUL_BAR 0.40. Jev question: rise ≥0.6% in 45min.
  - Fill path: post_only 25s → partial fill kept (maker_partial) or IOC market
    chase if ask within 0.15% of bid (taker_chase); else cancel+log.
  - MAX_OPEN 2, MAX_ENTRIES_PER_CYCLE 1; stake still = all free EUR / slots;
    stables swept every cycle.
  - COOLDOWN_MIN 30 per pair after a LOSING exit (risk_state.json exits map).
  - Daily breaker: realized equity (EUR, via balance eqUsd/cashBal FX derivation)
    ≤ −2.5% of UTC-day start → writes STOP file itself (day_roll at midnight).
  - Time-stop 30m losing, stale-exit 90m flat <0.45%.
- CRITICAL prune rule (v3 fix): position-is-gone check MUST use cashBal (or skip
  while a live OCO exists for the inst), NEVER availBal — a live OCO sell leg
  FREEZES the whole base balance so avail≈0 while the position is fully open.
  v2 pruned three live positions this way (dropped trailing/time management,
  only server OCO remained). Manage() now cancels OCOs FIRST, re-reads balances,
  then sizes the market sell; close_dust path no longer pops meta (prune loop
  detects the server-side exit and marks cooldown).
- close_detected fill matching: only sell fills with ts >= opened_ts−5s —
  matching the newest fill blindly reports stale exits (+4.59% phantoms).
- v2 history (kept for context): maker-first post_only 45s, TP0.8/SL0.5 OCO,
  25m/60m rotation exits, 3 slots, all-funds stakes + USDC→EUR sweep.

### Old v2 section (superseded)

## 3b. Pilot architecture (v2 volume tuning, 09-28)

- v2 changes ("multiple deals a day, fast scalping, trade with all available funds"):
  - Universe from a 24h 5m-bar fee-wall study on EUR pairs: NEAR 8.7% of bars move
    >0.8%, SUI 4.2%, LINK 3.1%, XRP 0.7%, BTC/ETH/SOL ~0% — PAIRS list ordered by
    realized volatility, MIN_VOL24H_EUR=200k liquidity floor.
  - MAKER-first entries: post_only limit at bid, wait FILL_WAIT_S=45s, cancel if
    unfilled (entry_unfilled logged). Maker 0.1% halves entry cost vs taker.
    WATCH: on fast-rising pairs the bid runs away → unfilled rate may be high;
    if fill rate < ~50% over a day, switch to IOC-limit at ask or hybrid.
  - Bracket TP +0.8% / SL -0.5% anchored at bid (expected fill); standalone-OCO
    fallback re-anchors to TRUE fill px. TIME_STOP 25m losing, STALE_EXIT 60m
    flat (<0.4%) — capital rotation keeps slots cycling.
  - Up to 3 entries per 5-min cycle while slots free.
  - Stake = free_EUR * 0.98 / slots_left — full bankroll deployed across slots.
  - sweep_stables(): USDC/USDT → EUR market sell each cycle (all-funds directive).
- `manage()` deterministic exits; server-side OCO is first line, poll is backup +
  time-stop owner. Prune rule: avail < minSz (dust is >0; `avail > 0` never prunes
  → phantom positions block MAX_OPEN — verified live).
- `decide()`: EUR cash, per-pair dense state (bid/ask/spread, day_chg vs open24h,
  chg_30min/range30_pos/bars_up_of_6/vol_30m from 5m candles, pos_off_day_low),
  ONE parallel Jev call (p_up_<base> noul + best_action choice), gates in code:
  CONF_BAR 0.35 / NOUL_BAR 0.30 (volume regime; EV cost documented) /
  MAX_SPREAD 0.10% / MAX_OPEN 3 / STAKE 18 EUR. Assert tradeable map non-empty
  BEFORE the Jev call (empty set = no_trade conf 1.0 masquerade, inherited lesson).
- News/sentiment FINAL architecture (prefer OKX-native data where available): orbit news
  (`okx news`) is DEAD ON EEA (see §0) — use OKX-NATIVE public stats instead, all
  verified working on eea.okx.com keyless:
  - `/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=X&period=5m` (crowd positioning, >1 = mostly long)
  - `/api/v5/rubik/stat/taker-volume?ccy=X&instType=CONTRACTS&period=5m` (buy/(buy+sell) = flow pressure)
  - `/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` (market-wide risk tone)
  - `/api/v5/public/economic-calendar` (macro catalysts; needs key auth on eea, 401 keyless)
  - NOT available on eea: rubik long-short-POSITION-ratio, put/call ratio (404).
  Plus keyless RSS catalyst.json (news_feed.py in the okx workspace, 30-min guard,
  called from cycle mode) for headline catalysts. All injected into Jev state:
  per-pair `sentiment` block, `market_funding_bps`, `macro_events_24h`, `news_catalysts`.
- No stocks on OKX (EEA spot crypto only) — stock path dropped entirely; the
  overnight/weekend stock rules no longer apply, crypto holds stop/target gates 24/7.
- Kill switch: STOP file in the workspace dir halts entries, manage keeps running.
- Log every round to okx_jev_log.jsonl (state, answers, action, fills, realized fees).

## 4. Pitfalls

- 51000 "Parameter sz error": sz below minSz or wrong rounding — check lotSz/minSz
  from /api/v5/public/instruments (cache per session).
- **Dust phantom positions**: OCO/fee-net exits leave dust > 0 in the base ccy, so
  a prune rule of `avail > 0` NEVER fires and dead metadata occupies MAX_OPEN slots
  (verified live 09-28: two phantom positions blocked entries for ~20min). Prune when
  `avail < minSz`, and log the real exit via /api/v5/trade/fills sell side.
- **Thin-book illusion**: on EEA EUR listings, 24h volume can be real while
  resting depth is dust (CT-EUR: €597k vol24h, €662 top-5 depth). ALWAYS check
  /market/books depth before sizing an entry; an IOC into a thin book fills
  far off mid ("taker_chase" makes this worse — it fills whatever is there).
- **availBal vs cashBal (v3, verified live 09-29)**: a LIVE OCO sell leg freezes
  the entire base balance → availBal≈0 while the position is open. Pruning on
  `avail < minSz` deletes LIVE positions (happened 3×). Prune on `cashBal < minSz`
  AND no live OCO for the inst; before any manage() market sell, cancel the OCO
  first and re-read balances (frozen avail would size the sell to dust).
- **Stale fill matching**: close_detected must filter fills by ts >= opened_ts;
  newest-fill matching misreports old exits (phantom +4.59% ZEC "win" 09-28).
- **Ground truth = fills ledger**: log upc values can lie (see above); for any
  stats/reporting always compute realized PnL from /api/v5/trade/fills
  (sell proceeds − buy cost − fees, fee converted at base-ccy EUR rate).
  totalEq is USD; EUR equity = totalEq / (EUR row eqUsd / cashBal).
- 51008 insufficient balance: tried to sell gross qty incl. fee-deducted part.
- 51020 on attach: OCO sz ≥ actual post-fee balance required.
- `okx config set demo false` → "Unknown config key"; edit config.toml directly.
- `okx news` 404 on API-key auth — OAuth-only module, not a bug.
- Cron output runs LOCAL tz, trading logic UTC — `date -u` before blaming time rules.
- CLI prefers API key over OAuth always; a broken key profile blocks OAuth login.
