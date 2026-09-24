"""process_archive (design.md §8.3; R7, R8.1, R8.2): the loop, archive- vs entry-level failures, and every crash
window of the §8.3 table — crash at the exact point, run again, check nothing is duplicated, orphaned or wrong."""
import io
import struct
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from engine.errors import Rejected
from workers.internal_client import FileClaim, FinalStatus
from tests.engine_helpers import POC_LIMITS

ORG, BATCH, FILE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PREFIX = f"ClinSync/staging/{ORG}/{BATCH}/{FILE}/"


class SimulatedCrash(BaseException):
    """A process death at an exact point: no `except` in the worker catches it, like SIGKILL."""


class StatefulApi:
    """The Internal API's observable state: candidates keyed like uq_entry, progress fields, and crash hooks."""

    file_id = FILE

    def __init__(self) -> None:
        """Start empty."""
        self.candidates: dict[str | None, dict[str, Any]] = {}
        self.staged_ids: dict[str | None, uuid.UUID] = {}
        self.entries_total: int | None = None
        self.entries_done = 0
        self.detected_type: str | None = None
        self.upserts: list[int | None] = []
        self.progress_calls: list[dict[str, Any]] = []
        self.crash_after: tuple[str, int] | None = None

    def progress(self, **fields: Any) -> None:
        """Record progress, then crash if scripted to crash after progress for this entry."""
        self.progress_calls.append(fields)
        self.entries_total = fields.get("entries_total", self.entries_total)
        self.entries_done = fields.get("entries_done", self.entries_done)
        self.detected_type = fields.get("detected_type", self.detected_type)
        if self.crash_after == ("progress", fields.get("entries_done", -1) - 1):
            raise SimulatedCrash("after progress")

    def upsert_candidate(self, *, entry_name: str | None, entry_index: int | None, **fields: Any) -> uuid.UUID:
        """Insert or overwrite by entry name (uq_entry), then crash if scripted to crash after this upsert."""
        self.upserts.append(entry_index)
        self.candidates[entry_name] = {"entry_index": entry_index, **fields}
        staged_id = self.staged_ids.setdefault(entry_name, uuid.uuid4())
        if self.crash_after == ("upsert", entry_index):
            raise SimulatedCrash("after upsert")
        return staged_id

    def heartbeat(self) -> None:
        """Unused here."""

    def finish(self, final: FinalStatus) -> str:
        """The API's finish rule (rev 1.3, tested against the real API in test_repository_files_finish_archive):
        every entry needs a candidate; processed becomes partial if any candidate is rejected."""
        if final.status in ("processed", "partial"):
            assert len(self.candidates) == self.entries_total, "a worker that skipped an entry is refused"
            if final.status == "processed" and any(c["status"] == "rejected" for c in self.candidates.values()):
                return "partial"
        return final.status


class MemoryStore:
    """S3 as a dict of key → bytes, with a crash hook after an upload."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}
        self.crash_after_upload_of: int | None = None
        self.deleted_prefixes: list[str] = []

    def upload(self, path: Path, key: str) -> None:
        """Store the object, then crash if scripted to crash after uploading this entry."""
        self.objects[key] = path.read_bytes()
        if self.crash_after_upload_of is not None and key.startswith(f"{PREFIX}{self.crash_after_upload_of:04d}_"):
            raise SimulatedCrash("after upload")

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under prefix."""
        self.deleted_prefixes.append(prefix)
        doomed = [k for k in self.objects if k.startswith(prefix)]
        for k in doomed:
            del self.objects[k]
        return len(doomed)


class Beat:
    """A heartbeat stand-in that can report superseded."""

    def __init__(self) -> None:
        """Not superseded."""
        self.checks = 0

    def raise_if_superseded(self) -> None:
        """Count the check."""
        self.checks += 1


def claim(entries_done: int = 0, name: str = "mixed.zip") -> FileClaim:
    """A claim for the one archive file, with the given resume point."""
    return FileClaim(file_id=FILE, organization_id=ORG, batch_id=BATCH, s3_key=f"ClinSync/incoming/x/{name}",
                     file_name=name, file_ext="zip", is_archive=True, entries_done=entries_done, attempt_count=1,
                     claim_token=uuid.uuid4())


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> Path:
    """A deflated zip of members, in order."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """workers.ingest with a valid minimal environment."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    get_settings.cache_clear()
    import workers.ingest as module
    yield module
    get_settings.cache_clear()


@pytest.fixture
def docs(fixtures_dir: Path) -> dict[str, bytes]:
    """Bytes of a valid .docx and of a PE executable."""
    return {"docx": (fixtures_dir / "valid.docx").read_bytes(), "exe": (fixtures_dir / "renamed_exe.docx").read_bytes()}


def mixed(docs: dict[str, bytes], tmp_path: Path) -> tuple[Path, dict[str, dict[str, Any]]]:
    """Six entries: 4 valid .docx (two sharing a basename), a .pdf, and a PE executable named .docx."""
    members = [("docs/a.docx", docs["docx"]), ("notes.pdf", b"%PDF-1.4 x"), ("docs/b.docx", docs["docx"]),
               ("fake.docx", docs["exe"]), ("cardiology/overview.docx", docs["docx"]),
               ("pulmonology/overview.docx", docs["docx"])]
    expected = {
        "docs/a.docx": {"status": "processed", "s3_key": f"{PREFIX}0000_a.docx"},
        "notes.pdf": {"status": "rejected", "reject_reason": ".pdf is not supported"},
        "docs/b.docx": {"status": "processed", "s3_key": f"{PREFIX}0002_b.docx"},
        "fake.docx": {"status": "rejected",
                      "reject_reason": "File content does not match .docx — it appears to be unknown"},
        "cardiology/overview.docx": {"status": "processed", "s3_key": f"{PREFIX}0004_overview.docx"},
        "pulmonology/overview.docx": {"status": "processed", "s3_key": f"{PREFIX}0005_overview.docx"},
    }
    return write_zip(tmp_path / "mixed.zip", members), expected


def assert_final_state(api: StatefulApi, store: MemoryStore, expected: dict[str, dict[str, Any]],
                       final: FinalStatus, docs: dict[str, bytes]) -> None:
    """Exactly one candidate per entry, each correct; staged objects == processed candidates' keys."""
    assert set(api.candidates) == set(expected), "one candidate per entry, no duplicates, none missing"
    for name, want in expected.items():
        got = api.candidates[name]
        assert got["status"] == want["status"], name
        assert got.get("s3_key") == want.get("s3_key") and got.get("reject_reason") == want.get("reject_reason"), name
    processed_keys = {c["s3_key"] for c in api.candidates.values() if c["status"] == "processed"}
    assert set(store.objects) == processed_keys, "no orphaned or missing staging objects"
    assert all(store.objects[k] == docs["docx"] for k in processed_keys)
    assert api.entries_total == 6 and api.entries_done == 6
    assert api.finish(final) == "partial", "the API decides partial from the candidates, even after a resume"


# --- the loop ---

def test_full_run_stages_valid_entries_and_rejects_the_rest(ingest: Any, docs: dict[str, bytes], tmp_path: Path) -> None:
    """One uninterrupted run: outer zip → entries_total → per entry → progress after each → partial."""
    path, expected = mixed(docs, tmp_path)
    api, store, beat = StatefulApi(), MemoryStore(), Beat()
    final = ingest.process_archive(api, claim(), path, beat, POC_LIMITS, store)
    assert_final_state(api, store, expected, final, docs)
    assert api.progress_calls[:2] == [{"detected_type": "zip"}, {"entries_total": 6}]
    assert [c["entries_done"] for c in api.progress_calls[2:]] == [1, 2, 3, 4, 5, 6]
    assert beat.checks == 6


def test_done_when_resume_from_three_of_five(ingest: Any, docs: dict[str, bytes], tmp_path: Path) -> None:
    """Task 9.1 Done-when: entries_done=3 on a 5-entry archive → upserts for 3 and 4 only, then progress(5)."""
    path = write_zip(tmp_path / "five.zip", [(f"d{i}.docx", docs["docx"]) for i in range(5)])
    api, store = StatefulApi(), MemoryStore()
    ingest.process_archive(api, claim(entries_done=3), path, Beat(), POC_LIMITS, store)
    assert api.upserts == [3, 4]
    assert api.progress_calls[-1] == {"entries_done": 5}
    assert set(store.objects) == {f"{PREFIX}0003_d3.docx", f"{PREFIX}0004_d4.docx"}


# --- crash windows (design.md §8.3 table) ---

WINDOWS = ([("a_after_upload", k) for k in (0, 2, 4, 5)]                  # only processed entries upload
           + [("b_after_upsert", k) for k in range(6)] + [("c_after_progress", k) for k in range(6)])


@pytest.mark.parametrize(("window", "k"), WINDOWS, ids=[f"{w}-entry{k}" for w, k in WINDOWS])
def test_crash_window_then_resume(ingest: Any, docs: dict[str, bytes], tmp_path: Path, window: str, k: int) -> None:
    """Crash at the exact point on entry K, run again from the recorded entries_done: the end state is exact."""
    path, expected = mixed(docs, tmp_path)
    api, store = StatefulApi(), MemoryStore()
    if window == "a_after_upload":
        store.crash_after_upload_of = k
    else:
        api.crash_after = ("upsert" if window == "b_after_upsert" else "progress", k)

    with pytest.raises(SimulatedCrash):
        ingest.process_archive(api, claim(), path, Beat(), POC_LIMITS, store)
    resume_from = api.entries_done
    assert resume_from == (k + 1 if window == "c_after_progress" else k)
    staged_before = dict(api.staged_ids)

    api.crash_after, store.crash_after_upload_of, api.upserts = None, None, []
    final = ingest.process_archive(api, claim(entries_done=resume_from), path, Beat(), POC_LIMITS, store)

    assert api.upserts == list(range(resume_from, 6)), "the second run starts exactly at the resume point"
    assert all(api.staged_ids[n] == sid for n, sid in staged_before.items()), "replays overwrite, never re-insert"
    assert_final_state(api, store, expected, final, docs)


# --- archive-level vs entry-level ---

def test_word_file_renamed_zip_is_rejected_before_anything_is_staged(ingest: Any, fixtures_dir: Path) -> None:
    """Outer detection first: a .docx named .zip → Rejected, no entries_total, no candidates."""
    api, store = StatefulApi(), MemoryStore()
    with pytest.raises(Rejected, match="appears to be docx"):
        ingest.process_archive(api, claim(), fixtures_dir / "valid.docx", Beat(), POC_LIMITS, store)
    assert api.progress_calls == [{"detected_type": "docx"}] and api.candidates == {}


@pytest.mark.parametrize(("fixture", "reason"), [("bomb.zip", "compression ratio"), ("slip.zip", "unsafe path"),
                                                 ("encrypted.zip", "encrypted"), ("dupnames.zip", "more than once")])
def test_unsafe_outer_archive_is_rejected_whole(ingest: Any, fixtures_dir: Path, fixture: str, reason: str) -> None:
    """Archive guards (incl. R6.9 duplicate names) reject the whole archive before any entry."""
    api, store = StatefulApi(), MemoryStore()
    with pytest.raises(Rejected, match=reason):
        ingest.process_archive(api, claim(), fixtures_dir / fixture, Beat(), POC_LIMITS, store)
    assert api.candidates == {} and api.entries_total is None


def _lying_third_entry(docs: dict[str, bytes], tmp_path: Path) -> Path:
    """Three valid .docx; the third's central-directory size is patched below its real size (outer CRC lie)."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(3):
            zf.writestr(f"d{i}.docx", docs["docx"])
    raw = bytearray(data.getvalue())
    third = raw.rfind(b"PK\x01\x02")
    (real,) = struct.unpack_from("<I", raw, third + 24)
    struct.pack_into("<I", raw, third + 24, real // 2)                       # uncompressed size
    struct.pack_into("<I", raw, third + 20, real // 2)                       # compressed size (stored)
    path = tmp_path / "lying3.zip"
    path.write_bytes(bytes(raw))
    return path


def test_outer_lie_mid_way_deletes_the_staging_prefix_then_reraises(ingest: Any, docs: dict[str, bytes],
                                                                    tmp_path: Path) -> None:
    """Third entry fails its outer CRC → delete_prefix (entries 0–1's objects gone) → Rejected re-raised (§8.5a)."""
    api, store = StatefulApi(), MemoryStore()
    with pytest.raises(Rejected, match="does not match its index"):
        ingest.process_archive(api, claim(), _lying_third_entry(docs, tmp_path), Beat(), POC_LIMITS, store)
    assert api.upserts == [0, 1] and store.deleted_prefixes == [PREFIX] and store.objects == {}


def test_inner_bomb_docx_is_an_entry_rejection(ingest: Any, docs: dict[str, bytes], fixtures_dir: Path,
                                               tmp_path: Path) -> None:
    """A .docx inside the archive that is itself a zip bomb → that entry rejected 'unsafe', the rest staged."""
    path = tmp_path / "inner.zip"
    with zipfile.ZipFile(path, "w") as zf:        # bomb stored uncompressed, so the OUTER ratio stays ~1
        zf.writestr(zipfile.ZipInfo("a.docx"), docs["docx"], compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(zipfile.ZipInfo("bomb.docx"), (fixtures_dir / "bomb.zip").read_bytes(),
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr(zipfile.ZipInfo("c.docx"), docs["docx"], compress_type=zipfile.ZIP_DEFLATED)
    api, store = StatefulApi(), MemoryStore()
    final = ingest.process_archive(api, claim(), path, Beat(), POC_LIMITS, store)
    assert final == FinalStatus("partial") and api.finish(final) == "partial"
    assert api.candidates["bomb.docx"]["status"] == "rejected"
    assert api.candidates["bomb.docx"]["reject_reason"].startswith("File is unsafe:")
    assert [api.candidates[n]["status"] for n in ("a.docx", "c.docx")] == ["processed", "processed"]


# --- entries: names and extensions ---

def test_same_file_name_in_different_folders_gets_separate_keys(ingest: Any, docs: dict[str, bytes],
                                                                tmp_path: Path) -> None:
    """cardiology/overview.docx and pulmonology/overview.docx: two candidates, two objects, keys by index."""
    path = write_zip(tmp_path / "two.zip", [("cardiology/overview.docx", docs["docx"]),
                                            ("pulmonology/overview.docx", docs["docx"])])
    api, store = StatefulApi(), MemoryStore()
    assert ingest.process_archive(api, claim(), path, Beat(), POC_LIMITS, store) == FinalStatus("processed")
    assert set(store.objects) == {f"{PREFIX}0000_overview.docx", f"{PREFIX}0001_overview.docx"}
    assert {n: c["file_name"] for n, c in api.candidates.items()} == {
        "cardiology/overview.docx": "overview.docx", "pulmonology/overview.docx": "overview.docx"}


def test_spaces_and_non_ascii_names_are_kept_exactly(ingest: Any, docs: dict[str, bytes], tmp_path: Path) -> None:
    """'Kardiologie/Überblick Herz 2026.docx' → candidate and staging key keep the name byte-for-byte."""
    name = "Kardiologie/Überblick Herz 2026.docx"
    path = write_zip(tmp_path / "utf8.zip", [(name, docs["docx"])])
    api, store = StatefulApi(), MemoryStore()
    ingest.process_archive(api, claim(), path, Beat(), POC_LIMITS, store)
    assert api.candidates[name]["file_name"] == "Überblick Herz 2026.docx"
    assert list(store.objects) == [f"{PREFIX}0000_Überblick Herz 2026.docx"]


@pytest.mark.parametrize(("entry", "reason", "stored_ext"), [
    ("README", "Files without an extension are not supported", ""),
    ("data.averyverylongextension", ".averyverylongextension is not supported", "averyveryl"),
    ("scan.PDF", ".pdf is not supported", "pdf"),
])
def test_unsupported_extensions_are_entry_rejections(ingest: Any, tmp_path: Path, entry: str, reason: str,
                                                     stored_ext: str) -> None:
    """No extension, an over-long one (stored truncated to the 10-char column) or a disallowed one → rejected entry."""
    path = write_zip(tmp_path / "ext.zip", [(entry, b"content")])
    api, store = StatefulApi(), MemoryStore()
    assert api.finish(ingest.process_archive(api, claim(), path, Beat(), POC_LIMITS, store)) == "partial"
    assert api.candidates[entry]["reject_reason"] == reason and api.candidates[entry]["file_ext"] == stored_ext
    assert store.objects == {}


def test_extracted_temp_files_are_removed_on_every_path(ingest: Any, docs: dict[str, bytes], tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-entry temp files go on success, on rejection and when the upload fails (rev 1.3)."""
    import tempfile
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    path, _ = mixed(docs, tmp_path)
    ingest.process_archive(StatefulApi(), claim(), path, Beat(), POC_LIMITS, MemoryStore())
    assert list(scratch.iterdir()) == []
    store = MemoryStore()
    store.crash_after_upload_of = 0
    with pytest.raises(SimulatedCrash):
        ingest.process_archive(StatefulApi(), claim(), path, Beat(), POC_LIMITS, store)
    assert list(scratch.iterdir()) == []


def test_entry_delay_sleeps_after_each_entry(ingest: Any, docs: dict[str, bytes], tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """poc_delay() reads ENTRY_DELAY_SECONDS and sleeps once per entry, after its progress."""
    sleeps: list[float] = []
    monkeypatch.setattr(ingest.settings, "ENTRY_DELAY_SECONDS", 1.5)
    monkeypatch.setattr(ingest.time, "sleep", sleeps.append)
    path = write_zip(tmp_path / "three.zip", [(f"d{i}.docx", docs["docx"]) for i in range(3)])
    ingest.process_archive(StatefulApi(), claim(), path, Beat(), POC_LIMITS, MemoryStore())
    assert sleeps == [1.5, 1.5, 1.5]
