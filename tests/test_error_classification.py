"""Exception classification (task 10.1; design.md §11; R11.1, R11.2)."""
import ast
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from engine.errors import Rejected, Transient
from tests.test_worker_archive import Beat, MemoryStore, StatefulApi, claim
from tests.test_worker_ingest import FakeStore, Recorder, a_claim, finals, run
from tests.test_worker_ingest import ingest  # noqa: F401  (fixture)
from tests.engine_helpers import POC_LIMITS
from workers.internal_client import FinalStatus
from workers.s3 import S3ObjectNotFound

ROOT = Path(__file__).resolve().parents[1]
TRANSIENT_RAISERS = {"workers/s3.py", "workers/internal_client.py"}


def test_only_the_s3_helper_and_internal_client_raise_transient() -> None:
    """Every `Transient(...)` construction in app/, engine/ and workers/ lives in the two adapters."""
    found = set()
    for path in [*ROOT.glob("app/**/*.py"), *ROOT.glob("engine/**/*.py"), *ROOT.glob("workers/**/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Transient":
                found.add(str(path.relative_to(ROOT)))
    assert found == TRANSIENT_RAISERS


def test_rejected_and_transient_are_distinct() -> None:
    """Deterministic and retryable never overlap."""
    assert not issubclass(Rejected, Transient) and not issubclass(Transient, Rejected)


def _truncated(fixtures_dir: Path, tmp_path: Path, name: str) -> Path:
    """The fixture cut in half: zip signature intact, central directory gone."""
    data = (fixtures_dir / name).read_bytes()
    path = tmp_path / f"cut-{name}"
    path.write_bytes(data[: len(data) // 2])
    return path


def test_corrupt_outer_archive_is_rejected_archive_is_damaged(ingest: Any, fixtures_dir: Path,  # noqa: F811
                                                               tmp_path: Path) -> None:
    """A top-level .zip that zipfile cannot open → Rejected('Archive is damaged'), nothing staged."""
    api, store = StatefulApi(), MemoryStore()
    with pytest.raises(Rejected, match="^Archive is damaged$"):
        ingest.process_archive(api, claim(), _truncated(fixtures_dir, tmp_path, "mixed.zip"), Beat(), POC_LIMITS,
                               store)
    assert api.progress_calls == [{"detected_type": "corrupt"}] and api.candidates == {}


def test_bad_zip_on_open_is_archive_is_damaged(ingest: Any, fixtures_dir: Path, tmp_path: Path,  # noqa: F811
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: even if detection passed, BadZipFile when the loop opens the archive → 'Archive is damaged'."""
    def bad(*_: Any, **__: Any) -> None:
        raise zipfile.BadZipFile("File is not a zip file")

    monkeypatch.setattr(ingest.zipfile, "ZipFile", bad)
    monkeypatch.setattr(ingest, "detect_file_type", lambda *_: __import__("engine.file_signature").file_signature
                        .Detection("zip"))
    with pytest.raises(Rejected, match="^Archive is damaged$"):
        ingest.process_archive(StatefulApi(), claim(), fixtures_dir / "mixed.zip", Beat(), POC_LIMITS, MemoryStore())


def test_process_upload_finishes_a_damaged_archive_rejected(ingest: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                            fixtures_dir: Path, tmp_path: Path) -> None:
    """In the task: finish(rejected, 'Archive is damaged'), then the incoming object is deleted (design.md §11)."""
    rec = Recorder()
    store = FakeStore(rec, _truncated(fixtures_dir, tmp_path, "mixed.zip"))
    run(ingest, monkeypatch, a_claim("mixed.zip", is_archive=True), store, rec)
    assert finals(rec) == [FinalStatus("rejected", "Archive is damaged")]
    assert rec.names()[-1] == "delete"


def test_damaged_single_document_keeps_its_own_message(ingest: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                      fixtures_dir: Path, tmp_path: Path) -> None:
    """A truncated top-level .docx is a document problem: 'File is damaged and cannot be read'."""
    rec = Recorder()
    run(ingest, monkeypatch, a_claim("valid.docx"), FakeStore(rec, _truncated(fixtures_dir, tmp_path, "valid.docx")),
        rec)
    assert finals(rec) == [FinalStatus("rejected", "File is damaged and cannot be read")]


@pytest.mark.parametrize(("error", "finished", "propagates"), [
    (S3ObjectNotFound("gone"), [FinalStatus("error", "Uploaded object not found")], None),   # deterministic, no retry
    (Transient("S3 503"), [], Transient),                                                  # retried (task 10.2)
], ids=["missing incoming object", "transient"])
def test_download_failures_are_classified(ingest: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException,  # noqa: F811
                                          finished: list[FinalStatus], propagates: type | None) -> None:
    """Missing incoming/ object → finish error; Transient → not finished here, raised for the retry path."""
    rec = Recorder()
    if propagates:
        with pytest.raises(propagates):
            run(ingest, monkeypatch, a_claim(), FakeStore(rec, None, error), rec)
    else:
        run(ingest, monkeypatch, a_claim(), FakeStore(rec, None, error), rec)
    assert finals(rec) == finished
