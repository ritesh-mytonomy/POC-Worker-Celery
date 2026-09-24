"""Tests for engine.file_signature (design.md §8.4; R5.1, R5.2, R5.5). No services, no settings."""
import shutil
import struct
import subprocess
import sys
import tracemalloc
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from engine import file_signature
from engine.file_signature import WORDML, Detection, detect_file_type, mismatch_reason
from tests.engine_helpers import POC_LIMITS, write_zip


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
    code = ("import sys, engine.file_signature, engine.archive, engine.limits, engine.errors; "
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

    monkeypatch.setattr(file_signature.zipfile, "ZipFile", Broken)
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
