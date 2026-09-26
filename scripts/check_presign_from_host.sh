#!/usr/bin/env bash
# Task 2.1 live check (upload-ingest-merge design.md §4; U2.3): a part URL presigned by the API works from the HOST,
# as a browser would use it. The API starts a multipart upload and presigns part 1; the host PUTs to that URL with
# curl and no Content-Type, reads the ETag, and the API completes the upload and finds the object.
# LocalStack checks signatures here (S3_SKIP_SIGNATURE_VALIDATION=0), so two negative PUTs prove the host is signed:
# a tampered signature and the same URL on another host (127.0.0.1) must both be refused.
# Task 2.2: the bucket's CORS rule answers a real preflight from http://localhost:5173 (allowing PUT, exposing ETag),
# refuses one from another origin, and the part PUT's response exposes ETag to the page.
set -euo pipefail
cd "$(dirname "$0")/.."
KEY="ClinSync/incoming/presign-check-$$/part.bin"
TMP=$(mktemp -d)
cleanup() {
  rm -rf "$TMP"
  docker compose exec -T localstack awslocal s3 rm "s3://clinsync-poc/$KEY" >/dev/null 2>&1 || true
}
trap cleanup EXIT

api_py() { docker compose exec -T api python -c "$1"; }
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS  $1: $2"; else echo "FAIL  $1: got $2, want $3"; fail=1; fi; }

read -r UPLOAD_ID URL < <(api_py "from app import storage
key = '$KEY'
upload_id = storage.create_multipart(key, 'application/octet-stream')
print(upload_id, storage.presign_part(key, upload_id, 1))")
echo "presigned URL host: $(echo "$URL" | cut -d/ -f1-3)"
check "presigned host" "$(echo "$URL" | cut -d/ -f3)" "localhost:4566"

head -c 1048576 /dev/urandom > "$TMP/part.bin"                  # 1 MiB: a last part may be under 5 MiB
MD5=$(md5sum "$TMP/part.bin" | cut -d' ' -f1)

TAMPERED="${URL%?}$([ "${URL: -1}" = 0 ] && echo 1 || echo 0)"
check "PUT with a tampered signature" "$(curl -s -o /dev/null -w '%{http_code}' -T "$TMP/part.bin" "$TAMPERED")" 403
OTHER_HOST="${URL/localhost:4566/127.0.0.1:4566}"
check "PUT on another host (host is signed)" \
  "$(curl -s -o /dev/null -w '%{http_code}' -T "$TMP/part.bin" "$OTHER_HOST")" 403

PREFLIGHT=$(curl -s -i -X OPTIONS "$URL" -H "Origin: http://localhost:5173" -H "Access-Control-Request-Method: PUT" \
  | tr -d '\r')
echo "$PREFLIGHT" | grep -iE '^HTTP|^access-control' | sed 's/^/  preflight: /'
check "preflight from localhost:5173" "$(echo "$PREFLIGHT" | head -1 | cut -d' ' -f2)" 200
check "allowed origin" "$(echo "$PREFLIGHT" | grep -i '^access-control-allow-origin:' | cut -d' ' -f2-)" \
  "http://localhost:5173"
check "PUT allowed" "$(echo "$PREFLIGHT" | grep -i '^access-control-allow-methods:' | grep -c PUT)" 1
check "ETag exposed" "$(echo "$PREFLIGHT" | grep -i '^access-control-expose-headers:' | cut -d' ' -f2-)" "ETag"
check "preflight from another origin" "$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS "$URL" \
  -H "Origin: http://evil.example" -H "Access-Control-Request-Method: PUT")" 403

# curl -T sends a PUT with no Content-Type, like the browser's XHR in s3ChunkedUpload.ts
CODE=$(curl -s -D "$TMP/headers" -o /dev/null -w '%{http_code}' -T "$TMP/part.bin" \
  -H "Origin: http://localhost:5173" "$URL")
check "PUT from the host" "$CODE" 200
ETAG=$(grep -i '^etag:' "$TMP/headers" | cut -d' ' -f2- | tr -d '\r')
check "PUT response exposes ETag" \
  "$(grep -i '^access-control-expose-headers:' "$TMP/headers" | cut -d' ' -f2- | tr -d '\r')" "ETag"
echo "ETag header: $ETAG"
check "ETag is the part's MD5" "$ETAG" "\"$MD5\""

RESULT=$(api_py "from app import storage
key = '$KEY'
storage.complete_multipart(key, '$UPLOAD_ID', [{'partNumber': 1, 'etag': '$ETAG'}])
print(storage.exists(key), storage.object_size(key))")
check "object exists after complete, with the part's size" "$RESULT" "True 1048576"

[ $fail -eq 0 ] && echo "PASSED: presigned for localhost:4566, uploaded from the host, completed; bucket CORS exposes ETag"
exit $fail
