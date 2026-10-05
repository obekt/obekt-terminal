#!/bin/bash
# Obekt Terminal — telemetry bridge (runs on the Mac, pushes to hostinger)
#
# Pushes ONLY the raw telemetry the terminal needs. Explicit whitelist: the
# engine's own source, credentials and anything else never leaves this box.
# Safe to run every minute; rsync only transfers what changed.
#
#   Usage:  ./sync-telemetry.sh            # one push
#           VERBOSE=1 ./sync-telemetry.sh  # show transfer stats
#
# Install as a launchd agent or cron:  * * * * * /path/to/sync-telemetry.sh

set -euo pipefail

SRC="${OKX_SRC:-$HOME/.hermes/workspace/okx}"
DEST="${TRADER_DEST:-root@100.113.195.75:/opt/trader/data/}"
LOCK="/tmp/obekt-telemetry-sync.lock"

# --- the whitelist. Nothing outside this list is ever sent. -----------------
FILES=(
  okx_jev_log.jsonl   # the full decision/trade audit trail (the main feed)
  universe.json       # latest universe scan
  social.json         # fear&greed / trending / funding
  catalyst.json       # news headlines riding in prompts
  risk_state.json     # cooldowns, daily breaker, session state
  positions.json      # engine's own position metadata
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
