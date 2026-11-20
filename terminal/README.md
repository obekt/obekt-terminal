# `terminal/` — the read-only observation deck

A Bloomberg-style text UI over the engine's live telemetry. Pure HTML/CSS/JS —
**no framework, no build step, no CDN**. A generator turns the engine's JSONL
log into a static `data.js`; the page renders it and re-polls every 10 seconds.

**Venue:** the live book is **Alpaca US stocks** (news-driven, long-only) since
2026-11-19. The deck is **stocks-first**; the retired OKX crypto books (X-Perps +
spot EUR) and their sentiment feeds live in one **collapsed `RETIRED BOOKS` block**
at the bottom, with their final losing records intact.

**It is observation-only by construction.** `cursor:default`, `user-select:none`,
no links or handlers anywhere except the trade-ledger replay viewer. You cannot
place an order from this page. That's deliberate — it's a window into a machine
that runs itself.

## Preview it in 30 seconds (no keys, no engine)

Ships with **real frozen telemetry** from our live run so you can see exactly
what it looks like before wiring anything up:

```bash
cd terminal
cp sample_data/data.js data.js
cp sample_data/data.json data.json
cp -r sample_data/dossiers dossiers
cp sample_data/og.png og.png
python3 -m http.server 8770
# → http://localhost:8770
```

Every panel and chart works off that sample data. The live stocks book has no
closed trades yet, so the ledger shows its honest empty state (each row will link
to a decision replay once the first fill lands). The replay viewer is wired and
the 500+ retired OKX dossiers are retained in `sample_data/dossiers/`.

## Run it against the live engine

```bash
cd terminal
# OKX_TERMINAL_BASE is a HISTORICAL env name (kept for compatibility); it now
# points at the Alpaca engine's dir — the one holding perp_log.jsonl + state.
export OKX_TERMINAL_BASE=/path/to/engine
python3 serve.py --port 8770                          # rebuilds data.js every 45s, serves the page
```

`serve.py` regenerates from the live log in the background and serves the static
files. Point it at your engine's workspace and it just works. The engine must be
running (see `../engine`) for the numbers to move.

### Optional: desk commentary + per-trade post-mortems (a local LLM)

The generator can ask an **OpenAI-compatible** chat endpoint to write the desk
note and one-line trade debriefs. This is **optional** — without it the terminal
still shows all telemetry, just with fallback commentary. Use a **local** model
(LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim) or any cheap hosted endpoint:

```bash
export TERMINAL_LLM_URL="http://localhost:1234/v1/chat/completions"  # LM Studio default
export TERMINAL_LLM_MODEL="qwen3-flash"                              # any small instruct model
export TERMINAL_LLM_API_KEY="***"                               # local servers ignore it
export TERMINAL_LLM_KEY_ENV="TERMINAL_LLM_API_KEY"                 # which env var holds the key
```

It's hash-cached and rate-limited (desk note ≥5 min; debriefs capped per build),
so a local model costs nothing but compute. We run a Qwen-3-class flash model on
a self-hosted relay — the point is the *decision* model is separate and typed;
this LLM only ever **explains** facts already in the engine state.

## What's on screen

**Live (stocks) panels:**
- **Hero strip** — equity (USD), day P&L, cycles run, model calls, latest vote, uptime, model cost ($0 marginal).
- **Session-liveness strip** — counts events/entries/exits *since you opened the tab*.
- **Desk note** — the local LLM's read of the tape.
- **US Stocks — the live book** — equity, realized %, win rate, last halogen call (vote + confidence bar + impact + headline + rationale), status, open positions (engine-managed TP/SL), recent closed, scanned universe.
- **Decision gates** — the ✓/✕ checklist (news freshness, market hours, spread/ask/quote pre-gates, chase/knife, conf floor, impact, slots, bracket geometry, breaker, one-entry-per-symbol-per-day). Constants parsed from the engine source.
- **Equity curve · Selectivity funnel · Decision scatter** (stocks axes: confidence × day-move) **· Exit-engine breakdown · P&L by hour · P&L per symbol.**
- **Universe scan** — the 60-name list (Alpaca wire + Yahoo RSS) + day movers from the engine's own price context.
- **News** — the LIVE signal: fresh Alpaca News API headlines from `stocks_news.json`.
- **Decision vote tape · Daily P&L · Architecture · Model stack · Cost optimization · Decision economics.**
- **Trade ledger** — **click any row → full decision replay** (honest empty state until the first close).
- **Event tape** — the raw log as SCAN / GATE / MODEL / FILL / EXIT / RISK rows; new rows flash.

**Retired (collapsed `RETIRED BOOKS` block):** the OKX X-Perp final record (426
closes, 40.8% WR, −$26.68 net), the spot EUR record (132 trades, −€21.03), their
ledgers/replays, and the crypto sentiment feeds — all labelled retired, never
narrated as current.

### The decision-replay dossier (the click-any-trade view)

Each closed trade has an immutable `dossiers/<tid>.json` rendering:
0. **candlestick chart** (keyless Yahoo Finance 5m OHLC) with ▲entry / ▼exit + cyan TP / red SL lines
1. the model vote (stocks: confidence + impact + rationale; retired OKX: probabilities + margin/noul)
2. the exact market state the model was shown (stocks: the headline + price context; retired: candidate pairs, funding, prompt size)
3. the news catalyst that was in the prompt (stocks: the headline IS the signal)
4. the pre-entry gauntlet it survived + its bracket
5. the full lifecycle timeline (entry → exit signals → exit)
6. the ledger outcome + an LLM post-mortem + a verbatim excerpt of the strategy prompt

## Files

| file | role |
|---|---|
| `index.html` `style.css` `app.js` | the UI (no deps) |
| `generate.py` | engine log + keyless Yahoo Finance market data (+ optional LLM) → `data.js` / `data.json` / `dossiers/` / `og.png`. **Read-only over the engine; never signs requests, never reads broker keys.** |
| `serve.py` | static server + 45s rebuild loop; injects absolute OG urls when `PUBLIC_BASE` is set; tiered cache headers |
| `make_og.py` | renders the 1200×630 Open Graph share card (Pillow) so shared links preview live numbers |
| `deploy.sh` | one-shot build + rsync a clean static bundle to a web root or `user@host:path` |
| `sync-telemetry.sh` | optional whitelist rsync bridge: engine host → deck host (stocks-era `perp_*` files + retired spot history) |
| `refresh-sample.sh` | gated `sample_data/` refresh (fetch → secret-scan → schema-check → swap) |
| `sample_data/` | frozen real telemetry for zero-credential preview |

## Environment variables

| var | default | purpose |
|---|---|---|
| `OKX_TERMINAL_BASE` | `~/.hermes/workspace/okx` | **historical name** — engine telemetry dir to read (the Alpaca engine's dir, holding `perp_log.jsonl`) |
| `TERMINAL_STATE_DIR` | *(the code dir)* | where generated artifacts land (`dossiers/`, `data.js`, `og.png`, caches). Point at a persistent path/volume so a restart doesn't re-spend LLM calls |
| `TERMINAL_LLM_URL` | `http://localhost:1234/v1/chat/completions` | desk-commentary endpoint (any OpenAI-compatible) |
| `TERMINAL_LLM_MODEL` | `qwen3-flash` | model name at that endpoint |
| `TERMINAL_LLM_KEY_ENV` | `TERMINAL_LLM_API_KEY` | which env var holds the key (blank ⇒ local, no key) |
| `TERMINAL_CONTACT_LINE` | Obekt footer | the contact strip in the page footer |
| `PUBLIC_BASE` | *(unset)* | absolute base for og:image/og:url when hosting publicly |

## Host it publicly

The deployed site is 100% static — `data.js` + `dossiers/` + `og.png`. Build and
ship it anywhere:

```bash
PUBLIC_BASE=https://terminal.yourdomain.com ./deploy.sh user@host:/var/www/terminal
```

…then cron that on the box with the engine log:

```cron
*/2 * * * * cd /path/to/terminal && PUBLIC_BASE=https://terminal.yourdomain.com ./deploy.sh user@host:/var/www/terminal >> deploy.log 2>&1
```

No secrets leave the machine that has the log — `data.js` is public telemetry
only. Set `PUBLIC_BASE` so link previews on X/Telegram/etc. resolve the OG image.

## Safety

`generate.py` **never writes to the engine workspace, never signs broker
requests, and never reads `credentials.json`.** Its inputs are the JSONL/JSON
state files, keyless public market endpoints (Yahoo Finance), and (optionally) the
LLM relay. Its only writes are inside `terminal/` (`data.js`, `og.png`,
`dossiers/`, caches).