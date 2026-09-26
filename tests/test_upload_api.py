"""Anugrah's Upload API beyond the fixtures (upload-ingest-merge U1–U4, U12, D4, D14, D15): batches, checks, errors."""
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import storage
from app.config import get_settings
from app.db import get_db_session
from app.main import app
from app.repositories import files
from tests.db_helpers import POC_ORG
from tests.upload_fakes import FakeStorage, Recorder, enqueued, fake_storage  # noqa: F401 — fixtures

MIB = 1024 * 1024


@pytest.fixture
def client(db_session: Session, fake_storage: FakeStorage, enqueued: Recorder) -> Iterator[TestClient]:  # noqa: F811
    """The real app on the rolled-back session, with fake S3 and a recorded enqueue; default settings."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def initiate(client: TestClient, filename: str = "valid.docx", size: int = 1000, **extra: Any) -> Any:
    """POST initiate with Anugrah's body (plus any extra fields, e.g. batchId)."""
    return client.post("/api/uploads/initiate", json={"filename": filename, "fileSize": size,
                                                       "contentType": "application/octet-stream", **extra})


def batch_files(db: Session, batch_id: str) -> list[dict[str, Any]]:
    """The batch's upload_file rows, oldest first, then by name (one test transaction shares created_at)."""
    return [dict(r) for r in db.execute(text("SELECT * FROM upload_file WHERE batch_id = :b "
                                             "ORDER BY created_at, file_name"), {"b": batch_id}).mappings()]


# ── 3.1 initiate: the row, the key, the parts ──────────────────────────────

def test_initiate_creates_a_staged_row_and_a_multipart_upload(client: TestClient, db_session: Session,
                                                              fake_storage: FakeStorage) -> None:  # noqa: F811
    """The row is staged with size, type, upload id and key; the multipart upload exists at that key (U1.3)."""
    body = initiate(client, "Mixed Docs.ZIP", 12 * MIB).json()
    (row,) = batch_files(db_session, body["batchId"])
    assert (row["status"], row["file_name"], row["file_ext"], row["is_archive"]) == \
        ("staged", "Mixed Docs.ZIP", "zip", True)
    assert (row["size_bytes"], row["content_type"], row["organization_id"]) == \
        (12 * MIB, "application/octet-stream", POC_ORG)
    assert (str(row["file_id"]), row["upload_id"], row["s3_key"]) == (body["id"], body["uploadId"], body["key"])
    assert fake_storage.keys[body["uploadId"]] == body["key"]
    assert body["key"] == f"ClinSync/incoming/{POC_ORG}/{body['batchId']}/{body['id']}_Mixed Docs.ZIP"


def test_the_server_builds_the_key_from_the_file_name_only(client: TestClient) -> None:
    """A path in the file name cannot steer the key (U1.6): only its last segment is used."""
    body = initiate(client, "../../etc/evil.docx").json()
    assert body["key"].endswith(f"/{body['id']}_evil.docx") and ".." not in body["key"]


@pytest.mark.parametrize(("size", "parts"), [(1, 1), (8 * MIB, 1), (8 * MIB + 1, 2), (20 * MIB, 3)])
def test_part_count_is_the_size_over_the_part_size(client: TestClient, size: int, parts: int) -> None:
    """partSize is UPLOAD_PART_SIZE_BYTES (8 MiB); totalParts rounds up."""
    body = initiate(client, size=size).json()
    assert (body["partSize"], body["totalParts"]) == (8 * MIB, parts)


def test_disallowed_type_is_400_with_the_default_settings(client: TestClient, db_session: Session,
                                                          fake_storage: FakeStorage) -> None:  # noqa: F811
    """ALLOWED_TOP_LEVEL_EXT is docx,zip by default (D2): a .pdf gets 400, and nothing is created (U1.5)."""
    response = initiate(client, "notes.pdf")
    assert response.status_code == 400 and response.json() == {"detail": "Unsupported file type: notes.pdf"}
    assert fake_storage.calls == [] and db_session.execute(text("SELECT count(*) FROM upload_file")).scalar() == 0


def test_a_failed_insert_aborts_the_multipart_upload(client: TestClient, fake_storage: FakeStorage,  # noqa: F811
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """No multipart upload is left without its row (Anugrah's order); the 500 keeps the detail body."""
    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(files, "create_upload", broken)
    with TestClient(app, raise_server_exceptions=False) as lenient:     # answer the 500 instead of re-raising
        response = initiate(lenient)
    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert fake_storage.calls == ["create_multipart", "abort_multipart"] and fake_storage.uploads == {}


@pytest.mark.parametrize(("error", "status", "detail"), [
    (ClientError({"Error": {"Code": "NoSuchBucket", "Message": "gone"}}, "CreateMultipartUpload"), 404,
     "S3 error (NoSuchBucket): gone"),
    (ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "CreateMultipartUpload"), 502,
     "S3 error (InternalError): boom"),
    (EndpointConnectionError(endpoint_url="http://localstack:4566"), 500,
     'Upload storage failed: Could not connect to the endpoint URL: "http://localstack:4566"'),
])
def test_storage_errors_keep_anugrahs_messages(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                               error: Exception, status: int, detail: str) -> None:
    """S3 failures answer as Anugrah's API did: 404/502 "S3 error (…)", 500 "Upload storage failed" (U12.1)."""
    def failing(*_: Any) -> str:
        raise error

    monkeypatch.setattr(storage, "create_multipart", failing)
    response = initiate(client)
    assert response.status_code == status and response.json() == {"detail": detail}


def test_no_poc_organization_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without POC_ORGANIZATION_ID the API cannot place a file: 503, with the detail body."""
    monkeypatch.delenv("POC_ORGANIZATION_ID", raising=False)
    get_settings.cache_clear()
    try:
        response = initiate(client)
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503 and response.json() == {"detail": "POC_ORGANIZATION_ID is not set"}


# ── 3.1 batches (D4) ───────────────────────────────────────────────────────

def test_without_a_batch_id_every_initiate_makes_a_new_batch(client: TestClient) -> None:
    """Old calls without batchId still work: each gets its own new batch (U1.2)."""
    first, second = initiate(client).json(), initiate(client, "b.docx").json()
    assert first["batchId"] != second["batchId"]


def test_initiates_with_one_batch_id_share_the_batch(client: TestClient, db_session: Session) -> None:
    """The first sight creates the batch, the next joins it; the batch is the POC org's, created by POC_USER_ID."""
    batch_id = str(uuid.uuid4())
    responses = [initiate(client, name, batchId=batch_id).json() for name in ("a.docx", "b.docx", "c.zip")]
    assert {r["batchId"] for r in responses} == {batch_id}
    assert [f["file_name"] for f in batch_files(db_session, batch_id)] == ["a.docx", "b.docx", "c.zip"]
    batch = db_session.execute(text("SELECT organization_id, created_by, status FROM upload_batch "
                                    "WHERE batch_id = :b"), {"b": batch_id}).one()
    assert tuple(batch) == (POC_ORG, 0, "in_progress")


@pytest.mark.parametrize("status", ["committed", "abandoned"])
def test_a_closed_batch_is_409_and_nothing_is_started(client: TestClient, db_session: Session,
                                                      fake_storage: FakeStorage, status: str) -> None:  # noqa: F811
    """Joining a committed or abandoned batch → 409 with the detail body; no multipart upload, no row."""
    batch_id = initiate(client).json()["batchId"]
    db_session.execute(text("UPDATE upload_batch SET status = :s WHERE batch_id = :b"), {"s": status, "b": batch_id})
    db_session.commit()
    calls_before = list(fake_storage.calls)
    response = initiate(client, "late.docx", batchId=batch_id)
    assert response.status_code == 409
    assert response.json() == {"detail": f"Batch {batch_id} is already {status}."}
    assert fake_storage.calls == calls_before and len(batch_files(db_session, batch_id)) == 1


def test_a_malformed_batch_id_is_422(client: TestClient) -> None:
    """batchId must be a UUID; FastAPI's 422 with the detail list, as for any bad field."""
    response = initiate(client, batchId="not-a-uuid")
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["body", "batchId"]


def test_parallel_initiates_with_one_new_batch_id_land_in_one_batch(
    race_engine: Engine, fake_storage: FakeStorage, enqueued: Recorder  # noqa: F811
) -> None:
    """D4: eight initiates at once, one new batchId, separate connections → one batch with eight files."""
    batch_id = str(uuid.uuid4())

    def session() -> Iterator[Session]:
        with Session(race_engine) as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    barrier, results = threading.Barrier(8), []
    try:
        with TestClient(app) as test_client:
            def go(i: int) -> None:
                barrier.wait()
                results.append(initiate(test_client, f"f{i}.docx", batchId=batch_id).status_code)
            threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        with race_engine.connect() as conn:
            batches = conn.execute(text("SELECT count(*) FROM upload_batch WHERE batch_id = :b"),
                                   {"b": batch_id}).scalar()
            files_in = conn.execute(text("SELECT count(*) FROM upload_file WHERE batch_id = :b"),
                                    {"b": batch_id}).scalar()
    finally:
        app.dependency_overrides.clear()
        with race_engine.begin() as conn:
            conn.execute(text("DELETE FROM upload_batch WHERE batch_id = :b"), {"b": batch_id})
    assert results == [200] * 8 and (batches, files_in) == (1, 8)


# ── error bodies chosen by route (D14, U12) and CORS ───────────────────────

def test_upload_api_errors_are_detail_and_v1_errors_keep_the_envelope(client: TestClient) -> None:
    """Same app, same kinds of error: /api/uploads/* answers {"detail"}, /api/v1/* the {"error"} envelope."""
    assert client.post("/api/uploads/initiate", json={"filename": "a.docx"}).json()["detail"][0]["loc"] == \
        ["body", "fileSize"]
    assert client.get("/api/uploads/nowhere").json() == {"detail": "Not Found"}
    assert client.post("/api/v1/uploads/not-a-uuid/confirm").json()["error"]["code"] == "validation_error"
    assert client.get("/api/v1/nowhere").json()["error"]["code"] == "not_found"


def test_cors_allows_the_client_origin_only(client: TestClient) -> None:
    """A preflight from http://localhost:5173 is allowed; another origin is not."""
    ok = client.options("/api/uploads/initiate", headers={"Origin": "http://localhost:5173",
                                                          "Access-Control-Request-Method": "POST"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    refused = client.options("/api/uploads/initiate", headers={"Origin": "http://evil.example",
                                                               "Access-Control-Request-Method": "POST"})
    assert refused.status_code == 400 and "access-control-allow-origin" not in refused.headers


def test_check_duplicate_ignores_uploads_not_in_the_library(client: TestClient) -> None:
    """Only library documents count (U6.1): a file merely uploaded is not a duplicate."""
    initiate(client, "report.docx", 5000)
    response = client.post("/api/uploads/check-duplicate", json={"filename": "report.docx", "fileSize": 5000})
    assert response.json() == {"duplicate": False, "message": None}



# ── 3.2 parts/presign and parts (U2) ───────────────────────────────────────

REAL_PRESIGN = storage.presign_part                       # captured before the fake_storage fixture patches it


def presign(client: TestClient, body: dict[str, Any], parts: list[int]) -> Any:
    """POST parts/presign for an initiate response's key and uploadId."""
    return client.post("/api/uploads/parts/presign",
                       json={"key": body["key"], "uploadId": body["uploadId"], "partNumbers": parts})


def status_of(db: Session, body: dict[str, Any]) -> str:
    """The file's status now."""
    return db.execute(text("SELECT status FROM upload_file WHERE file_id = :id"), {"id": body["id"]}).scalar_one()


def test_first_presign_moves_staged_to_uploading(client: TestClient, db_session: Session) -> None:
    """staged → uploading on the first presign (U1.3); a second presign keeps it uploading."""
    body = initiate(client, size=20 * MIB).json()
    assert status_of(db_session, body) == "staged"
    assert presign(client, body, [1, 2]).status_code == 200
    assert status_of(db_session, body) == "uploading"
    assert presign(client, body, [3]).status_code == 200 and status_of(db_session, body) == "uploading"


@pytest.mark.parametrize("wrong", ["key", "uploadId"])
def test_presign_refuses_a_pair_matching_no_row(client: TestClient, wrong: str) -> None:
    """Both halves must match one row (U2.2): a right key with another uploadId, or the reverse, is 404."""
    body = initiate(client).json()
    response = client.post("/api/uploads/parts/presign",
                           json={"key": body["key"], "uploadId": body["uploadId"], wrong: "other",
                                 "partNumbers": [1]})
    assert response.status_code == 404 and response.json() == {"detail": "Upload not found."}


@pytest.mark.parametrize("status", ["uploaded", "processing", "processed", "error"])
def test_presign_refuses_a_file_past_uploading(client: TestClient, db_session: Session, status: str) -> None:
    """A file no longer awaiting parts → 409 with Anugrah's text; its status is unchanged."""
    body = initiate(client).json()
    db_session.execute(text("UPDATE upload_file SET status = :s WHERE file_id = :id"), {"s": status, "id": body["id"]})
    db_session.commit()
    response = presign(client, body, [1])
    assert response.status_code == 409 and response.json() == {"detail": "Upload is not awaiting completion."}
    assert status_of(db_session, body) == status


def test_presigned_urls_name_the_public_host(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the real presign (no network needed), every URL is on S3_PUBLIC_ENDPOINT_URL, not the internal host."""
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    for cached in (get_settings, storage.public_client, storage.internal_client):
        cached.cache_clear()
    monkeypatch.setattr(storage, "presign_part", REAL_PRESIGN)
    try:
        body = initiate(client, size=20 * MIB).json()
        urls = presign(client, body, [1, 2, 3]).json()["urls"]
    finally:
        for cached in (get_settings, storage.public_client, storage.internal_client):
            cached.cache_clear()
    assert sorted(urls) == ["1", "2", "3"]
    assert all(u.startswith(f"http://localhost:4566/{get_settings().S3_BUCKET}/{body['key']}?") for u in urls.values())


def test_list_parts_checks_the_pair_and_status(client: TestClient, db_session: Session) -> None:
    """GET …/parts: 404 for an unknown pair, 409 for a file past uploading — the same row check as presign."""
    body = initiate(client).json()
    unknown = client.get("/api/uploads/other/parts", params={"key": body["key"]})
    assert unknown.status_code == 404 and unknown.json() == {"detail": "Upload not found."}
    db_session.execute(text("UPDATE upload_file SET status = 'uploaded' WHERE file_id = :id"), {"id": body["id"]})
    db_session.commit()
    past = client.get(f"/api/uploads/{body['uploadId']}/parts", params={"key": body["key"]})
    assert past.status_code == 409 and past.json() == {"detail": "Upload is not awaiting completion."}


def test_list_parts_of_an_upload_s3_no_longer_has_is_404(client: TestClient, fake_storage: FakeStorage  # noqa: F811
                                                        ) -> None:
    """S3 lost the multipart upload (lifecycle expiry) → 404 "S3 error (NoSuchUpload): …", as Anugrah's API."""
    body = initiate(client).json()
    fake_storage.uploads.pop(body["uploadId"])
    response = client.get(f"/api/uploads/{body['uploadId']}/parts", params={"key": body["key"]})
    assert response.status_code == 404 and response.json()["detail"].startswith("S3 error (NoSuchUpload): ")


# ── 3.3 abort (U4.1, D15) and GET /api/uploads (U4.2) ──────────────────────

def abort(client: TestClient, body: dict[str, Any]) -> Any:
    """POST abort for an initiate response's key and uploadId."""
    return client.post("/api/uploads/abort", json={"key": body["key"], "uploadId": body["uploadId"]})


@pytest.mark.parametrize("presigned", [False, True], ids=["staged", "uploading"])
def test_abort_cancels_a_staged_or_uploading_file(client: TestClient, db_session: Session,
                                                  fake_storage: FakeStorage, presigned: bool) -> None:  # noqa: F811
    """The multipart upload is aborted and the row ends error "Upload cancelled" — kept, not deleted (D10)."""
    body = initiate(client).json()
    if presigned:
        presign(client, body, [1])
    assert abort(client, body).json() == {"ok": True}
    assert body["uploadId"] not in fake_storage.uploads and "abort_multipart" in fake_storage.calls
    row = db_session.execute(text("SELECT status, status_message FROM upload_file WHERE file_id = :id"),
                             {"id": body["id"]}).one()
    assert tuple(row) == ("error", "Upload cancelled")


@pytest.mark.parametrize("status", ["uploaded", "processing", "processed", "partial", "rejected", "error"])
def test_abort_after_the_upload_changes_nothing(client: TestClient, db_session: Session,
                                                fake_storage: FakeStorage, status: str) -> None:  # noqa: F811
    """D15: any later status → {"ok": true}, the full row unchanged, no S3 call."""
    body = initiate(client).json()
    db_session.execute(text("UPDATE upload_file SET status = :s WHERE file_id = :id"), {"s": status, "id": body["id"]})
    db_session.commit()
    before = dict(db_session.execute(text("SELECT * FROM upload_file WHERE file_id = :id"),
                                     {"id": body["id"]}).mappings().one())
    calls = list(fake_storage.calls)
    assert abort(client, body).json() == {"ok": True}
    after = dict(db_session.execute(text("SELECT * FROM upload_file WHERE file_id = :id"),
                                    {"id": body["id"]}).mappings().one())
    assert after == before and fake_storage.calls == calls


def test_aborting_twice_is_harmless(client: TestClient, db_session: Session) -> None:
    """The second abort finds the file cancelled — a later status — and changes nothing."""
    body = initiate(client).json()
    assert abort(client, body).json() == abort(client, body).json() == {"ok": True}
    assert status_of(db_session, body) == "error"


def test_a_failed_s3_abort_leaves_the_file_as_it_was(client: TestClient, db_session: Session,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 refusing the abort → 502 with Anugrah's text, and the file is still uploading, so it can be retried."""
    body = initiate(client).json()
    presign(client, body, [1])

    def refused(*_: Any) -> None:
        raise ClientError({"Error": {"Code": "InternalError", "Message": "try later"}}, "AbortMultipartUpload")

    monkeypatch.setattr(storage, "abort_multipart", refused)
    response = abort(client, body)
    assert response.status_code == 502 and response.json() == {"detail": "S3 error (InternalError): try later"}
    assert status_of(db_session, body) == "uploading"


def test_list_hides_files_still_uploading(client: TestClient, db_session: Session) -> None:
    """GET /api/uploads lists files past uploading, as Anugrah's hid `initiated`; nulls where the LLD has no column."""
    staged, uploading, done = (initiate(client, f"{n}.docx").json() for n in ("staged", "uploading", "done"))
    presign(client, uploading, [1])
    db_session.execute(text("UPDATE upload_file SET status = 'processed' WHERE file_id = :id"), {"id": done["id"]})
    db_session.commit()
    (item,) = client.get("/api/uploads").json()
    assert (item["id"], item["filename"], item["status"], item["s3_key"]) == \
        (done["id"], "done.docx", "processed", done["key"])
    assert (item["s3_location"], item["parent_id"], item["source_path"]) == (None, None, None)
