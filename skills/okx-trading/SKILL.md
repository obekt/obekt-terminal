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
