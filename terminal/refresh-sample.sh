#!/usr/bin/env bash
# OBEKT TERMINAL — refresh sample_data/ from a live build.
#
# WHY THIS EXISTS
#   sample_data/ is the frozen telemetry that lets anyone run the full deck with
#   ZERO credentials (see README "Run the demo"). Nothing else in the repo
#   regenerates it, so it silently rots: after a schema change it keeps rendering
#   an OLD layout while claiming to be real data. It did exactly that — it was
#   missing `engine.header`, so the demo fell back to the pre-compaction composite
#   string and showed "X-PERPS" twice plus the retired engine's version, i.e. the
#   very redundancies the header was tightened to remove.
#
#   A stale demo is worse than no demo: it is the first thing a visitor runs, and
#   it misrepresents the current UI.
#
# ORDERING MATTERS
#   Fetch into a staging dir, scan and schema-check THERE, and only then swap into
#   sample_data/. The first version of this script wrote sample_data/ first and
#   validated after, so a bad source corrupted the repo — the check still reported
#   correctly, but the damage was already on disk and `git checkout` was the only
#   recovery. Never mutate the thing you are validating.
#
# Usage:
#   ./refresh-sample.sh /path/to/live/state      # local dir with data.js/json, og.png, dossiers/
#   ./refresh-sample.sh user@host:/opt/x/state   # pull over scp
#
# Exit codes: 0 ok · 1 schema failure · 2 secret flagged · 3 usage/fetch error
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
DEST="sample_data"

SRC="${1:-}"
if [ -z "$SRC" ]; then
  echo "usage: $0 <live-state-dir | user@host:path>" >&2
  echo "       (the dir holding data.js, data.json, og.png, dossiers/)" >&2
  exit 3
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/tt-sample.XXXXXX")"
trap 'rm -rf "$STAGE"; rm -rf "$DEST.new"' EXIT

fetch() {  # fetch <relpath> <local-dest>
  if [[ "$SRC" == *:* && "$SRC" != /* ]]; then
    scp -q "$SRC/$1" "$2"
  else
    cp -R "$SRC/$1" "$2"
  fi
}

echo "[1/6] staging from $SRC"
mkdir -p "$STAGE/dossiers"
fetch data.js    "$STAGE/data.js"
fetch data.json  "$STAGE/data.json"
fetch og.png     "$STAGE/og.png"
fetch dossiers/. "$STAGE/dossiers/"
ndoss=$(find "$STAGE/dossiers" -name '*.json' | wc -l | tr -d ' ')
echo "       staged data.js, data.json, og.png, $ndoss dossiers ($DEST/ untouched so far)"

echo "[2/6] SECRET SCAN (public repo — this is the gate, not a nicety)"
# Generic credential SHAPES only. Deliberately no literal personal identifiers
# (hostnames, tailnet names, provider names) in this tracked file: a scanner that
# hardcodes the secrets it hunts for publishes them — this script would have been
# the first thing in the repo to disclose the operator's host and tailnet name.
# Site-specific identifiers live in an UNTRACKED .sample-secrets (one ERE per
# line, '#' comments allowed), so the check stays sharp without leaking.
# A machine-generated dump is the easiest place to leak a host or key, because
# nobody reads it line by line. Report and let a human decide — never auto-strip,
# which can mangle real data.
PATTERNS='BEGIN [A-Z ]*PRIVATE KEY|gh[opsru]_[A-Za-z0-9]{20,}|xox[baprs]-|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN|"api_key"[[:space:]]*:[[:space:]]*"[^"]{12,}"|"secret"[[:space:]]*:[[:space:]]*"[^"]{8,}"|"passphrase"[[:space:]]*:[[:space:]]*"[^"]{8,}"|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|/root/\.config|/Users/[a-z]+/\.config|LITELLM_MASTER_KEY'
EXTRA=".sample-secrets"
if [ -f "$EXTRA" ]; then
  # shellcheck disable=SC2013
  extra_re=$(grep -vE '^[[:space:]]*(#|$)' "$EXTRA" | paste -sd'|' -)
  if [ -n "$extra_re" ]; then PATTERNS="$PATTERNS|$extra_re"; echo "       + site-specific patterns from $EXTRA (untracked)"; fi
else
  echo "       (no $EXTRA — generic shapes only; add one for your own host/tailnet names)"
fi
flagged=0
while IFS= read -r f; do
  if grep -qE "$PATTERNS" "$f" 2>/dev/null; then
    echo "  FLAGGED: ${f#"$STAGE"/}"
    grep -oE "$PATTERNS" "$f" 2>/dev/null | sort -u | head -5 | sed 's/^/      /'
    flagged=$((flagged+1))
  fi
done < <(find "$STAGE" -type f)
scanned=$(find "$STAGE" -type f | wc -l | tr -d ' ')
if [ "$flagged" -gt 0 ]; then
  echo "  -> $flagged of $scanned file(s) flagged. ABORTING — $DEST/ was NOT modified."
  exit 2
fi
echo "  -> clean ($scanned files)"

echo "[3/6] SCHEMA CHECK (the failure mode this script exists to prevent)"
if ! python3 - "$STAGE/data.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get("engine") or {}
# Every key the deck's renderers require. If generate.py adds or renames one, a
# frozen sample missing it renders the OLD fallback silently — which is exactly how
# the header regression shipped unnoticed.
required = {
    "engine.header":            e.get("header"),
    "engine.version":           e.get("version"),
    "engine.venue":             e.get("venue"),
    "engine.spot_book.status":  (e.get("spot_book") or {}).get("status"),
    "engine.spot_book.version": (e.get("spot_book") or {}).get("version"),
    "live_book":                d.get("live_book"),
    "stats":                    d.get("stats"),
    "params":                   d.get("params"),
    "retired_spot":             d.get("retired_spot"),
    "perp":                     d.get("perp"),
    "desk_note":                d.get("desk_note"),
}
missing = [k for k, v in required.items() if v in (None, "", {}, [])]
if missing:
    print("  MISSING/FALSEY: " + ", ".join(missing))
    print("  -> sample would render fallbacks, not the current UI. Do not ship.")
    sys.exit(1)
h = e["header"]
print("  engine.header = %r (%d chars)" % (h, len(h)))
if h.upper().count("X-PERP") > 1:
    print("  DUPLICATE 'X-PERP' TOKEN IN HEADER — the compaction regressed"); sys.exit(1)
if "retired" in h.lower() or "SPOT v" in h:
    print("  HEADER NAMES THE RETIRED ENGINE — it must name only the live one"); sys.exit(1)
rs = (d.get("retired_spot") or {}).get("stats") or {}
ls = d.get("stats") or {}
if rs.get("trades_all") == ls.get("trades_all") and ls.get("trades_all"):
    print("  retired_spot stats identical to live stats — misattributed"); sys.exit(1)
print("  live book %s: %s trades | retired spot: %s trades  (distinct)" % (
    d.get("live_book"), ls.get("trades_all"), rs.get("trades_all")))
print("  -> schema OK")
PY
then
  echo "  -> ABORTING — $DEST/ was NOT modified."
  exit 1
fi

echo "[4/6] data.js and data.json agree?"
if ! python3 - "$STAGE/data.js" "$STAGE/data.json" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
js = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
dj = json.load(open(sys.argv[2]))
a = (js.get("engine") or {}).get("header")
b = (dj.get("engine") or {}).get("header")
if a != b:
    print("  MISMATCH: data.js=%r data.json=%r" % (a, b)); sys.exit(1)
if js.get("generated") != dj.get("generated"):
    print("  MISMATCH: generated data.js=%r data.json=%r" % (
        js.get("generated"), dj.get("generated"))); sys.exit(1)
print("  both engine.header = %r" % a)
PY
then
  echo "  -> ABORTING — $DEST/ was NOT modified."
  exit 1
fi

echo "[5/6] all checks passed — swapping staging into $DEST"
rm -rf "$DEST.new" && mkdir -p "$DEST.new"
cp -R "$STAGE"/. "$DEST.new"/
rm -rf "$DEST" && mv "$DEST.new" "$DEST"
trap 'rm -rf "$STAGE"' EXIT
echo "       installed $(find "$DEST" -type f | wc -l | tr -d ' ') files, $ndoss dossiers"

echo "[6/6] done. Review, then commit:"
echo "  git -C .. diff --stat -- terminal/$DEST"
echo "  git -C .. status --porcelain"
