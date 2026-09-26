"""Checkpoint M1 (upload-ingest-merge task 3.6): upload through Anugrah's API exactly as the browser does.

From the host, standard library only, the calls s3ChunkedUpload.ts and UploadQueueContext make:
  check-duplicate → initiate (one client-generated batchId for the whole click, both files IN PARALLEL) →
  parts/presign → PUT every part straight to S3 (4 at a time, no Content-Type, ETag read from the response) →
  complete — then wait for the Ingest worker to process both.
Two files: valid.docx (one part) and padded.docx (valid.docx plus a 9 MiB stored media entry — two 8 MiB parts).
Assert: both land in the one batch, complete answers `uploaded`, every presigned URL names localhost:4566, and each
file ends `processed` with exactly one candidate, carrying the SHA-256 of its bytes, and its incoming/ object
gone. Becomes U-S1 in Phase 7.

Usage: python3 scripts/check_m1_upload.py [--keep]
"""
import hashlib
import http.client
import io
import os
import urllib.parse
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import lib

ORIGIN = "http://localhost:5173"
CONCURRENT_PARTS = 4                     # VITE_UPLOAD_CONCURRENCY's default


def padded_docx(valid: bytes) -> bytes:
    """valid.docx with a 9 MiB media entry, stored (no compression), so the file spans two 8 MiB parts."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(valid)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr(zipfile.ZipInfo("word/media/padding.bin"), os.urandom(9 * 1024 * 1024),
                     compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def put_part(url: str, data: bytes) -> tuple[int, str | None]:
    """PUT one part to its presigned URL with no Content-Type, as the browser's XHR does; return (status, ETag)."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
    try:
        connection.request("PUT", f"{parts.path}?{parts.query}", body=data,
                           headers={"Origin": ORIGIN, "Content-Length": str(len(data))})
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("ETag")
    finally:
        connection.close()


def main() -> int:
    """Run M1; return the exit code."""
    s = lib.Scenario("M1 upload through Anugrah's API")
    valid = (lib.ensure_fixtures() / "valid.docx").read_bytes()
    files = {f"m1-{uuid.uuid4().hex[:6]}-valid.docx": valid,
             f"m1-{uuid.uuid4().hex[:6]}-padded.docx": padded_docx(valid)}
    batch_id = str(uuid.uuid4())                                   # one per Upload click (D4)
    keys: list[str] = []
    try:
        for name, data in files.items():
            status, body = lib.api("POST", "/api/uploads/check-duplicate", {"filename": name, "fileSize": len(data)})
            s.check(f"check-duplicate {name}", (status, body), (200, {"duplicate": False, "message": None}))

        def initiate(item: tuple[str, bytes]) -> tuple[int, Any]:
            name, data = item
            return lib.api("POST", "/api/uploads/initiate", {
                "filename": name, "fileSize": len(data), "batchId": batch_id,
                "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})

        with ThreadPoolExecutor(len(files)) as pool:                 # both initiates at once, as handleUpload does
            started = dict(zip(files, pool.map(initiate, files.items())))
        s.check("initiate statuses", sorted(st for st, _ in started.values()), [200, 200])
        uploads = {name: body for name, (_, body) in started.items()}
        keys = [u["key"] for u in uploads.values()]
        s.check("both initiates answered our batchId", {u["batchId"] for u in uploads.values()}, {batch_id})
        s.check("parts per file", sorted(u["totalParts"] for u in uploads.values()), [1, 2])

        jobs = []
        for name, u in uploads.items():
            numbers = list(range(1, u["totalParts"] + 1))
            status, body = lib.api("POST", "/api/uploads/parts/presign",
                                   {"key": u["key"], "uploadId": u["uploadId"], "partNumbers": numbers})
            s.check(f"presign {name}", status, 200)
            hosts = {urllib.parse.urlsplit(url).netloc for url in body["urls"].values()}
            s.check(f"presigned host {name}", hosts, {"localhost:4566"})
            for n in numbers:
                chunk = files[name][(n - 1) * u["partSize"]: n * u["partSize"]]
                jobs.append((name, n, body["urls"][str(n)], chunk))

        with ThreadPoolExecutor(CONCURRENT_PARTS) as pool:
            results = list(pool.map(lambda job: (job[0], job[1], *put_part(job[2], job[3])), jobs))
        s.check("every part PUT answered 200 with an ETag",
                all(status == 200 and etag for _, _, status, etag in results), True)

        for name, u in uploads.items():
            parts = [{"partNumber": n, "etag": etag} for f, n, _, etag in results if f == name]
            status, body = lib.api("POST", "/api/uploads/complete", {
                "id": u["id"], "fileId": u["fileId"], "key": u["key"], "uploadId": u["uploadId"], "filename": name,
                "fileSize": len(files[name]), "contentType": "application/octet-stream", "parts": parts})
            s.check(f"complete {name}", (status, body["status"], body["batchId"]), (200, "uploaded", batch_id))

        final = lib.wait_terminal(batch_id, timeout=90)
        s.check("files in the one batch", sorted(f["file_name"] for f in final), sorted(files))
        s.check("statuses", sorted(f["status"] for f in final), ["processed", "processed"])
        candidates = lib.staged(batch_id)
        s.check("one processed candidate per file",
                sorted((c["file_name"], c["status"]) for c in candidates), sorted((n, "processed") for n in files))
        s.check("incoming/ objects gone", [k for k in keys if lib.s3_keys(k)], [])
        stored = dict(line.split("|") for line in lib.psql(
            f"SELECT file_name || '|' || content_hash FROM staged_document WHERE batch_id = '{batch_id}'").splitlines())
        s.check("each candidate's content_hash is the SHA-256 of the bytes uploaded (U5.2)", stored,
                {name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
        lib.assert_no_undeliverable(s)
    finally:
        if lib.api("GET", f"/api/v1/uploads/batches/{batch_id}")[0] == 200:     # created by the first initiate
            lib.cleanup(s, batch_id, keys)
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
