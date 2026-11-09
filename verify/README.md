# Proof of Trades

The track record on <https://trader.obekt.com> is published as a **signed,
hash-chained, GitHub-anchored log** so you never have to take the dashboard's
word for anything.

## How it works

- `engine/verify_snapshot.py` runs on an isolated systemd timer on the trading
  host (daily 00:03 UTC). It is **never imported by the trading engine** and
  only ever calls OKX **GET** endpoints (`/account/balance`, `/account/positions`)
  with the existing account key, plus counts events in the engine log.
- Each snapshot is signed (ed25519) and appended to `chain.jsonl`, where every
  line embeds the hash of the previous raw line. Editing history breaks every
  later link.
- After each append, the chain head is committed to this repo
  (`verify/HEAD.json`) by a write-only deploy key — third-party timestamping of
  "this was the head at this time".

## Verify (run this, don't trust)

```bash
pip install cryptography
curl -sL https://trader.obekt.com/verify.py | python3 -
```

The script re-checks every signature and chain link against the public key
embedded in it, then compares the head with the GitHub anchor
(`verify/HEAD.json`). Exit code 0 = chain unbroken and anchored.

## Trust anchors

1. `verify/pub.pem` — the signing public key (pinned verbatim inside `verify.py`).
2. This repo's commit history — every head was committed by GitHub clock order.
3. OKX itself — equity in each snapshot is what the exchange reported that day.

## Honest limits

- The key holder could stop signing at any time (gaps are visible, quits are not).
- Snapshot cadence is daily; intraday swings are not sealed.
- Nobody can prove they will trade tomorrow. Everything else is mechanically
  checkable with the command above.

## Files

| file | role |
|---|---|
| `engine/verify_snapshot.py` | signer (runs on the server; private key lives ONLY there, mode 600) |
| `verify/verify.py` | public verifier (also served at trader.obekt.com/verify.py) |
| `verify/pub.pem` | public key (pinned inside verify.py) |
| `verify/HEAD.json` | machine-anchored chain head, updated daily |
