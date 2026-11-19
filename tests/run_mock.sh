#!/usr/bin/env bash
# Offline test against tests/mock_site (six venue tabs on one page, like the real site).
#  1. per-person routing: each person only gets lines matching their venues/levels/snooze; paused gets nothing
#  2. same page again: no repeat alerts
#  3. probe: reads every venue tab with levels
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8765
DATA=$(mktemp -d)
python3 -m http.server "$PORT" --directory tests/mock_site >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV' EXIT; sleep 1

cat > "$DATA/users.json" <<'JSON'
[
  {"quiet":{"on":false},"id":"jon","name":"Jon","topic":"t","paused":false,"snooze":[],
   "rules":[{"venue":"LGA","levels":[]},{"venue":"Beacon","levels":["Advanced Intermediate"]}]},
  {"quiet":{"on":false},"id":"sam","name":"Sam","topic":"t","paused":false,"snooze":["12/04"],
   "rules":[{"venue":"BrandeisFri","levels":[]},{"venue":"BrandeisSun","levels":["Advanced"]}]},
  {"quiet":{"on":false},"id":"kai","name":"Kai","topic":"t","paused":true,"snooze":[],
   "rules":[{"venue":"LGA","levels":[]}]}
]
JSON
export NYU_NOTIFY_DRY=1 NYU_LGA_URL="http://localhost:$PORT/index.html" NYU_DATA_DIR="$DATA" NYU_USERS_FILE="$DATA/users.json"

echo "== 0. first run re-baselines silently"; OUT0=$(python3 bot.py); echo "$OUT0"
python3 - "$DATA/state.json" <<'PY'
import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["last_seen"]={}; json.dump(s,open(p,"w"))
PY
echo "== 1. alert run";         OUT1=$(python3 bot.py); echo "$OUT1"
echo "== 2. repeat (no alert)"; OUT2=$(python3 bot.py); echo "$OUT2"
echo "== 3. probe";             OUT3=$(NYU_MODE=probe python3 bot.py); echo "$OUT3"

fail() { echo "FAIL: $1"; exit 1; }
grep -q "NOTIFY" <<<"$OUT0"                                          && fail "re-baseline run should be silent"
J=$(grep "NOTIFY\[jon\]" <<<"$OUT1" || true); S=$(grep "NOTIFY\[sam\]" <<<"$OUT1" || true)
grep -q "LaGuardia · Fri 11/20 7pm Intermediate · Court 2: 1 open" <<<"$J" || fail "jon LGA 1"
grep -q "LaGuardia · Fri 12/04 7pm Intermediate · Court 2: 4 open" <<<"$J" || fail "jon LGA 4"
grep -q "Beacon · Fri 11/20 7pm Advanced Intermediate · Court 2: 2 open" <<<"$J" || fail "jon Beacon AI"
grep -q "Beacon · Fri 11/20 7pm Advanced · Court 1" <<<"$J"              && fail "jon should not get plain Advanced"
grep -q "Brandeis" <<<"$J"                                            && fail "jon should not get Brandeis"
grep -q "Brandeis · Fri 11/20 7pm Intermediate · Court 1: 6+ open" <<<"$S" || fail "sam Brandeis Fri (Yes=6+)"
grep -q "12/04" <<<"$S"                                               && fail "sam snoozed 12/04"
grep -q "Brandeis · Sun 11/22 7pm Advanced: 3 open" <<<"$S"               || fail "sam Brandeis Sun Advanced"
grep -q "LaGuardia" <<<"$S"                                           && fail "sam should not get LGA"
grep -q "NOTIFY\[kai\]" <<<"$OUT1"                                    && fail "paused kai got an alert"
grep -q "no new openings" <<<"$OUT2"                                  || fail "repeat run re-alerted"
grep -q "\[venue Clinics\] 1 sessions; levels=\['Beginner Clinic'\]" <<<"$OUT3" || fail "probe clinics"
grep -q "\[venue W50\] 1 sessions" <<<"$OUT3"                          || fail "probe W50"
echo PASS
