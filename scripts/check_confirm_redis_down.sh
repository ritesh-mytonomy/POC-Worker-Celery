#!/usr/bin/env bash
# R2.4 / R2.6: with Redis actually stopped, confirm returns 200 `uploaded`, enqueued=false, within 2 s,
# and the API logs enqueue_failed. Restarts Redis and removes its demo batch on exit. Reused by S8.
set -euo pipefail
cd "$(dirname "$0")/.."
API=http://127.0.0.1:8000
BATCH=""

cleanup() {
  docker compose start redis >/dev/null
  docker compose up -d --wait redis >/dev/null
  if [ -n "$BATCH" ]; then
    docker compose exec -T postgres psql -U "${POSTGRES_USER:-clinsync}" -d "${POSTGRES_DB:-clinsync}" -qtAc \
      "DELETE FROM upload_batch WHERE batch_id = '$BATCH'" >/dev/null
  fi
  echo "cleanup: redis $(docker compose ps --format '{{.Status}}' redis), demo batch removed"
}
trap cleanup EXIT

SEED=$(curl -sf -X POST "$API/poc/seed" -H 'Content-Type: application/json' \
  -d '{"files":[{"file_name":"valid_a.docx","s3_key":"ClinSync/incoming/redis-down/valid_a.docx"}]}')
BATCH=$(echo "$SEED" | jq -r .batch_id)
FILE=$(echo "$SEED" | jq -r '.files[0].file_id')

docker compose stop redis >/dev/null
echo "redis: $(docker compose ps -a --format '{{.Status}}' redis)"
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)

OUT=$(curl -s -o /dev/stderr -w '%{http_code} %{time_total}' -X POST "$API/api/v1/uploads/$FILE/confirm" 2>body.tmp) || true
BODY=$(cat body.tmp); rm -f body.tmp
CODE=${OUT% *}; SECS=${OUT#* }
echo "confirm -> HTTP $CODE in ${SECS}s: $BODY"

LOGGED=$(docker compose logs --no-log-prefix --since "$SINCE" api | jq -R -c --arg f "$FILE" \
  'fromjson? | select(.event == "enqueue_failed" and .file_id == $f) | {event, file_id, error}' | head -1)
echo "api log: $LOGGED"

fail=0
[ "$CODE" = "200" ] || { echo "FAIL: status $CODE"; fail=1; }
[ "$(echo "$BODY" | jq -r .status)" = "uploaded" ] || { echo "FAIL: status not uploaded"; fail=1; }
[ "$(echo "$BODY" | jq -r .enqueued)" = "false" ] || { echo "FAIL: enqueued not false"; fail=1; }
awk -v s="$SECS" 'BEGIN { exit !(s < 2.0) }' || { echo "FAIL: took ${SECS}s (limit 2 s)"; fail=1; }
[ -n "$LOGGED" ] || { echo "FAIL: no enqueue_failed log line"; fail=1; }
docker compose start redis >/dev/null && docker compose up -d --wait redis >/dev/null
UNDELIVERED=$(docker compose exec -T redis redis-cli LLEN ae.undeliver | tr -d '\r')
echo "LLEN ae.undeliver: $UNDELIVERED"
[ "$UNDELIVERED" = "0" ] || { echo "FAIL: ae.undeliver has $UNDELIVERED messages"; fail=1; }
[ $fail -eq 0 ] && echo "PASS: confirm with Redis stopped"
exit $fail
