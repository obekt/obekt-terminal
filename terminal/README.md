# `terminal/` — the read-only observation deck

A Bloomberg-style text UI over the engine's live telemetry. Pure HTML/CSS/JS —
**no framework, no build step, no CDN**. A generator turns the engine's JSONL
log into a static `data.js`; the page renders it and re-polls every 10 seconds.

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

Every panel, chart, and click-to-replay dossier works off that sample data.

## Run it against the live engine

```bash
cd terminal
export OKX_TERMINAL_BASE=~/.hermes/workspace/okx     # dir holding okx_jev_log.jsonl + state
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

## What's on screen (25 panels)

- **Hero strip** — equity, day P&L, cycles run, Jev calls, latest vote, uptime, model cost.
- **Session-liveness strip** — counts events/entries/exits *since you opened the tab*, so you can watch it work in real time.
- **Desk note** — the local LLM's read of the tape.
- **Performance** — win rate, profit factor, avg win/loss, streaks, best/worst, fees.
- **Latest Jev call** — probability bars + what the engine *did* (`▸ ORDER SENT` / `✕ BLOCKED BY RISK LAYER`).
- **Decision gates** — the ✓/✕ checklist (margin, conf, noul, depth floor, breaker, cooldowns).
- **Equity curve** — SVG, every closed trade, peak/trough labeled.
- **Selectivity funnel** — pair-scans → filters → Jev evals → buy votes → opens (why it trades rarely).
- **Decision scatter** — every buy-vote as margin×noul vs the engine bars, colored by outcome. Grey = model wanted in, code said no.
- **Exit-engine breakdown · P&L by hour · P&L per instrument.**
- **Universe scan · Open positions** (live uPnL + 5m candle track + protection state).
- **News · Sentiment · Macro** — the exact context riding in the prompt.
- **Jev vote tape · Daily P&L · Architecture · Model stack · Cost optimization · Decision economics.**
- **Trade ledger** — **click any row → full decision replay.**
- **Event tape** — the raw log; new rows flash.

### The decision-replay dossier (the click-any-trade view)

Each closed trade has an immutable `dossiers/<tid>.json` rendering:
0. **candlestick chart** (real OKX 5m OHLC) with ▲entry / ▼exit + cyan TP / red SL lines and ◈ trail-arm ticks
1. the model vote (probabilities + pick/conf/margin/noul)
2. the exact market state Jev was shown (all candidate pairs, cash, stake, funding, prompt size)
3. the news + sentiment + macro that were in that prompt
4. the pre-entry gauntlet it survived + its bracket
5. the full lifecycle timeline (entry → trail arms → exit)
6. the ledger outcome + an LLM post-mortem + a verbatim excerpt of the strategy prompt

## Files

| file | role |
|---|---|
| `index.html` `style.css` `app.js` | the UI (no deps) |
| `generate.py` | engine log + public market data (+ optional LLM) → `data.js` / `data.json` / `dossiers/` / `og.png`. **Read-only over the engine; never signs requests, never reads exchange keys.** |
| `serve.py` | static server + 45s rebuild loop; injects absolute OG urls when `PUBLIC_BASE` is set; tiered cache headers |
| `make_og.py` | renders the 1200×630 Open Graph share card (Pillow) so shared links preview live numbers |
| `deploy.sh` | one-shot build + rsync a clean static bundle to a web root or `user@host:path` |
| `sample_data/` | frozen real telemetry for zero-credential preview |

## Environment variables

| var | default | purpose |
|---|---|---|
| `OKX_TERMINAL_BASE` | `~/.hermes/workspace/okx` | engine workspace to read |
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

`generate.py` **never writes to the engine workspace, never signs exchange
requests, and never reads `credentials.json`.** Its inputs are the JSONL/JSON
state files, public market endpoints, and (optionally) the LLM relay. Its only
writes are inside `terminal/` (`data.js`, `og.png`, `dossiers/`, caches).
