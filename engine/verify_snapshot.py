#!/usr/bin/env python3
"""verify_snapshot.py — cryptographic proof-of-trades for trader.obekt.com.

Runs on its OWN systemd timer (obekt-verify.timer, daily 00:03 UTC).
NEVER imported by the trading engine; NEVER places orders. It reads:
  - Alpaca account equity/positions via ~/.config/alpaca/credentials.json
    (GET endpoints only),
  - the engine's event log (counts only),
  - ENGINE_VERSION from alpaca_stocks.py (single source for the version label).
VENUE CHANGE 2026-11-19: OKX X-Perps -> Alpaca US stocks. The chain continues
unbroken (same key, same seq); pre-cutover entries carry okx_* payload keys,
later entries carry alpaca_* keys.

It appends a signed, hash-chained snapshot to chain.jsonl. Tampering with any
historical line breaks every later link, and the current head is anchored to
the public GitHub repo (third-party timestamping). Verify independently with
terminal/verify.py — the trust anchor is the public key EMBEDDED there and
published in this repo (verify/pub.pem).

Chain format (one JSON object per line, no trailing spaces):
  entry   = {"seq": N, "ts": iso, "prev": "<sha256 of previous raw line>", "payload": {...}}
  signed  = sha256( canonical(entry) )        canonical = json.dumps(sort_keys, separators)
  line    = json.dumps(entry + {"sig": base64(ed25519(signed))})
First line: prev = "GENESIS".
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# Directories default to the repo layout (engine/, state/, verify/ siblings of
# the base dir). Override any of them via env for a different deployment. The
# script lives in engine/, so HERE is the engine dir; BASE defaults to its parent.
HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.expanduser(os.environ.get("OBKT_BASE_DIR", os.path.join(HERE, os.pardir)))
ENGINE_DIR = os.path.expanduser(os.environ.get("OBKT_ENGINE_DIR", HERE))
STATE_DIR = os.path.expanduser(os.environ.get("OBKT_STATE_DIR", os.path.join(BASE_DIR, "state")))
VERIFY_DIR = os.path.expanduser(os.environ.get("OBKT_VERIFY_DIR", os.path.join(BASE_DIR, "verify")))
CHAIN_NAME = "chain.jsonl"
HEAD_NAME = "head.json"
LOG = os.path.join(VERIFY_DIR, "verify.log")
CRED_PATH = os.path.expanduser("~/.config/alpaca/credentials.json")
ANCHOR_REPO = "git@github.com:obekt/obekt-terminal"
ANCHOR_KEY = os.path.join(VERIFY_DIR, "anchor_deploy")
ANCHOR_DIR = os.path.join(VERIFY_DIR, "anchor")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), msg)
    os.makedirs(VERIFY_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)


def alpaca_get(path):
    """Read-only GET against Alpaca v2. Reuses the engine's credential file."""
    with open(CRED_PATH) as f:
        c = json.load(f)
    req = urllib.request.Request(
        "https://api.alpaca.markets" + path,
        headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"],
                 "User-Agent": "obekt-verify/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def engine_version():
    try:
        src = open(os.path.join(ENGINE_DIR, "alpaca_stocks.py")).read()
        m = re.search(r'^ENGINE_VERSION\s*=\s*["\']([^"\']+)["\']', src, re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def log_counts():
    opens = closes = 0
    try:
        with open(os.path.join(ENGINE_DIR, "perp_log.jsonl")) as f:
            for l in f:
                if '"dry": false' in l or '"dry":false' in l:
                    if '"perp_open"' in l:
                        opens += 1
                    elif '"perp_close"' in l:
                        closes += 1
    except FileNotFoundError:
        pass
    return opens, closes


def build_payload():
    acct = alpaca_get("/v2/account")
    pos = alpaca_get("/v2/positions")
    o, c = log_counts()
    return {
        "engine": engine_version(),
        "venue": "alpaca_us_equities",
        "alpaca_equity_usd": float(acct.get("equity") or 0),
        "alpaca_cash_usd": float(acct.get("cash") or 0),
        "alpaca_buying_power": float(acct.get("buying_power") or 0),
        "open_positions": [
            {"inst": p.get("symbol"), "side": p.get("side"),
             "pos": float(p.get("qty") or 0), "avg_px": float(p.get("avg_entry_price") or 0),
             "upl_usd": float(p.get("unrealized_pl") or 0)}
            for p in pos if p.get("asset_class") == "us_equity" and float(p.get("qty") or 0) != 0
        ],
        "engine_log_opens": o,
        "engine_log_closes": c,
        "method": "alpaca_get_account+positions; log event counts; no LLM",
    }


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def load_priv():
    key = serialization.load_pem_private_key(
        open(os.path.join(VERIFY_DIR, "priv.pem"), "rb").read(), None
    )
    assert isinstance(key, Ed25519PrivateKey)
    return key


def chain_path(directory):
    return os.path.join(directory, CHAIN_NAME)


def append_entry():
    path = chain_path(VERIFY_DIR)
    prev = "GENESIS"
    seq = 0
    if os.path.exists(path):
        with open(path, "rb") as f:
            lines = [l for l in f.read().split(b"\n") if l.strip()]
        if lines:
            prev = hashlib.sha256(lines[-1]).hexdigest()
            seq = json.loads(lines[-1])["seq"]
    entry = {"seq": seq + 1, "ts": datetime.now(timezone.utc).isoformat(),
             "prev": prev, "payload": build_payload()}
    sig = base64.b64encode(load_priv().sign(hashlib.sha256(canonical(entry).encode()).digest())).decode()
    line = json.dumps({**entry, "sig": sig}, sort_keys=True)
    # canonical state dir file first (served), then private copy
    for p in (chain_path(STATE_DIR), path):
        with open(p, "a") as f:
            f.write(line + "\n")
    head = {"seq": entry["seq"], "ts": entry["ts"],
            "head_sha256": hashlib.sha256(line.encode()).hexdigest(),
            "sig": sig,
            "note": "anchor of https://trader.obekt.com/chain.jsonl — verify.py checks the whole chain"}
    for p in (os.path.join(STATE_DIR, HEAD_NAME), os.path.join(VERIFY_DIR, HEAD_NAME)):
        with open(p, "w") as f:
            json.dump(head, f, indent=1, sort_keys=True)
    return head


def anchor_to_github(head):
    """Push head.json into the public repo (third-party timestamping). Best-effort."""
    if not os.path.exists(ANCHOR_KEY):
        log("anchor skipped: no deploy key")
        return
    env = dict(os.environ, GIT_SSH_COMMAND=(
        "ssh -i %s -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes" % ANCHOR_KEY))
    def git(*a):
        subprocess.run(("git",) + a, env=env, cwd=ANCHOR_DIR if os.path.isdir(ANCHOR_DIR) else None,
                       check=True, capture_output=True, timeout=120)
    try:
        if not os.path.isdir(os.path.join(ANCHOR_DIR, ".git")):
            os.makedirs(VERIFY_DIR, exist_ok=True)
            git("clone", "--depth", "5", ANCHOR_REPO, ANCHOR_DIR)
        git("pull", "--rebase", "--autostash", "origin", "HEAD")
        dst = os.path.join(ANCHOR_DIR, "verify", "HEAD.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            json.dump(head, f, indent=1, sort_keys=True)
        r = subprocess.run(("git", "status", "--porcelain", "verify/HEAD.json"),
                           cwd=ANCHOR_DIR, capture_output=True, env=env, timeout=60)
        if r.stdout.strip():
            git("add", "verify/HEAD.json")
            git("-c", "user.name=verify-snapshot", "-c",
                "user.email=verify@obekt.com", "commit", "-m",
                "anchor: chain head seq=%s" % head["seq"])
            git("push", "origin", "HEAD:refs/heads/main")
            log("anchored seq=%s to github" % head["seq"])
        else:
            log("anchor: head unchanged (seq=%s)" % head["seq"])
    except Exception as e:
        log("anchor FAILED: %r" % (e,))


def main():
    # serve-copy must exist even before first run
    os.makedirs(STATE_DIR, exist_ok=True)
    head = append_entry()
    log("snapshot seq=%s written" % head["seq"])
    anchor_to_github(head)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL %r" % (e,))
        sys.exit(1)
