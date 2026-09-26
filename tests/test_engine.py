"""Tests for engine/file_checks.py: detection (design.md §8.4; R5.1, R5.2, R5.5), archive guards (§8.5; R6.1–R6.6,
R6.9, R6.10) and streaming extraction (R6.7, R6.8, NFR-4). No services, no settings."""
import hashlib
import io
import shutil
import struct
import subprocess
import sys
import tracemalloc
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from engine import file_checks
from engine.file_checks import (
    CHUNK, WORDML, Detection, Rejected, assert_safe_path, detect_file_type, extract_streaming, inspect_archive,
    mismatch_reason,
)
from tests.engine_helpers import POC_LIMITS, write_zip


# ============================================================ detection

def detect(path: Path) -> Detection:
    """detect_file_type with the POC limits."""
    return detect_file_type(path, POC_LIMITS)


# --- the Done-when cases ---

@pytest.mark.parametrize(("fixture", "expected"), [
    ("valid.docx", "docx"),
    ("renamed_exe.docx", "unknown"),
    ("renamed_zip.docx", "zip"),
    ("mixed.zip", "zip"),
    ("big30.zip", "zip"),
])
def test_fixture_is_detected_by_content(fixtures_dir: Path, fixture: str, expected: str) -> None:
    """Detection follows the bytes, not the name."""
    assert detect(fixtures_dir / fixture) == Detection(expected)


def test_pdf_header_is_pdf(tmp_path: Path) -> None:
    """%PDF- → pdf."""
    path = tmp_path / "x.docx"
    path.write_bytes(b"%PDF-1.7\n...")
    assert detect(path) == Detection("pdf")


def test_bomb_renamed_docx_is_unsafe_with_reason(fixtures_dir: Path, tmp_path: Path) -> None:
    """A .docx is a zip, so the archive guards apply (R5.5): bomb.zip renamed .docx → unsafe, ratio reason."""
    renamed = shutil.copy(fixtures_dir / "bomb.zip", tmp_path / "report.docx")
    d = detect(Path(renamed))
    assert d.type == "unsafe" and "compression ratio" in (d.reason or "")


def test_truncated_zip_is_corrupt(fixtures_dir: Path, tmp_path: Path) -> None:
    """A zip cut short (signature intact, central directory gone) → corrupt, not an exception."""
    data = (fixtures_dir / "valid.docx").read_bytes()
    path = tmp_path / "truncated.docx"
    path.write_bytes(data[: len(data) // 2])
    d = detect(path)
    assert d.type == "corrupt" and d.reason


# --- never raises for bad content ---

@pytest.mark.parametrize(("fixture", "reason"), [
    ("slip.zip", "unsafe path"),
    ("encrypted.zip", "encrypted"),
    ("dupnames.zip", "more than once"),
])
def test_guarded_content_is_unsafe(fixtures_dir: Path, fixture: str, reason: str) -> None:
    """Every archive guard, tripped inside detection, gives unsafe with its reason."""
    d = detect(fixtures_dir / fixture)
    assert d.type == "unsafe" and reason in (d.reason or "")


def test_corrupt_deflate_stream_is_corrupt(tmp_path: Path) -> None:
    """[Content_Types].xml whose compressed bytes are damaged → corrupt (zlib.error or bad CRC)."""
    path = write_zip(tmp_path / "bad.docx", [("[Content_Types].xml", b"<Types>" + b"x" * 5000 + b"</Types>"),
                                             ("word/document.xml", b"<doc/>")])
    data = bytearray(path.read_bytes())
    start = 30 + len("[Content_Types].xml")                  # first member's data follows its local header
    for i in range(start + 2, start + 40):
        data[i] ^= 0xFF
    path.write_bytes(bytes(data))
    assert detect(path).type == "corrupt"


def test_unsupported_compression_method_is_corrupt(tmp_path: Path) -> None:
    """A member with an unknown compression method → corrupt, not NotImplementedError."""
    path = write_zip(tmp_path / "odd.docx", [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<d/>")])
    data = bytearray(path.read_bytes())
    struct.pack_into("<H", data, 8, 99)                       # local header of the first member
    central = data.find(b"PK\x01\x02")
    struct.pack_into("<H", data, central + 10, 99)            # its central directory record
    path.write_bytes(bytes(data))
    assert detect(path).type == "corrupt"


@pytest.mark.parametrize("content", [b"", b"PK", b"hello world", b"\x00" * 100])
def test_short_or_foreign_bytes_are_unknown(tmp_path: Path, content: bytes) -> None:
    """Empty, too short or non-signature bytes → unknown."""
    path = tmp_path / "x.docx"
    path.write_bytes(content)
    assert detect(path) == Detection("unknown")


def test_zip_with_docx_names_but_no_wordml_content_type_is_zip(tmp_path: Path) -> None:
    """Both docx part names but a different content type → zip, not docx."""
    path = write_zip(tmp_path / "fake.docx", [("[Content_Types].xml", b"<Types>text/plain</Types>"),
                                              ("word/document.xml", b"<doc/>")])
    assert detect(path) == Detection("zip")


def test_limits_are_an_argument(fixtures_dir: Path) -> None:
    """The same file is unsafe under tighter limits — the engine uses the Limits it is given."""
    tight = POC_LIMITS.__class__(max_entries=29, max_uncompressed_bytes=POC_LIMITS.max_uncompressed_bytes,
                                 max_compression_ratio=200, allowed_entry_ext=frozenset({"docx"}))
    assert detect(fixtures_dir / "big30.zip") == Detection("zip")
    assert detect_file_type(fixtures_dir / "big30.zip", tight).type == "unsafe"


# --- mismatch_reason ---

@pytest.mark.parametrize(("declared", "detection", "expected"), [
    ("docx", Detection("unknown"), "File content does not match .docx — it appears to be unknown"),
    ("docx", Detection("zip"), "File content does not match .docx — it appears to be zip"),
    ("zip", Detection("docx"), "File content does not match .zip — it appears to be docx"),
    ("docx", Detection("unsafe", "'a' is encrypted"), "File is unsafe: 'a' is encrypted"),
    ("docx", Detection("corrupt", "Bad CRC-32"), "File is damaged and cannot be read"),
])
def test_mismatch_reason(declared: str, detection: Detection, expected: str) -> None:
    """Plain-language reasons; S3 checks that they name `unknown` and `zip`."""
    assert mismatch_reason(declared, detection) == expected


# --- engine purity ---

def test_engine_imports_no_framework_and_no_settings() -> None:
    """engine/ loads no FastAPI, Celery, SQLAlchemy, network client or app.* module (design.md §3)."""
    code = ("import sys, engine.file_checks; "
            "bad = {'fastapi', 'starlette', 'celery', 'kombu', 'sqlalchemy', 'httpx', 'boto3', 'redis', 'app', "
            "'pydantic_settings'}; print(sorted({m.split('.')[0] for m in sys.modules} & bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"



# --- rev 1.3: environment errors stay retryable; the content-types read is bounded ---

@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), MemoryError()], ids=["OSError", "MemoryError"])
def test_environment_errors_propagate_not_corrupt(fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                  error: BaseException) -> None:
    """A full disk or out-of-memory while reading is not a verdict on the file: it propagates, never corrupt."""
    class Broken:
        def __init__(self, *_: object, **__: object) -> None:
            raise error

    monkeypatch.setattr(file_checks.zipfile, "ZipFile", Broken)
    with pytest.raises(type(error)):
        detect(fixtures_dir / "valid.docx")


def test_content_types_read_is_bounded(tmp_path: Path) -> None:
    """A 50 MB [Content_Types].xml is not loaded: detection still says docx, with a small memory peak (NFR-4)."""
    body = f"<Types><Override ContentType=\"{WORDML}\"/>".encode() + b" " * (50 * 1024 * 1024) + b"</Types>"
    path = write_zip(tmp_path / "big_types.docx", [("[Content_Types].xml", body), ("word/document.xml", b"<d/>")])
    roomy = replace(POC_LIMITS, max_compression_ratio=10_000)   # allow the highly compressible padding
    tracemalloc.start()
    try:
        d = detect_file_type(path, roomy)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert d == Detection("docx")
    assert peak < 4 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MB"


def test_wordml_type_beyond_the_first_64_kb_is_not_docx(tmp_path: Path) -> None:
    """Documented consequence of the bounded read: a content type hidden after 64 KB reads as a plain zip."""
    body = b"<Types>" + b" " * (70 * 1024) + f"<Override ContentType=\"{WORDML}\"/></Types>".encode()
    path = write_zip(tmp_path / "late.docx", [("[Content_Types].xml", body), ("word/document.xml", b"<d/>")])
    assert detect_file_type(path, replace(POC_LIMITS, max_compression_ratio=10_000)) == Detection("zip")


# ============================================================ archive guards and extraction

def inspect(path: Path, limits=POC_LIMITS) -> list[zipfile.ZipInfo]:  # type: ignore[no-untyped-def]
    """inspect_archive on the zip at path."""
    with zipfile.ZipFile(path) as zf:
        return inspect_archive(zf, limits)


# --- inspect_archive: the Done-when cases ---

def test_mixed_zip_is_accepted_with_its_entries_in_order(fixtures_dir: Path) -> None:
    """mixed.zip passes every guard; entries come back in directory order (R7.5)."""
    assert [e.filename for e in inspect(fixtures_dir / "mixed.zip")] == [
        "doc1.docx", "doc2.docx", "doc3.docx", "notes.pdf", "readme.txt"]


@pytest.mark.parametrize(("fixture", "reason"), [
    ("bomb.zip", "implausible compression ratio"),    # R6.4
    ("slip.zip", "unsafe path"),                      # R6.5
    ("encrypted.zip", "is encrypted"),                # R6.6
    ("dupnames.zip", "appears more than once"),       # R6.9 (rev 1.3)
])
def test_unsafe_fixture_is_rejected(fixtures_dir: Path, fixture: str, reason: str) -> None:
    """Each unsafe fixture is rejected by its guard, with a message naming the problem."""
    with pytest.raises(Rejected, match=reason):
        inspect(fixtures_dir / fixture)


def test_too_many_entries_is_rejected(tmp_path: Path) -> None:
    """MAX_ZIP_ENTRIES + 1 files → rejected; exactly MAX_ZIP_ENTRIES is fine (R6.2)."""
    n = POC_LIMITS.max_entries
    at_limit = write_zip(tmp_path / "at.zip", [(f"f{i}.docx", b"x") for i in range(n)])
    over = write_zip(tmp_path / "over.zip", [(f"f{i}.docx", b"x") for i in range(n + 1)])
    assert len(inspect(at_limit)) == n
    with pytest.raises(Rejected, match=f"{n + 1} files; limit is {n}"):
        inspect(over)


def test_over_the_size_cap_is_rejected(fixtures_dir: Path) -> None:
    """Declared uncompressed total above MAX_ZIP_UNCOMPRESSED_BYTES → rejected; exactly at it is fine (R6.3)."""
    with zipfile.ZipFile(fixtures_dir / "mixed.zip") as zf:
        total = sum(e.file_size for e in zf.infolist())
    assert inspect(fixtures_dir / "mixed.zip", replace(POC_LIMITS, max_uncompressed_bytes=total))
    with pytest.raises(Rejected, match="too large once unpacked"):
        inspect(fixtures_dir / "mixed.zip", replace(POC_LIMITS, max_uncompressed_bytes=total - 1))


def test_bomb_is_under_the_size_cap_so_the_ratio_guard_is_what_fires(fixtures_dir: Path) -> None:
    """bomb.zip declares 200 MB (< 500 MB cap): it is the ratio guard that rejects it, not the size cap."""
    with pytest.raises(Rejected, match="compression ratio"):
        inspect(fixtures_dir / "bomb.zip")


# --- inspect_archive: edges ---

def test_directories_are_not_counted_or_returned(tmp_path: Path) -> None:
    """Directory entries are excluded from the count and from the result (design.md §4 entry_index)."""
    path = tmp_path / "dirs.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docs/", b"")
        zf.writestr("docs/a.docx", b"a")
        zf.writestr("docs/sub/", b"")
        zf.writestr("docs/sub/b.docx", b"b")
    assert [e.filename for e in inspect(path, replace(POC_LIMITS, max_entries=2))] == ["docs/a.docx", "docs/sub/b.docx"]


def test_empty_entry_does_not_divide_by_zero(tmp_path: Path) -> None:
    """A zero-byte stored entry (compress_size 0) passes the ratio guard."""
    path = write_zip(tmp_path / "empty.zip", [("empty.docx", b"")], compress=zipfile.ZIP_STORED)
    assert len(inspect(path)) == 1


def test_same_basename_in_different_folders_is_not_a_duplicate(tmp_path: Path) -> None:
    """R6.9 is about identical entry names; a/x.docx and b/x.docx are different entries."""
    path = write_zip(tmp_path / "two.zip", [("a/x.docx", b"1"), ("b/x.docx", b"2")])
    assert len(inspect(path)) == 2


def test_ratio_at_the_limit_is_accepted(tmp_path: Path) -> None:
    """The guard is strictly greater than MAX_COMPRESSION_RATIO."""
    path = write_zip(tmp_path / "zeros.zip", [("z.docx", bytes(1024 * 1024))])
    with zipfile.ZipFile(path) as zf:
        ratio = zf.infolist()[0].file_size / zf.infolist()[0].compress_size
    assert inspect(path, replace(POC_LIMITS, max_compression_ratio=ratio))
    with pytest.raises(Rejected):
        inspect(path, replace(POC_LIMITS, max_compression_ratio=ratio - 0.01))


# --- R6.10: name length, in UTF-8 bytes (rev 1.3) ---

def test_long_non_ascii_name_passes_a_character_count_but_fails_the_byte_limit(tmp_path: Path) -> None:
    """120 'Ü' = 120 characters but 240 bytes (+5 for '.docx' = 245): fine. 130 'Ü' = 265 bytes: rejected."""
    ok_name, long_name = "Ü" * 120 + ".docx", "Ü" * 130 + ".docx"
    assert len(long_name) == 135 < 255 < len(long_name.encode("utf-8")) == 265
    assert len(inspect(write_zip(tmp_path / "ok.zip", [(ok_name, b"x")]))) == 1
    with pytest.raises(Rejected, match="longer than 255 bytes"):
        inspect(write_zip(tmp_path / "long.zip", [(long_name, b"x")]))


def test_ascii_name_limit_is_exactly_255_bytes(tmp_path: Path) -> None:
    """A 255-byte ASCII name passes; 256 bytes is rejected."""
    assert len(inspect(write_zip(tmp_path / "a.zip", [("a" * 250 + ".docx", b"x")]))) == 1
    with pytest.raises(Rejected, match="longer than 255 bytes"):
        inspect(write_zip(tmp_path / "b.zip", [("a" * 251 + ".docx", b"x")]))


def test_long_folder_path_is_rejected_at_the_column_length(tmp_path: Path) -> None:
    """A short file name under a path over 1024 characters is rejected (source_entry_name is VARCHAR(1024))."""
    deep = "/".join(["folder"] * 150) + "/a.docx"
    assert len(deep) > 1024
    with pytest.raises(Rejected, match="path longer than 1024 characters"):
        inspect(write_zip(tmp_path / "deep.zip", [(deep, b"x")]))


def test_worst_case_staging_key_fits_s3(tmp_path: Path) -> None:
    """The longest allowed name (255 bytes, 4-byte characters) still yields a staging key under 1024 bytes."""
    name = "😀" * 62 + ".docx"                                  # 248 + 5 = 253 bytes
    prefix = f"ClinSync/staging/{'0' * 36}/{'0' * 36}/{'0' * 36}/9999_"
    assert len((prefix + name).encode("utf-8")) < 1024
    assert len(inspect(write_zip(tmp_path / "emoji.zip", [(name, b"x")]))) == 1


# --- assert_safe_path ---

@pytest.mark.parametrize("name", [
    "/abs", "/etc/passwd", "a/../b", "../evil.docx", "C:\\x", "c:x.docx", "..\\x", "a\\..\\b", "\\\\server\\share\\x",
])
def test_unsafe_path_is_rejected(name: str) -> None:
    """Absolute paths, '..' components (either slash) and drive letters are rejected (R6.5)."""
    with pytest.raises(Rejected, match="unsafe path"):
        assert_safe_path(name)


@pytest.mark.parametrize("name", ["a.docx", "docs/a.docx", "a..b.docx", "x/..y/z.docx", "./a.docx", "notes...docx"])
def test_safe_path_is_accepted(name: str) -> None:
    """Names that merely contain dots are fine."""
    assert_safe_path(name)


# --- extract_streaming (task 5.4) ---


class FakeZip:
    """A stand-in ZipFile whose member stream is scripted, to reach paths CPython's zipfile never takes."""

    def __init__(self, stream: io.RawIOBase | Any) -> None:
        """Serve `stream` as the member's contents."""
        self.stream = stream

    @contextmanager
    def open(self, _: zipfile.ZipInfo) -> Iterator[Any]:
        """Yield the scripted stream."""
        yield self.stream


class RecordingStream(io.BytesIO):
    """Records every read size; optionally raises after `fail_after` reads."""

    def __init__(self, data: bytes, fail_after: int | None = None, error: BaseException | None = None) -> None:
        """Serve data; raise `error` on read number fail_after + 1."""
        super().__init__(data)
        self.sizes: list[int] = []
        self.fail_after, self.error = fail_after, error

    def read(self, size: int | None = -1) -> bytes:
        """Record, maybe fail, then read."""
        self.sizes.append(size if size is not None else -1)
        if self.fail_after is not None and len(self.sizes) > self.fail_after and self.error is not None:
            raise self.error
        return super().read(size)


def entry(name: str, size: int) -> zipfile.ZipInfo:
    """A ZipInfo declaring `size` uncompressed bytes."""
    info = zipfile.ZipInfo(name)
    info.file_size = size
    return info


def test_extracts_an_entry_exactly(fixtures_dir: Path, tmp_path: Path) -> None:
    """A good entry comes out byte-for-byte; the caller owns (and deletes) the file."""
    with zipfile.ZipFile(fixtures_dir / "mixed.zip") as zf:
        info = zf.getinfo("doc1.docx")
        out = extract_streaming(zf, info, tmp_dir=tmp_path)
        assert out.read_bytes() == zf.read(info) and out.parent == tmp_path


def test_lying_index_is_rejected_with_crc_and_leaves_no_temp_file(fixtures_dir: Path, tmp_path: Path) -> None:
    """lying.zip: the truncated stream fails its CRC; BadZipFile becomes Rejected naming the CRC (R6.8, rev 1.1)."""
    with zipfile.ZipFile(fixtures_dir / "lying.zip") as zf:
        (info,) = zf.infolist()
        with pytest.raises(Rejected, match="CRC") as exc:
            extract_streaming(zf, info, tmp_dir=tmp_path)
    assert isinstance(exc.value.__cause__, zipfile.BadZipFile)
    assert list(tmp_path.iterdir()) == []


def test_reads_in_chunks_of_at_most_64_kb(tmp_path: Path) -> None:
    """Every read asks for at most 64 KB (R6.7)."""
    stream = RecordingStream(bytes(300 * 1024))
    extract_streaming(FakeZip(stream), entry("a.docx", 300 * 1024), tmp_dir=tmp_path)  # type: ignore[arg-type]
    assert CHUNK == 64 * 1024 and stream.sizes and all(0 < s <= CHUNK for s in stream.sizes)


def test_more_bytes_than_declared_is_rejected_and_cleaned_up(tmp_path: Path) -> None:
    """Defence in depth (never fires on CPython): a stream longer than file_size is rejected, temp file removed."""
    stream = RecordingStream(bytes(200 * 1024))
    with pytest.raises(Rejected, match="larger than its index claims"):
        extract_streaming(FakeZip(stream), entry("a.docx", 100 * 1024), tmp_dir=tmp_path)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [zipfile.BadZipFile("Bad CRC-32 for file 'a.docx'"),
                                   zlib.error("Error -3 while decompressing data"),
                                   EOFError("Compressed file ended before the end-of-stream marker")],
                         ids=["BadZipFile", "zlib.error", "EOFError"])
def test_unreadable_member_mid_stream_is_rejected_and_cleaned_up(tmp_path: Path, error: BaseException) -> None:
    """Any way the bytes disagree with the index, after partial output, → Rejected and no temp file."""
    stream = RecordingStream(bytes(300 * 1024), fail_after=2, error=error)
    with pytest.raises(Rejected, match="does not match its index"):
        extract_streaming(FakeZip(stream), entry("a.docx", 300 * 1024), tmp_dir=tmp_path)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), KeyboardInterrupt(), MemoryError()],
                         ids=["OSError", "KeyboardInterrupt", "MemoryError"])
def test_any_other_failure_propagates_unchanged_and_cleans_up(tmp_path: Path, error: BaseException) -> None:
    """Environment failures stay retryable: propagated as-is, never Rejected (rev 1.3); no temp file."""
    stream = RecordingStream(bytes(300 * 1024), fail_after=2, error=error)
    with pytest.raises(type(error)) as exc:
        extract_streaming(FakeZip(stream), entry("a.docx", 300 * 1024), tmp_dir=tmp_path)  # type: ignore[arg-type]
    assert not isinstance(exc.value, Rejected)
    assert list(tmp_path.iterdir()) == []


def test_bad_local_header_on_open_is_rejected_and_cleaned_up(fixtures_dir: Path, tmp_path: Path) -> None:
    """A local header that disagrees with the central directory (BadZipFile on open) → Rejected, no temp file."""
    data = bytearray((fixtures_dir / "slip.zip").read_bytes())
    name_at = 30                                              # the first local header's file name
    data[name_at] = ord("X")                                  # '../../evil.docx' → 'X./../evil.docx' locally
    path = tmp_path / "header.zip"
    path.write_bytes(bytes(data))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with zipfile.ZipFile(path) as zf:
        with pytest.raises(Rejected, match="does not match its index"):
            extract_streaming(zf, zf.infolist()[0], tmp_dir=out_dir)
    assert list(out_dir.iterdir()) == []


def test_200_mb_entry_extracts_in_under_64_mb_of_memory(fixtures_dir: Path, tmp_path: Path) -> None:
    """Peak traced memory while extracting bomb.zip's 200 MB entry stays under 64 MB (NFR-4)."""
    with zipfile.ZipFile(fixtures_dir / "bomb.zip") as zf:
        (info,) = zf.infolist()
        tracemalloc.start()
        try:
            out = extract_streaming(zf, info, tmp_dir=tmp_path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert out.stat().st_size == 200 * 1024 * 1024
    assert peak < 64 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MB"
    print(f"peak traced memory: {peak / 1024 / 1024:.2f} MB")


def test_extraction_hashes_the_entry_as_it_writes_it(fixtures_dir: Path, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """U5.2: the digest is fed each block while extracting; the entry is opened exactly once."""
    with zipfile.ZipFile(fixtures_dir / "mixed.zip") as zf:
        info = zf.getinfo("doc1.docx")
        expected = hashlib.sha256(zf.read(info)).hexdigest()
        opens: list[str] = []
        real_open = zf.open
        monkeypatch.setattr(zf, "open", lambda e, *a, **k: opens.append(e.filename) or real_open(e, *a, **k))
        digest = hashlib.sha256()
        out = extract_streaming(zf, info, tmp_dir=tmp_path, digest=digest)
    assert digest.hexdigest() == expected == hashlib.sha256(out.read_bytes()).hexdigest()
    assert opens == ["doc1.docx"]
