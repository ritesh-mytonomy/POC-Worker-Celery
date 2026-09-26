"""Capture real request/response fixtures from the Upload POC's API (task 0.2b).

Run against Anugrah's server UNMODIFIED (File Upload source code, commit 3d2bed9), pointed at LocalStack, with a
Redis reachable so a ZIP's `complete` can enqueue (no Celery worker needed). Each request is sent exactly as the
Client sends it: `utils/s3ChunkedUpload.ts` and `utils/uploadsApi.ts` — JSON bodies with `Content-Type:
application/json`, part PUTs with NO Content-Type. How it was run is in docs/upload-poc-review.md §3.

Usage: python3 tests/fixtures/upload_api/_capture.py [--api http://127.0.0.1:8001]
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = sys.argv[sys.argv.index("--api") + 1] if "--api" in sys.argv else "http://127.0.0.1:8001"
ORIGIN = "http://localhost:5173"
OUT = Path(__file__).parent
REPO = OUT.parents[2]
SOURCE = "File Upload POC @ 3d2bed9, unmodified; S3 = LocalStack (http://localhost:4566)"
KEPT_HEADERS = ("content-type", "etag", "access-control-allow-origin", "access-control-expose-headers")


def pdf_bytes(size: int, seed: str) -> bytes:
    """Deterministic bytes that start like a PDF (the server checks only the %PDF header)."""
    out = bytearray(b"%PDF-1.4\n")
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block * 64
    return bytes(out[:size])


def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """One HTTP call; returns status, the headers worth recording, and the raw body."""
    req = urllib.request.Request(url, data=body, method=method, headers={"Origin": ORIGIN, **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, hdrs, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, exc.headers, exc.read()
    kept = {k.lower(): v for k, v in hdrs.items() if k.lower() in KEPT_HEADERS}
    return status, kept, raw


def api(name: str, method: str, path: str, body: dict | None = None, note: str | None = None) -> tuple[int, object]:
    """Call the Upload API like the Client does and save the exchange as <name>.json."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    data = json.dumps(body).encode() if body is not None else None
    status, resp_headers, raw = send(method, API + path, data, headers)
    try:
        resp_body: object = json.loads(raw) if raw else None
    except ValueError:
        resp_body = raw.decode(errors="replace")
    fixture = {
        "endpoint": f"{method} {path.split('?')[0]}",
        "captured_from": SOURCE,
        **({"note": note} if note else {}),
        "request": {"method": method, "path": path, "headers": headers, "body": body},
        "response": {"status": status, "headers": resp_headers, "body": resp_body},
    }
    (OUT / f"{name}.json").write_text(json.dumps(fixture, indent=2) + "\n")
    print(f"{status}  {name}")
    return status, resp_body


def put_part(url: str, chunk: bytes, name: str | None = None) -> str:
    """PUT one part straight to S3 with no Content-Type, as putChunk() does; return the ETag."""
    status, resp_headers, _ = send("PUT", url, chunk, {})
    assert status == 200 and "etag" in resp_headers, (status, resp_headers)
    if name:
        fixture = {
            "endpoint": "PUT <presigned S3 URL>",
            "captured_from": SOURCE,
            "note": "Sent by the browser straight to S3, not to the API. No Content-Type header (it is not in the "
                    "signature). The Client reads the ETag response header, so the bucket CORS must expose it.",
            "request": {"method": "PUT", "url": url, "headers": {}, "body": f"<{len(chunk)} bytes>"},
            "response": {"status": status, "headers": resp_headers, "body": ""},
        }
        (OUT / f"{name}.json").write_text(json.dumps(fixture, indent=2) + "\n")
        print(f"{status}  {name}")
    return resp_headers["etag"]


def upload(name: str, data: bytes, content_type: str, prefix: str | None = None) -> dict:
    """initiate → presign → PUT every part → (list parts) — the Client's start(); returns what complete needs."""
    save = (lambda suffix: f"{prefix}{suffix}") if prefix is not None else (lambda suffix: None)
    init_body = {"filename": name, "fileSize": len(data), "contentType": content_type}
    status, init = api(save("initiate") or "_tmp", "POST", "/api/uploads/initiate", init_body)
    assert status == 200, init
    part_numbers = list(range(1, init["totalParts"] + 1))
    presign_body = {"key": init["key"], "uploadId": init["uploadId"], "partNumbers": part_numbers}
    _, presigned = api(save("parts_presign") or "_tmp", "POST", "/api/uploads/parts/presign", presign_body)
    parts = []
    for n in part_numbers:
        chunk = data[(n - 1) * init["partSize"]: n * init["partSize"]]
        parts.append({"partNumber": n, "etag": put_part(presigned["urls"][str(n)], chunk,
                                                         save("s3_put_part") if n == 1 else None)})
    if prefix is not None:
        api("list_parts", "GET", f"/api/uploads/{init['uploadId']}/parts?key={urllib.request.quote(init['key'])}",
            note="Defined on the server; the Client never calls it (its retry() resumes from in-memory ETags).")
    return {"id": init["id"], "fileId": init["fileId"], "key": init["key"], "uploadId": init["uploadId"],
            "filename": name, "fileSize": len(data), "contentType": content_type, "parts": parts}


def complete_body(u: dict) -> dict:
    """The exact body runToCompletion() sends."""
    return {k: u[k] for k in ("id", "fileId", "key", "uploadId", "filename", "fileSize", "contentType", "parts")}


def main() -> None:
    """Drive every endpoint the Client uses, plus the kept ones it doesn't, and the main error paths."""
    report = pdf_bytes(12 * 1024 * 1024 + 123, "report")              # 2 parts of 8 MiB
    api("check_duplicate", "POST", "/api/uploads/check-duplicate", {"filename": "report.pdf", "fileSize": len(report)})
    u = upload("report.pdf", report, "application/pdf", prefix="")
    api("complete", "POST", "/api/uploads/complete", complete_body(u), note="Non-ZIP → status 'stored'.")
    api("complete__409_already_completed", "POST", "/api/uploads/complete", complete_body(u))
    api("check_duplicate__true", "POST", "/api/uploads/check-duplicate", {"filename": "REPORT.pdf", "fileSize": len(report)},
        note="Name match is case-insensitive.")
    api("initiate__409_duplicate", "POST", "/api/uploads/initiate",
        {"filename": "report.pdf", "fileSize": len(report), "contentType": "application/pdf"})

    zip_data = (REPO / "fixtures" / "out" / "mixed.zip").read_bytes()
    z = upload("mixed.zip", zip_data, "application/zip")
    api("complete__zip_extracting", "POST", "/api/uploads/complete", complete_body(z),
        note="ZIP → key under incoming/, extract_zip enqueued on Redis, status 'extracting'. No worker was running. "
             "The server only checks the 'PK' header here; entry checks happen in the worker.")

    same = upload("copy-of-report.pdf", report, "application/pdf")
    api("complete__409_duplicate_by_hash", "POST", "/api/uploads/complete", complete_body(same),
        note="Same bytes, different name: passes initiate, refused at complete AFTER S3 completed the object. "
             "The object stays in S3 and the row stays 'initiated'.")

    bad = upload("bad.pdf", b"not a pdf at all" * 1000, "application/pdf")
    api("complete__400_validation", "POST", "/api/uploads/complete", complete_body(bad),
        note="Content check after S3 completion; object and 'initiated' row are left behind.")

    abort_data = pdf_bytes(6 * 1024 * 1024, "abort")
    status, init = api("_tmp", "POST", "/api/uploads/initiate",
                       {"filename": "abort-me.pdf", "fileSize": len(abort_data), "contentType": "application/pdf"})
    _, presigned = api("_tmp", "POST", "/api/uploads/parts/presign",
                       {"key": init["key"], "uploadId": init["uploadId"], "partNumbers": [1]})
    put_part(presigned["urls"]["1"], abort_data[: init["partSize"]])
    api("abort", "POST", "/api/uploads/abort", {"key": init["key"], "uploadId": init["uploadId"]},
        note="The Client sends this with keepalive and ignores the response. The 'initiated' row is deleted.")
    api("list_parts__404_after_abort", "GET",
        f"/api/uploads/{init['uploadId']}/parts?key={urllib.request.quote(init['key'])}")
    api("abort__200_unknown_pair", "POST", "/api/uploads/abort", {"key": "processed/nope/x.pdf", "uploadId": "nope"},
        note="Any pair is accepted: an unknown multipart upload is ignored and still returns ok. The Client also "
             "sends abort when a COMPLETED item is removed from the queue.")
    api("abort__200_completed_file", "POST", "/api/uploads/abort", {"key": u["key"], "uploadId": u["uploadId"]},
        note="What the Client sends when a finished upload is removed or the queue is cleared: harmless today "
             "because only 'initiated' rows are deleted.")

    api("list_uploads", "GET", "/api/uploads",
        note="Hides 'initiated' rows. fetchStoredUploads() in the Client calls this, but nothing calls "
             "fetchStoredUploads().")

    api("initiate__413_too_large", "POST", "/api/uploads/initiate",
        {"filename": "huge.pdf", "fileSize": 5120 * 1024 * 1024 + 1, "contentType": "application/pdf"})
    api("initiate__422_zero_size", "POST", "/api/uploads/initiate",
        {"filename": "empty.pdf", "fileSize": 0, "contentType": "application/pdf"},
        note="Pydantic Field(gt=0). The Client blocks empty files before calling.")
    api("parts_presign__400_empty", "POST", "/api/uploads/parts/presign",
        {"key": u["key"], "uploadId": u["uploadId"], "partNumbers": []})
    api("complete__400_no_parts", "POST", "/api/uploads/complete", {**complete_body(u), "parts": []})
    api("complete__404_unknown", "POST", "/api/uploads/complete",
        {**complete_body(u), "id": "00000000-0000-0000-0000-000000000000",
         "fileId": "00000000-0000-0000-0000-000000000000", "key": "processed/nope/x.pdf"})
    (OUT / "_tmp.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
