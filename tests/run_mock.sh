#!/usr/bin/env bash
# Offline test against tests/mock_site.
#  1. alert run: LGA rows + Beacon "Advanced Intermediate" only (plain "Advanced" must be ignored)
#  2. same page again: no repeat alerts
#  3. buy mode (dry run) still reaches Pay
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8765
DATA=$(mktemp -d)
python3 -m http.server "$PORT" --directory tests/mock_site >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV' EXIT; sleep 1

export NYU_LGA_URL="http://localhost:$PORT/index.html" NYU_BEACON_URL="http://localhost:$PORT/beacon.html"
export NYU_DATA_DIR="$DATA" NYU_TARGETS=any

echo "== 1. alert run";        OUT1=$(python3 bot.py); echo "$OUT1"
echo "== 2. repeat (no alert)"; OUT2=$(python3 bot.py); echo "$OUT2"
echo "== 3. buy mode dry run"
NYU_MODE=buy DRY_RUN=1 NYU_TARGETS=11/20 NYU_FIRST=T NYU_LAST=U NYU_EMAIL=t@example.com \
  NYU_CARD_NUMBER=4111111111111111 NYU_CARD_EXP=04/27 NYU_CARD_CVV=123 python3 bot.py
echo "== sightings"; cat "$DATA/sightings.csv"

fail() { echo "FAIL: $1"; exit 1; }
grep -q "LGA 11/20 Court 2: 1 open (need 4)" <<<"$OUT1" || fail "LGA partial alert"
grep -q "LGA 12/04 Court 2: 4 open ✅" <<<"$OUT1"        || fail "LGA enough alert"
grep -q "Beacon 11/20 Court 2: 2 open" <<<"$OUT1"        || fail "Beacon AI alert"
grep -q "Court 1: 5 open" <<<"$OUT1"                     && fail "plain Advanced should be ignored"
[[ $(grep -o "NOTIFY:.*" <<<"$OUT1" | head -1) == *"LGA"*"Beacon"* ]] || fail "LGA should be listed first"
grep -q "no new openings" <<<"$OUT2"                     || fail "repeat run re-alerted"
grep -q "dry_run_ok" "$DATA/sightings.csv"               || fail "buy dry run"
echo PASS
