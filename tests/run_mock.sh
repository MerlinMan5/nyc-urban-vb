#!/usr/bin/env bash
# Offline end-to-end test against tests/mock_site: dry run, real "buy", then a re-run that must not buy again.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8765
DATA=$(mktemp -d)
python3 -m http.server "$PORT" --directory tests/mock_site >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV' EXIT; sleep 1

export NYU_URL="http://localhost:$PORT/index.html" NYU_TARGETS="11/20" NYU_DATA_DIR="$DATA"
export NYU_FIRST=Test NYU_LAST=User NYU_EMAIL=test@example.com
export NYU_CARD_NUMBER=4111111111111111 NYU_CARD_EXP=04/27 NYU_CARD_CVV=123 NYU_CARD_ZIP=10024

echo "== 1. dry run";       DRY_RUN=1 python3 bot.py
echo "== 2. real run";      DRY_RUN=0 python3 bot.py
echo "== 3. re-run (must skip)"; DRY_RUN=0 python3 bot.py
echo "== state";     cat "$DATA/state.json"
echo "== sightings"; cat "$DATA/sightings.csv"
grep -q '"status": "purchased"' "$DATA/state.json" && grep -c purchased "$DATA/sightings.csv" | grep -qx 1 \
  && echo "PASS" || { echo "FAIL"; exit 1; }
