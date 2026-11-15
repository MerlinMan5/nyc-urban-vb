#!/usr/bin/env bash
# Offline test against tests/mock_site (venue tabs on one page, like the real site).
#  1. alert run: LGA rows + Beacon "Advanced Intermediate" only (plain "Advanced" ignored)
#  2. same page again: no repeat alerts
#  3. probe mode on Brandeis ("Yes" = 6+): fills form, Checkout, finds card fields + Pay, never pays
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8765
DATA=$(mktemp -d)
python3 -m http.server "$PORT" --directory tests/mock_site >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV' EXIT; sleep 1

export NYU_LGA_URL="http://localhost:$PORT/index.html" NYU_DATA_DIR="$DATA" NYU_TARGETS=any

echo "== 1. alert run";         OUT1=$(python3 bot.py); echo "$OUT1"
echo "== 2. repeat (no alert)"; OUT2=$(python3 bot.py); echo "$OUT2"
echo "== 3. probe Brandeis";    OUT3=$(NYU_MODE=probe NYU_PROBE_VENUE=Brandeis python3 bot.py); echo "$OUT3"
echo "== sightings"; cat "$DATA/sightings.csv"

fail() { echo "FAIL: $1"; exit 1; }
grep -q "LGA 11/20 Court 2: 1 open" <<<"$OUT1"         || fail "LGA 1-spot alert"
grep -q "LGA 12/04 Court 2: 4 open" <<<"$OUT1"         || fail "LGA 4-spot alert"
grep -q "need\|enough" <<<"$OUT1"                     && fail "group-size wording should be gone"
grep -q "Beacon 11/20 Court 2: 2 open" <<<"$OUT1"        || fail "Beacon AI alert"
grep -q "Court 1: 5 open" <<<"$OUT1"                     && fail "plain Advanced should be ignored"
[[ $(grep -o "NOTIFY:.*" <<<"$OUT1" | head -1) == *"LGA"*"Beacon"* ]] || fail "LGA should be listed first"
grep -q "no new openings" <<<"$OUT2"                     || fail "repeat run re-alerted"
grep -q "open:6+" <<<"$OUT3"                             || fail "Yes should parse as 6+"
grep -q "clicked Checkout" <<<"$OUT3"                    || fail "probe checkout"
grep -q "card fields found" <<<"$OUT3"                   || fail "probe card fields"
grep -q "pay button: FOUND .*not clicked" <<<"$OUT3"   || fail "probe pay button"
echo PASS
