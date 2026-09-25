#!/usr/bin/env bash
# Every scenario from a clean stack (task 13.1): S1–S8, S5b, the check scripts and the NFR timings (13.2).
# Each asserts LLEN ae.undeliver == 0 itself, and NFR-5/NFR-6 on its batch before cleanup. Prints a pass/fail table; exits non-zero on any failure.
# Output of each script: run_logs/<name>.log. S5 runs without --wait-visibility.
set -uo pipefail
cd "$(dirname "$0")/.."

# Block system sleep and lid-close suspend for the whole run: a suspended laptop stalls every scenario for hours
# (--what=sleep alone does not cover closing the lid).
if [ -z "${RUN_ALL_INHIBITED:-}" ]; then
  export RUN_ALL_INHIBITED=1
  exec systemd-inhibit --what=sleep:handle-lid-switch --who="clinsync run_all" --why="scenario suite running" --mode=block "$0" "$@" \
    || { echo "systemd-inhibit failed: disable automatic suspend, then run again with RUN_ALL_INHIBITED=1"; exit 3; }
fi
LOGS=run_logs
mkdir -p "$LOGS"
mono() { python3 -c 'import time; print(int(time.monotonic()))'; }   # durations immune to clock jumps

echo "== clean stack: docker compose down -v && up -d --build --wait"
docker compose down -v >/dev/null 2>&1
docker compose up -d --build --wait >/dev/null 2>&1 || { echo "stack failed to start"; exit 2; }
sleep 5                                                   # workers finish booting, first sweeps run

echo "== regenerate fixtures"
rm -rf fixtures/out
python3 -c "import sys; sys.path.insert(0, 'scripts'); import lib; lib.ensure_fixtures()" || { echo "fixtures failed"; exit 2; }

RUNS=(
  "S1|python3 scripts/scenario_s1.py"
  "S2|python3 scripts/scenario_s2.py"
  "S3|python3 scripts/scenario_s3.py"
  "S4|python3 scripts/scenario_s4.py"
  "S5|python3 scripts/scenario_s5.py"
  "S5b|python3 scripts/scenario_s5b.py"
  "S6|python3 scripts/scenario_s6.py"
  "S7|python3 scripts/scenario_s7.py"
  "S8|python3 scripts/scenario_s8.py"
  "check_confirm_redis_down|./scripts/check_confirm_redis_down.sh"
  "check_archive_rejection|python3 scripts/check_archive_rejection.py"
  "check_retry|python3 scripts/check_retry.py"
  "check_nfr|python3 scripts/check_nfr.py"
)

declare -a RESULTS
failed=0
for run in "${RUNS[@]}"; do
  name=${run%%|*}; cmd=${run#*|}
  printf "== %-26s " "$name"
  start=$(mono)
  if $cmd >"$LOGS/$name.log" 2>&1; then verdict=PASS; else verdict=FAIL; failed=$((failed + 1)); fi
  secs=$(( $(mono) - start ))
  echo "$verdict (${secs}s)"
  RESULTS+=("$name|$verdict|$secs")
done

echo
printf "%-26s %-6s %8s\n" "scenario" "result" "seconds"
printf "%-26s %-6s %8s\n" "--------------------------" "------" "--------"
for r in "${RESULTS[@]}"; do
  IFS='|' read -r n v t <<<"$r"
  printf "%-26s %-6s %8s\n" "$n" "$v" "$t"
done
echo
echo "$(( ${#RESULTS[@]} - failed )) passed, $failed failed  (logs: $LOGS/)"
[ "$failed" -eq 0 ]
