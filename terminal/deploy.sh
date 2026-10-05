#!/usr/bin/env bash
# OBEKT TERMINAL — public deploy helper.
#
# The site is 100% static. This script builds fresh data + an absolute-URL
# index, then syncs everything to your web root (or a remote host over ssh).
#
# Usage:
#   ./deploy.sh /var/www/jev                 # local web root
#   PUBLIC_BASE=https://terminal.yourdomain.com ./deploy.sh user@host:/var/www/terminal   # rsync over ssh
#
# Set PUBLIC_BASE so social crawlers resolve og.png to an absolute URL.
# Cron this every 2 min on the box that has the trading log:
#   */2 * * * * cd ~/.hermes/workspace/okx/terminal-site && ./deploy.sh /var/www/jev >> deploy.log 2>&1
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PUBLIC_BASE="${PUBLIC_BASE:-}"
DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "usage: PUBLIC_BASE=https://your.domain $0 <dest-dir-or-user@host:path>" >&2
  exit 1
fi

echo "[1/4] building data.json + data.js + og.png from the live engine…"
python3 generate.py --og

echo "[2/4] baking absolute OG urls (PUBLIC_BASE=${PUBLIC_BASE:-none})…"
if [ -n "$PUBLIC_BASE" ]; then
  PUBLIC_BASE="$PUBLIC_BASE" python3 serve.py --build-public
  cp index.public.html .index.deploy.html
else
  cp index.html .index.deploy.html
fi

echo "[3/4] assembling payload…"
# stage into a temp dir so the deploy is atomic-ish and excludes dev cruft
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp .index.deploy.html "$STAGE/index.html"
cp style.css app.js data.js og.png "$STAGE/"
mkdir -p "$STAGE/dossiers"
cp dossiers/*.json "$STAGE/dossiers/" 2>/dev/null || true

echo "[4/4] syncing to $DEST …"
if [[ "$DEST" == *:* ]]; then
  rsync -az --delete "$STAGE/" "$DEST/"
else
  mkdir -p "$DEST"
  rsync -a --delete "$STAGE/" "$DEST/"
fi

echo "deployed $(ls "$STAGE/dossiers" | wc -l | tr -d ' ') dossiers + data.js ($(du -h "$STAGE/data.js" | cut -f1)) to $DEST"
