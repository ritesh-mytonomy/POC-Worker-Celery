"""process_upload and process_document (design.md §8.1, §8.2; R3.4, R5, R10.2, R10.3) against fakes — no services."""
import logging
import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.errors import ClaimSuperseded
from engine.errors import Rejected, Transient
from workers.internal_client import FileClaim, FinalStatus
from workers.s3 import S3ConfigError, S3ObjectNotFound


class Recorder:
    """One ordered log of every API and S3 call, so tests can assert order (finish before delete)."""

    def __init__(self) -> None:
        """Start empty."""
        self.calls: list[tuple[Any, ...]] = []

    def names(self) -> list[str]:
        """Just the call names, in order."""
        return [c[0] for c in self.calls]


class FakeBound:
    """The token-bound client: records writes; optionally loses ownership at finish."""

    file_id = "f"

    def __init__(self, rec: Recorder, fail_finish: bool) -> None:
        """Share the recorder."""
        self.rec, self.fail_finish = rec, fail_finish

    def heartbeat(self) -> None:
        """Record."""
        self.rec.calls.append(("heartbeat",))

    def progress(self, **fields: Any) -> None:
        """Record."""
        self.rec.calls.append(("progress", fields))

    def upsert_candidate(self, **fields: Any) -> uuid.UUID:
        """Record."""
        self.rec.calls.append(("upsert_candidate", fields))
        return uuid.uuid4()

    def finish(self, final: FinalStatus) -> None:
        """Record, or raise ClaimSuperseded."""
        self.rec.calls.append(("finish", final))
        if self.fail_finish:
            raise ClaimSuperseded("f")


class FakeInternal:
    """Scripted claim; bound writes are recorded."""

    def __init__(self, rec: Recorder, claim: FileClaim | None, fail_finish: bool = False) -> None:
        """Script the claim result."""
        self.rec, self.claim_result, self.fail_finish = rec, claim, fail_finish

    def claim(self, file_id: str) -> FileClaim | None:
        """Record and answer."""
        self.rec.calls.append(("claim", file_id))
        return self.claim_result

    def with_token(self, file_id: str, token: uuid.UUID) -> FakeBound:
        """Record the binding."""
        self.rec.calls.append(("with_token", file_id, token))
        return FakeBound(self.rec, self.fail_finish)


class FakeStore:
    """S3: download serves a local fixture (or raises); copy and delete are recorded."""

    def __init__(self, rec: Recorder, source: Path | None, error: BaseException | None = None) -> None:
        """Serve `source` as the incoming object, or raise `error`."""
        self.rec, self.source, self.error = rec, source, error
        self.downloaded: list[Path] = []

    def download_to_tmp(self, key: str, tmp_dir: Path | None = None) -> Path:
        """Record, then copy the fixture to a temp file (or raise)."""
        self.rec.calls.append(("download", key))
        if self.error is not None:
            raise self.error
        assert self.source is not None
        out = Path(shutil.copy(self.source, self.source.parent / f"dl-{uuid.uuid4().hex}"))
        self.downloaded.append(out)
        return out

    def copy(self, src: str, dst: str) -> None:
        """Record."""
        self.rec.calls.append(("copy", src, dst))

    def delete_quietly(self, key: str) -> None:
        """Record."""
        self.rec.calls.append(("delete", key))

    def upload(self, path: Path, key: str) -> None:
        """Record."""
        self.rec.calls.append(("upload", key))

    def delete_prefix(self, prefix: str) -> int:
        """Record."""
        self.rec.calls.append(("delete_prefix", prefix))
        return 0


def a_claim(file_name: str = "valid.docx", is_archive: bool = False) -> FileClaim:
    """A FileClaim for a fresh file."""
    ext = file_name.rsplit(".", 1)[-1]
    return FileClaim(file_id=uuid.uuid4(), organization_id=uuid.uuid4(), batch_id=uuid.uuid4(),
                     s3_key=f"ClinSync/incoming/x/{file_name}", file_name=file_name, file_ext=ext,
                     is_archive=is_archive, entries_done=0, attempt_count=1, claim_token=uuid.uuid4())


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """workers.ingest with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.ingest as module
    yield module
    get_settings.cache_clear()


def run(ingest: Any, monkeypatch: pytest.MonkeyPatch, claim: FileClaim | None, store: FakeStore,
        rec: Recorder, fail_finish: bool = False) -> None:
    """Run the task eagerly against fakes."""
    fake = FakeInternal(rec, claim, fail_finish)
    monkeypatch.setattr(ingest, "internal", lambda: fake)
    monkeypatch.setattr(ingest, "s3", lambda: store)
    file_id = str(claim.file_id) if claim else str(uuid.uuid4())
    ingest.process_upload.apply(kwargs={"file_id": file_id, "organization_id": str(uuid.uuid4())}).get()


def finals(rec: Recorder) -> list[FinalStatus]:
    """Every finish() argument."""
    return [c[1] for c in rec.calls if c[0] == "finish"]


# --- process_upload paths ---

def test_valid_docx_is_staged_then_finished_then_incoming_deleted(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                                  fixtures_dir: Path, tmp_path: Path) -> None:
    """valid.docx: progress docx → server-side copy to …/{file_id}/0000_valid.docx → candidate → finish → delete."""
    rec, claim = Recorder(), a_claim()
    store = FakeStore(rec, Path(shutil.copy(fixtures_dir / "valid.docx", tmp_path)))
    run(ingest, monkeypatch, claim, store, rec)
    key = f"ClinSync/staging/{claim.organization_id}/{claim.batch_id}/{claim.file_id}/0000_valid.docx"
    work = [c for c in rec.calls if c[0] != "heartbeat"]
    assert [c[0] for c in work] == ["claim", "with_token", "download", "progress", "copy", "upsert_candidate",
                                    "finish", "delete"]
    assert work[3][1] == {"detected_type": "docx"} and work[4] == ("copy", claim.s3_key, key)
    candidate = work[5][1]
    assert candidate == {"entry_name": None, "entry_index": None, "file_name": "valid.docx", "file_ext": "docx",
                         "size_bytes": (fixtures_dir / "valid.docx").stat().st_size, "s3_key": key,
                         "status": "processed"}
    assert finals(rec) == [FinalStatus("processed")] and work[7] == ("delete", claim.s3_key)
    assert all(not p.exists() for p in store.downloaded)                  # temp download removed


@pytest.mark.parametrize(("fixture", "named"), [("renamed_exe.docx", "unknown"), ("renamed_zip.docx", "zip")])
def test_renamed_file_is_rejected_naming_its_real_type(ingest: Any, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path,
                                                       tmp_path: Path, fixture: str, named: str) -> None:
    """A renamed file → finish(rejected) naming the detected type, no copy, no candidate, incoming deleted (R5.3)."""
    rec, claim = Recorder(), a_claim(fixture)
    store = FakeStore(rec, Path(shutil.copy(fixtures_dir / fixture, tmp_path)))
    run(ingest, monkeypatch, claim, store, rec)
    (final,) = finals(rec)
    assert final.status == "rejected" and f"appears to be {named}" in (final.message or "")
    assert "copy" not in rec.names() and "upsert_candidate" not in rec.names()
    assert rec.names()[-1] == "delete" and rec.names().index("finish") < rec.names().index("delete")


def test_unsafe_docx_is_rejected(ingest: Any, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path,
                                 tmp_path: Path) -> None:
    """A .docx that trips an archive guard (R5.5) → rejected 'File is unsafe: …'."""
    rec, claim = Recorder(), a_claim("report.docx")
    store = FakeStore(rec, Path(shutil.copy(fixtures_dir / "bomb.zip", tmp_path / "report.docx")))
    run(ingest, monkeypatch, claim, store, rec)
    (final,) = finals(rec)
    assert final.status == "rejected" and (final.message or "").startswith("File is unsafe:")


def test_missing_incoming_object_finishes_error_without_retry(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S3ObjectNotFound → finish(error, 'Uploaded object not found'); the task completes (no retry)."""
    rec = Recorder()
    run(ingest, monkeypatch, a_claim(), FakeStore(rec, None, S3ObjectNotFound("gone")), rec)
    assert finals(rec) == [FinalStatus("error", "Uploaded object not found")]


@pytest.mark.parametrize("error", [Transient("S3 503"), S3ConfigError("access denied")], ids=["Transient", "S3ConfigError"])
def test_other_s3_failures_propagate_without_finishing(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                       error: BaseException) -> None:
    """Transient (retry arrives in 10.x) and S3ConfigError fail the task; the file is not finished here."""
    rec = Recorder()
    with pytest.raises(type(error)):
        run(ingest, monkeypatch, a_claim(), FakeStore(rec, None, error), rec)
    assert finals(rec) == []


def test_not_claimable_does_not_download(ingest: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """claim → None: claim_lost, no download (R3.4)."""
    rec = Recorder()
    run(ingest, monkeypatch, None, FakeStore(rec, None), rec)
    assert rec.names() == ["claim"]


def test_superseded_at_finish_is_logged_and_incoming_kept(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                         fixtures_dir: Path, tmp_path: Path,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    """Losing ownership at finish → warning; the new owner, not us, deletes the incoming object."""
    rec = Recorder()
    store = FakeStore(rec, Path(shutil.copy(fixtures_dir / "valid.docx", tmp_path)))
    with caplog.at_level(logging.WARNING, logger="workers.ingest"):
        run(ingest, monkeypatch, a_claim(), store, rec, fail_finish=True)
    assert any(r.getMessage() == "claim_superseded" for r in caplog.records)
    assert "delete" not in rec.names()


def test_archive_goes_through_process_archive_then_finish_then_delete(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                                      fixtures_dir: Path, tmp_path: Path) -> None:
    """mixed.zip: 3 entries uploaded, 2 rejected, finish(partial), then the incoming object is deleted."""
    rec = Recorder()
    store = FakeStore(rec, Path(shutil.copy(fixtures_dir / "mixed.zip", tmp_path)))
    claim = a_claim("mixed.zip", is_archive=True)
    run(ingest, monkeypatch, claim, store, rec)
    assert finals(rec) == [FinalStatus("partial")]
    assert sum(n == "upload" for n in rec.names()) == 3 and sum(n == "upsert_candidate" for n in rec.names()) == 5
    assert rec.names()[-1] == "delete" and rec.names().index("finish") < rec.names().index("delete")


def test_heartbeat_thread_is_stopped_when_the_task_ends(ingest: Any, monkeypatch: pytest.MonkeyPatch,
                                                         fixtures_dir: Path, tmp_path: Path) -> None:
    """No heartbeat thread outlives the task, on success or on a superseded finish."""
    for fail in (False, True):
        rec = Recorder()
        store = FakeStore(rec, Path(shutil.copy(fixtures_dir / "valid.docx", tmp_path / f"v{fail}.docx")))
        run(ingest, monkeypatch, a_claim(), store, rec, fail_finish=fail)
    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat-")]


def test_staging_key_is_deterministic(ingest: Any) -> None:
    """…/staging/{org}/{batch}/{file_id}/{index:04d}_{name}; the same claim always gives the same key."""
    claim = a_claim()
    assert ingest.staging_key(claim, 0, "valid.docx") == ingest.staging_key(claim, 0, "valid.docx")
    assert ingest.staging_key(claim, 7, "a.docx").endswith(f"/{claim.file_id}/0007_a.docx")
    assert ingest.staging_prefix(claim) == (f"ClinSync/staging/{claim.organization_id}/{claim.batch_id}/"
                                            f"{claim.file_id}/")


def test_task_is_registered_under_the_queue_contract_name(ingest: Any) -> None:
    """workers.ingest.process_upload (design.md §6.3), routed to clinsync.ingest."""
    from workers.celery_app import app
    assert "workers.ingest.process_upload" in app.tasks
    assert app.amqp.router.route({}, "workers.ingest.process_upload")["queue"].name == "clinsync.ingest"
