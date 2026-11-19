# Alpaca API + data quirks (verified live)

Auth headers on both hosts: `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`.

## Hosts / endpoints

| Host | Path | Notes |
|---|---|---|
| api.alpaca.markets | /v2/account | equity, cash, buying_power, status |
| api | /v2/account/configurations | fractional_trading, max_margin_multiplier, no_shorting |
| api | /v2/clock | is_open, next_open, next_close (ET) |
| api | /v2/orders, /v2/orders/{id} | market/limit; poll status→filled; filled_avg_price, filled_qty |
| api | /v2/positions | symbol, qty, avg_entry_price, unrealized_pl, asset_class |
| api | /v2/assets?asset_class=us_equity\|crypto | tradable/fractionable/marginable/shortable/min_order_size |
| api | /v2/assets/{symbol} | single stock; crypto is listed as PAIRS (BTC/USD), no bare coins |
| api | /v2/wallets, /v2/wallets?asset=USDC | crypto deposit addresses (see below) |
| api | /v2/wallets/transfers | INCOMING/OUTGOING history, status PROCESSING/COMPLETE/FAILED |
| data.alpaca.markets | /v2/stocks/bars | timeframe=1Hour/1Day, feed=iex/sip, start/end, limit, page_token |
| data | /v2/stocks/snapshots?symbols=...&feed=iex | dailyBar, prevDailyBar, latestTrade, latestQuote, minuteBar |
| data | /v1beta1/news | free Benzinga-derived headlines (see below) |
| data | /v1beta3/crypto/us/bars | crypto bars if ever needed |

- Snapshots/bars/news on the TRADING host → 404. They are data-host endpoints.
- Bars pagination: `next_page_token`; multi-symbol + long-range + feed=sip can
  403 on the free plan while short ranges work — fall back to feed=iex for
  long studies, per-symbol requests if a batch 403s.
- Quote/trade timestamps are nanosecond RFC3339 — truncate fraction to 6
  digits before fromisoformat on Python < 3.11.
- IEX latestQuote BBO is a noisy proxy for NBBO (routinely 2–4% wide on
  mega-caps). Gate on ask-vs-last-trade deviation (≤2.5% ghost guard) and
  quote age; raw spread only as a ~10% disaster guard — tight IEX spread
  gates reject legitimate candidates while measuring nothing about real
  execution cost (Alpaca routes to NBBO).

## News API

`GET data.alpaca.markets/v1beta1/news?limit=50&sort=desc[&symbols=SYM][&page_token=...]`
Free on the trading account. The `start` param is UNRELIABLE (returns 0 rows
for valid windows while fresh items exist) — fetch newest pages without it
and filter `created_at` client-side, stopping when items predate the cutoff.
Fields: headline, summary (~400 chars usable),
symbols[], source, created_at, url; `content` is empty on free tier. Filter
to your universe client-side; dedupe by id; persist seen-ids so a headline is
evaluated once. One symbol may carry many routine items — expect a high
no_trade rate from the decision model; that is selectivity, not a bug.

## Fractional orders

- Fractional market orders work in regular hours; qty precision ~4+ decimals,
  min notional $1.
- Fractional orders CANNOT carry server-side bracket/OCO legs → manage TP/SL
  in the engine loop (poll + market exit). Document the poll-granularity
  slippage and overnight gap risk honestly.
- Use a unique client_order_id per entry (idempotency on retry).

## Crypto wallets / funding route (OKX → Alpaca verified path)

1. Alpaca deposit address: `GET /v2/wallets?asset=USDC` → {asset_id, address}.
   The path form `/v2/wallets/USDC` 404s; `/v2/assets/{asset_id}` lookups of
   wallet ids can be stale (40410000) — trust the ?asset= query form. Confirm
   the address against `/v2/wallets/transfers` history (a prior COMPLETE
   INCOMING to the same address proves the route).
2. Deposits: BTC, ETH and ERC-20 tokens on **Ethereum mainnet ONLY** — no
   L2s (Base/Arbitrum/Polygon/etc. will not credit). Deposits free (sender
   pays gas); several block confirmations; possible AML review delay.
3. OKX side: trade-only API keys return **50120 (no permission)** on
   `/api/v5/asset/transfer` AND `/api/v5/asset/withdrawal`; the OAuth CLI
   scopes are read/trade only. Plan the withdrawal as a MANUAL operator step
   (address-book entry + Trading→Funding transfer + withdraw) or a key
   permission edit — probe the key scope BEFORE building the cutover
   sequence around it. OKX signing covers path+query: put params in the
   path string. `GET /api/v5/asset/currencies?ccy=USDC` lists chains/fees —
   USDC-ERC20 fee ~0.46 USDC, minWd 1. Funds must sit in the FUNDING account
   to withdraw.
4. Credit lands as a USDCUSD position. Convert to stock buying power:
   `POST /v2/orders {symbol:"USDC/USD", qty, side:"sell", type:"market",
   time_in_force:"gtc"}` → USD cash (0.25% taker on the crypto sale).
   CRYPTO orders reject `time_in_force:"day"` AND reject an omitted tif
   (422 both ways) — use `gtc`. Stock orders use `day`. Poll the POSITION
   qty for deposit readiness (transfers show PROCESSING→COMPLETE; the
   position appearing is the trade-ready signal — confirmations can take
   ~15 min, so poll on a bounded loop instead of assuming instant credit).
5. Alpaca WITHDRAWALS (outbound) need address whitelisting + 24h wait +
   travel-rule beneficiary info — a different, slower flow than deposits.

## Account facts worth re-checking after venue changes

`/v2/account` margin_multiplier/daytrade_count fields are deprecated under
the post-PDT intraday margin framework (return None/0) — use
`/v2/account/configurations` for the real trading flags.
