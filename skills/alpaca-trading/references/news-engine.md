# News-driven stock engine: design, LLM contract, validation, telemetry

Reference implementation: `engine/alpaca_stocks.py` in this repo.
CURRENT: v13.0-volbrackets (2026-11-25, kill-gate replacement of v12.0-intraday:
34 closes, realized net −$0.09 ≤ 0 at the 30-close gate). v13 keeps the v12
intraday frame — medium-quality substantive catalysts admitted, 1–6h holds,
45m per-symbol re-entry cooldown, MAX_OPEN 3, MAX_LLM_CALLS 8, CONF_FLOOR
0.55, no entries after 15:30 ET, FLAT ALL 15:50 ET — and changes what the v12
book proved broken:
  - brackets are CODE-computed from the symbol's 14-session daily ATR (IEX):
    SL = clamp(0.35×ATR%, 0.6, 2.0), TP = clamp(1.5×SL, 1.0, 3.0). The LLM is
    no longer asked for target/stop — v12's model templated 1.2/0.8 on 32/39
    trades and the fixed 0.8% stop sat inside the noise band (53% stop-outs);
  - prompt asks for calibrated confidence across the full range (v12 anchored
    at 0.62 on 30/39 calls);
  - deep-dip rule: day_chg ≤ −4% requires impact=high AND conf ≥ 0.70;
  - no pyramiding (a held symbol is never re-entered — symbol-keyed meta was
    being overwritten), cash-clamped leg budget, fail-soft entry errors.
Kill-gate v13 (pre-registered 11-25): 30 closes OR 7 days, net ≤ 0 → replace.
The v11 catalyst-only design below is the original baseline (48h holds, one
entry/symbol/day, TP 1–5 / SL 0.8–3, conf 0.60) — kept for reference.

## Pipeline + deterministic gates (code owns risk; LLM never overrides)

1. Clock: entries/exits only while `is_open` (regular hours). Outside hours:
   scan + accumulate news, defer exits to next open (log the deferral).
2. News scan: fresh headlines (90-min window) on a ~60-name liquid universe
   (30 mega-caps + 30 news-heavy mid/high-beta), TWO sources: Alpaca wire +
   Yahoo per-ticker RSS (15-min cache, >=0.7 token-overlap cross-source dedupe,
   stable md5 ids — builtin hash() is salted per process, so hash()-based ids
   change every restart and the seen-ledger re-evaluates the same headlines),
   dedupe by id, one evaluation per headline, 45m post-exit re-entry cooldown
   per symbol, never re-enter a symbol already held (no pyramiding).
   On the closed→open transition widen the window to ~18h so overnight and
   pre-market catalysts are visible at the open.
3. Pre-gates BEFORE spending an LLM call: ask-vs-last deviation ≤ 2.5%
   (ghost-quote guard), quote age ≤ 90s, day-change chase cap (+7%) and
   knife floor (−9%). The spread gate is a 10% disaster guard ONLY — IEX BBO
   spread is not the NBBO (routinely 2–4% on liquid names) and Alpaca routes
   to the inside market; tight IEX spread/ask gates (1%/0.35%) rejected most
   legitimate candidates (TSLA, COIN) while measuring nothing about real
   execution cost. News fetch: use the server-side `start` param (RFC3339)
   PLUS a client-side created_at re-filter, and paginate to the token end
   (20-page cap) — NEVER early-stop on the first item older than the cutoff:
   the wire sorts by updated_at, not created_at, so old-but-recently-updated
   items interleave with fresh ones and early-stop silently truncates the
   window. A transient `start`-returns-0-rows failure is possible; the client
   re-filter + a no-start probe distinguish API outage from a genuinely quiet
   tape before touching the fetch logic.
4. LLM decision (≤8 calls/cycle): halogen via chat.obekt.com/v1, temp ~0.2,
   strict-JSON system prompt. Schema: action buy|no_trade, confidence 0–1,
   impact high|medium|low, horizon_hours, rationale. NO target/stop — exit
   geometry is code-computed from measured volatility (see step 7).
5. Post-gates: confidence ≥ 0.55, impact ≠ low, slots free (MAX_OPEN 3),
   deep-dip rule (day_chg ≤ −4% needs impact=high AND conf ≥ 0.70).
6. Sizing: min(equity × 0.98 / MAX_OPEN, cash × 0.98) per leg, fractional
   qty = budget/ask, min $1 notional; hard notional bug-guard far above equity.
7. Brackets (v13, code-computed): SL = clamp(0.35 × ATR14%, 0.6, 2.0),
   TP = clamp(1.5 × SL, 1.0, 3.0), enforce TP ≥ 1.5×SL; ATR from 14-session
   daily IEX bars, cached per symbol per day, wider fallback if the fetch
   fails; engine-managed (no server brackets on fractional). Time exit 6h,
   flat everything 15:50 ET.
8. Daily breaker: equity ≤ day-start × 0.92 → STOP file marked
   `breaker <date>`, auto-clears on UTC rollover; manual STOP/STOP_PERP
   persists. Orphan reconciliation every cycle (adopt exchange positions
   without meta; log vanished ones) — never fly blind.

## LLM decision contract robustness

- Parse ladder: direct json.loads → outermost balanced-brace block →
  field-level regex for action/confidence/impact (rationale optional). Only
  truly unparseable output fails closed. v13 does not request brackets at
  all; legacy outputs that still carry them parse fine and are ignored.
- Prompt: "no prose outside JSON; never use double-quote characters inside
  rationale" — the rationale string is where models break JSON.
- Fail-closed events: log the error WITH a snippet of the raw answer (debug
  visibility) + a no_trade event; never let an LLM exception crash the cycle.
- Unit-test recipe: call the decision function directly with (a) a synthetic
  strong catalyst (guidance raise + buyback) against live price context →
  expect buy with sane conf/brackets, (b) a noise item (conference appearance)
  → expect no_trade. Live news often gate-rejects before the LLM, so a clean
  dry cycle does NOT prove the LLM path works.

## Pre-live validation: news→drift study (cheap, honest)

1. Collect N days of headlines for ~8 universe names (news API, paginated).
2. Fetch hourly bars over the same window (IEX if SIP 403s on long ranges).
3. Measure signed + absolute drift at t+4h / t+24h trading hours after each
   headline vs the UNCONDITIONAL all-hours baseline.
4. Read it honestly: if post-news |drift| ≤ baseline, raw headlines have no
   blind edge — the LLM catalyst filter IS the strategy hypothesis; say so.
   Calibrate TP/SL to the measured post-news move distribution (e.g. p90
   |4h move| ~2.3% → TP 1–3% reachable on real catalysts).
5. News-driven LLM decisions cannot be cleanly backtested (historical
   news+point-in-time context is leaky/expensive) — launch with a
   pre-registered kill-gate instead of a fake backtest number.

## Telemetry contract (deck + chain compatibility)

The engine REUSES the OKX-era filenames and event vocabulary so the public
deck's `normalize_perp_events` adapter and the signed chain keep working:

- `perp_log.jsonl` events: perp_open {inst, side, contracts, notional_usd,
  fill_px, conf, realized_fee, entry_type, headline, bracket}, perp_close
  {inst, side, reason, upc, age_min, fill_px, realized_fee, notional_usd},
  perp_close_detected, perp_jev {choice: buy_<SYM>|no_trade, chosen_inst,
  chosen_side, conf, noul, probabilities:{buy_<SYM>:conf}, pairs:[{inst,
  base, headline, impact, day_chg_pct, target_pct, stop_pct}],
  collateral_usdc, budget_usd, input_tokens, output_tokens, rationale},
  perp_no_trade, perp_gate_reject, perp_universe, mom_scan {signal:"news",
  longs/top/bottom: (sym, day_chg) tuples, shorts: []}, mom_heartbeat,
  perp_dry_open. Every record: ts, engine, strategy, dry.
- `perp_positions.json` meta keyed by SYMBOL: opened_ts, side, contracts,
  entry_ref, notional_usd, strategy, news_headline, jev_conf, bracket,
  hwm_pct, algo_ok:false, entry_ordId, max_hold_h.
- `perp_status.json`: engine (ENGINE_VERSION), usdc_equity (KEY NAME IS
  HISTORICAL — value is Alpaca USD equity), eur_float:0, account_total_usd,
  collateral_ccy:"USD", halted, dry, n_positions, positions[], strategy,
  venue, ts. The deck and signer read this file; keep the shape.
- `stocks_news.json`: [{ts, headline, symbols, source, url}] top-12 cache
  for the deck news panel; reader must tolerate absence.
- Deck version labels parse ENGINE_VERSION from the engine SOURCE — point
  the parser at the new file; never hardcode.
- At cutover: archive old-era files to `*_okx_retired`, start fresh logs;
  the signed chain continues unbroken (same key/seq), payload keys gain
  venue-specific names (alpaca_equity_usd) — keep verifier display branches
  tolerant of both eras. Capital base for realized-% = money actually put
  into the NEW book (single capital event at cutover), not the old venue's.
  Archive the RETIRED SPOT book's risk_state.json/positions.json too — the
  deck reads them and will render months-old EUR cooldowns as current risk.
- Deck tape rendering: stocks perp_jev events must render as kind MODEL with
  the rationale text — the legacy JEV kind + `p[buy_X=conf]` format misreads
  a no_trade's confidence as a buy probability. Empty panels need explicit
  "no closed trades / no model calls yet" states, and panel copy must name
  the TRUE data source (the deck reads the engine log of Alpaca fills, not a
  fills ledger). After a venue rebrand, audit EVERY live-path renderer string
  (grep X-PERP/USDC/OKX/JEV) — header-only checks miss panel bodies.
