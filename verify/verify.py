#!/usr/bin/env python3
"""Independently verify trader.obekt.com's proof-of-trades chain.

You trust NOTHING served by trader.obekt.com except the raw chain bytes; the
trust anchor is the ed25519 public key embedded below, published in this repo
(verify/pub.pem) and pinned on the VERIFY page. The chain head is additionally
anchored to GitHub commit history: verify/HEAD.json.

    pip install cryptography
    python3 verify.py

What this proves: every daily snapshot of the exchange equity was signed by the
key holder, in order, without gaps or edits. What it does NOT prove: that the
key holder trades well, or that OKX itself is honest. Bring your own trust.
"""
import argparse
import base64
import hashlib
import json
import sys
import urllib.request
from typing import cast

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    sys.exit("missing dependency: pip install cryptography")

# ---- trust anchor (if you cloned THIS repo, you already have pub.pem; the
# embedded copy is what the author published; compare them) ----
PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAIvrC4MoVuFwyPpDTGNqlU9dJvn5A6lpPN9IxZ75rOr8=
-----END PUBLIC KEY-----
"""

CHAIN_URL = "https://trader.obekt.com/chain.jsonl"
ANCHOR_URL = ("https://raw.githubusercontent.com/obekt/obekt-terminal/main/verify/HEAD.json")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "obekt-verify/1"})
    return urllib.request.urlopen(req, timeout=30).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default=CHAIN_URL, help="chain.jsonl URL or local path")
    ap.add_argument("--pub", default=None, help="public key PEM path (default: embedded)")
    ap.add_argument("--anchor", default=ANCHOR_URL,
                    help="GitHub HEAD.json to compare the head against ('none' to skip)")
    args = ap.parse_args()

    pem = open(args.pub, "rb").read() if args.pub else PUBKEY_PEM.encode()
    pub = cast(Ed25519PublicKey, serialization.load_pem_public_key(pem))
    try:
        embedded = cast(Ed25519PublicKey,
                        serialization.load_pem_public_key(PUBKEY_PEM.encode()))
        if args.pub and pub.public_bytes_raw() != embedded.public_bytes_raw():
            print("WARNING: --pub key does NOT match the key published in this repo")
    except Exception:
        pass

    if args.chain.startswith("http"):
        raw = fetch(args.chain)
    else:
        raw = open(args.chain, "rb").read()
    lines = [l for l in raw.split(b"\n") if l.strip()]
    if not lines:
        sys.exit("EMPTY CHAIN — refusing")

    prev = "GENESIS"
    for i, l in enumerate(lines):
        e = json.loads(l)
        sig = e.pop("sig")
        if e.get("prev") != prev:
            sys.exit("CHAIN BREAK at line %d: prev hash mismatch" % (i + 1))
        try:
            pub.verify(base64.b64decode(sig),
                       hashlib.sha256(canonical(e).encode()).digest())
        except Exception:
            sys.exit("BAD SIGNATURE at line %d (seq %s)" % (i + 1, e.get("seq")))
        if e.get("seq") != i + 1:
            sys.exit("SEQ GAP at line %d (seq %s)" % (i + 1, e.get("seq")))
        prev = hashlib.sha256(l).hexdigest()

    print("OK: %d snapshots, every signature valid, chain unbroken from GENESIS." % len(lines))
    last = json.loads(lines[-1])
    p = last["payload"]
    print("head  seq=%s ts=%s engine=%s" % (last["seq"], last["ts"][:16], p.get("engine")))
    if p.get("okx_equity_usdc") is not None:
        print("      equity $%s  uPnL $%s  open=%d" % (
            p["okx_equity_usdc"], p.get("okx_unrealized_pnl_usdc"),
            len(p.get("open_positions") or [])))

    if args.anchor and args.anchor != "none":
        try:
            head_local = {"head_sha256": hashlib.sha256(lines[-1]).hexdigest(),
                          "seq": last["seq"]}
            a = json.loads(fetch(args.anchor))
            if a.get("head_sha256") == head_local["head_sha256"]:
                print("ANCHOR MATCH: GitHub verify/HEAD.json pins this exact chain head"
                      " (seq %s)." % a["seq"])
            elif a.get("seq", 0) >= head_local["seq"]:
                print("ANCHOR AHEAD: GitHub pins seq %s >= local %s — you fetched a"
                      " stale chain file." % (a.get("seq"), head_local["seq"]))
            else:
                sys.exit("ANCHOR MISMATCH: GitHub head (seq %s) does not match this"
                         " chain head (seq %s). The chain you fetched is NOT the"
                         " anchored one." % (a.get("seq"), head_local["seq"]))
        except SystemExit:
            raise
        except Exception as e:
            print("anchor check unavailable: %r" % (e,))


if __name__ == "__main__":
    main()
