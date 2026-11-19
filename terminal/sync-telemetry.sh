#!/bin/bash
# Obekt Terminal — telemetry bridge
#
# Optional convenience script: pushes raw engine telemetry from a machine that
# runs the engine to the machine that serves the terminal deck. Not needed if
# the engine and the deck live on the same host.
#
# Pushes ONLY the raw telemetry the terminal needs. Explicit whitelist: the
# engine's own source, credentials and anything else never leaves this box.
# Safe to run every minute; rsync only transfers what changed.
#
#   Usage:  TRADER_DEST=user@host:/path/to/data ./sync-telemetry.sh   # one push
#           VERBOSE=1 ./sync-telemetry.sh                             # transfer stats
#
# Install as a launchd agent or cron:  * * * * * /path/to/sync-telemetry.sh
#
# TRADER_DEST is REQUIRED and has no default on purpose: this file is public,
# so a hardcoded host would publish infrastructure detail. Export it in your
# shell profile or the cron/launchd environment, e.g.
#   export TRADER_DEST=trader@myhost:/srv/obekt/data/

set -euo pipefail

SRC="${OKX_SRC:-$HOME/.hermes/workspace/okx}"
DEST="${TRADER_DEST:-}"
if [ -z "$DEST" ]; then
  echo "error: TRADER_DEST is not set." >&2
  echo "  export TRADER_DEST=user@host:/path/to/trader/data" >&2
  exit 2
fi
LOCK="/tmp/obekt-telemetry-sync.lock"

# --- the whitelist. Nothing outside this list is ever sent. -----------------
# Stocks era (2026-11-19+): the Alpaca engine REUSES the perp_* filenames and
# event vocabulary, so the live feed is perp_log.jsonl + its state files. The
# retired spot log/universe are included so the deck's frozen history panels
# still render. See engine/alpaca_stocks.py for the authoritative file set.
FILES=(
  perp_log.jsonl        # LIVE stocks book: the full decision/trade audit trail
  perp_positions.json   # live position metadata (engine-managed TP/SL)
  perp_status.json      # engine snapshot (equity, positions, last call)
  perp_risk_state.json  # daily breaker / session state
  news_state.json       # seen/acted headline ids + market-open transition flag
  stocks_news.json      # fresh Alpaca News API headlines (the live signal)
  okx_jev_log.jsonl     # RETIRED spot book log (frozen history panels)
  universe.json         # retired spot universe scan (history)
)

# single-flight: skip if a previous push is still running (portable mkdir lock)
LOCKDIR="$LOCK.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # stale lock (>10 min) -> steal it; otherwise exit quietly
  if [[ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]]; then
    rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

RSYNC_OPTS=(-a --delete --timeout=60)
[[ "${VERBOSE:-0}" == "1" ]] && RSYNC_OPTS+=(--stats)

# build the explicit include list; rsync sends exactly these files, nothing more
INCLUDES=()
for f in "${FILES[@]}"; do
  [[ -e "$SRC/$f" ]] && INCLUDES+=(--include="/$f")
done
INCLUDES+=(--exclude='*')

/usr/bin/rsync "${RSYNC_OPTS[@]}" "${INCLUDES[@]}" "$SRC/" "$DEST" \
  >>"${SYNC_LOG:-/tmp/obekt-telemetry-sync.log}" 2>&1

# optional: also mirror the dossiers the Mac already built, so the server
# doesn't re-spend LLM calls on trades it has already debriefed.
if [[ "${SYNC_DOSSIERS:-0}" == "1" && -d "$SRC/terminal-site/dossiers" ]]; then
  /usr/bin/rsync -a --timeout=60 \
    "$SRC/terminal-site/dossiers/" \
    "${DEST%/data/}/state/dossiers/" \
    >>"${SYNC_LOG:-/tmp/obekt-telemetry-sync.log}" 2>&1 || true
fi
