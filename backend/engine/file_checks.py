"""File checks (design.md §8.4, §8.5; R5, R6): pure functions, no framework, no network, no settings.

Sections, in order: limits · Rejected · detection · archive checks · extraction. Limits are passed in as a value,
so every rule is unit-testable with a file on disk.
"""
import os
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

# ============================================================================ limits


@dataclass(frozen=True)
class Limits:
    """The R6 guards' thresholds and the allowed archive entry extensions."""

    max_entries: int
    max_uncompressed_bytes: int
    max_compression_ratio: float
    allowed_entry_ext: frozenset[str]
    max_folder_depth: int = 1            # upload-ingest-merge U5.4: at most one subfolder inside an archive

    @classmethod
    def from_settings(cls, settings: Any) -> "Limits":
        """Build from any object with the design.md §9 attribute names (the app's Settings, in practice)."""
        return cls(
            max_entries=settings.MAX_ZIP_ENTRIES,
            max_uncompressed_bytes=settings.MAX_ZIP_UNCOMPRESSED_BYTES,
            max_compression_ratio=settings.MAX_COMPRESSION_RATIO,
            allowed_entry_ext=frozenset(settings.ALLOWED_ZIP_ENTRY_EXT),
            max_folder_depth=settings.MAX_ZIP_FOLDER_DEPTH,
        )


# ============================================================================ Rejected


class Rejected(Exception):
    """The content is unacceptable; retrying cannot help (R11.2)."""


# How zipfile reports content that does not match its index, beyond BadZipFile: a broken deflate stream,
# a truncated member, an unsupported compression method, a zip64 record it cannot handle. FORMAT errors only:
# OSError and MemoryError are environment problems (a full disk) and must stay retryable, never Rejected.
UNREADABLE = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, NotImplementedError)


# ============================================================================ detection

ZIP = b"PK\x03\x04"
PDF = b"%PDF-"
WORDML = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CONTENT_TYPES_READ = 64 * 1024           # the WordML type sits near the start; never load the whole part (NFR-4)


@dataclass(frozen=True)
class Detection:
    """What the bytes are: docx | zip | pdf | unknown | unsafe | corrupt, with a reason for the last two."""

    type: str
    reason: str | None = None


def detect_file_type(path: Path, limits: Limits) -> Detection:
    """Return what the file's bytes are, regardless of its name. Never raises for bad content.

    Only format errors (UNREADABLE) mean corrupt. Environment errors such as OSError or MemoryError propagate,
    so the caller retries them instead of rejecting a good file.
    """
    with path.open("rb") as f:
        head = f.read(8)
    if head.startswith(PDF):
        return Detection("pdf")
    if not head.startswith(ZIP):
        return Detection("unknown")
    try:
        with zipfile.ZipFile(path) as zf:
            inspect_archive(zf, limits)              # R5.5 — a .docx is a zip; guard it too
            names = set(zf.namelist())
            if {"[Content_Types].xml", "word/document.xml"} <= names:
                with zf.open("[Content_Types].xml") as part:
                    head = part.read(CONTENT_TYPES_READ)
                if WORDML in head.decode("utf-8", "ignore"):
                    return Detection("docx")
            return Detection("zip")
    except Rejected as r:
        return Detection("unsafe", str(r))
    except UNREADABLE as exc:
        return Detection("corrupt", str(exc) or type(exc).__name__)


def mismatch_reason(declared: str, d: Detection) -> str:
    """Plain-language reason a file's content is not what its name claims."""
    if d.type == "unsafe":
        return f"File is unsafe: {d.reason}"
    if d.type == "corrupt":
        return "File is damaged and cannot be read"
    return f"File content does not match .{declared} — it appears to be {d.type}"


# ============================================================================ archive checks

# R6.10 (rev 1.3). Bytes, not characters: S3 keys are limited to 1024 UTF-8 bytes and non-ASCII characters take
# 2–4 bytes. A 255-byte file name keeps the staging key (~133 bytes of prefix) far inside that; the full entry
# name must also fit staged_document.source_entry_name, VARCHAR(1024).
MAX_NAME_BYTES = 255
MAX_ENTRY_NAME_CHARS = 1024


def inspect_archive(zf: zipfile.ZipFile, limits: Limits) -> list[zipfile.ZipInfo]:
    """Refuse an archive whose central directory describes something unsafe; return its file entries in order."""
    entries = [e for e in zf.infolist() if not e.is_dir()]
    if len(entries) > limits.max_entries:
        raise Rejected(f"{len(entries)} files; limit is {limits.max_entries}")                   # R6.2
    if sum(e.file_size for e in entries) > limits.max_uncompressed_bytes:
        raise Rejected("Archive is too large once unpacked")                                     # R6.3
    seen: set[str] = set()
    for e in entries:
        if e.compress_size and e.file_size / e.compress_size > limits.max_compression_ratio:
            raise Rejected(f"'{e.filename}' has an implausible compression ratio")              # R6.4
        assert_safe_path(e.filename)                                                             # R6.5
        if e.flag_bits & 0x1:
            raise Rejected(f"'{e.filename}' is encrypted")                                       # R6.6
        if e.filename in seen:
            raise Rejected(f"'{e.filename}' appears more than once")                             # R6.9 (rev 1.3)
        seen.add(e.filename)
        if len(PurePosixPath(e.filename).name.encode("utf-8")) > MAX_NAME_BYTES:
            raise Rejected(f"'{e.filename}' has a file name longer than {MAX_NAME_BYTES} bytes")  # R6.10 (rev 1.3)
        if len(e.filename) > MAX_ENTRY_NAME_CHARS:
            raise Rejected(f"'{e.filename[:80]}…' has a path longer than {MAX_ENTRY_NAME_CHARS} characters")
    return entries


def candidate_entries(entries: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    """The entries that can become candidates: not under __MACOSX/, not a hidden (dot) file; order kept (U5.3).

    Call it only AFTER inspect_archive: the safety guards must see every entry, hidden ones included — a bomb
    named ".x" or placed under __MACOSX/ is still refused. Never used to judge a .docx, whose own parts include
    _rels/.rels. Positions are numbered over this list, so staging keys and resume stay deterministic.
    """
    return [e for e in entries
            if "__MACOSX" not in PurePosixPath(e.filename).parts[:-1]
            and not PurePosixPath(e.filename).name.startswith(".")]


def folder_depth(name: str) -> int:
    """How many folders deep an entry sits: "a.docx" is 0, "docs/a.docx" is 1 (U5.4)."""
    return len(PurePosixPath(name).parts) - 1


def assert_safe_path(name: str) -> None:
    """Raise Rejected for an absolute path, a '..' component or a drive letter (R6.5)."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
        raise Rejected(f"'{name}' has an unsafe path")


# ============================================================================ extraction

CHUNK = 64 * 1024                        # R6.7 — at most 64 KB per read


def tmp_path(tmp_dir: Path | None = None) -> Path:
    """Create an empty temporary file for one extracted entry and return its path."""
    fd, name = tempfile.mkstemp(prefix="clinsync-entry-", dir=tmp_dir)
    os.close(fd)
    return Path(name)


def extract_streaming(zf: zipfile.ZipFile, e: zipfile.ZipInfo, chunk: int = CHUNK,
                      tmp_dir: Path | None = None, digest: Any = None) -> Path:
    """Extract one entry; an entry whose bytes disagree with its index is rejected (R6.7, R6.8).

    `digest` (a hashlib object) is fed each block as it is written — the entry's hash with no second read (U5.2).
    Never leaves a partial temp file behind: every failure path deletes it.
    """
    out, total = tmp_path(tmp_dir), 0
    try:
        with zf.open(e) as src, out.open("wb") as dst:
            while block := src.read(chunk):
                total += len(block)
                if total > e.file_size:          # defence in depth — CPython already truncates
                    raise Rejected(f"'{e.filename}' is larger than its index claims")
                dst.write(block)
                if digest is not None:
                    digest.update(block)
    except UNREADABLE as exc:                    # R6.8 — how a lying index actually surfaces (Bad CRC-32)
        out.unlink(missing_ok=True)
        raise Rejected(f"'{e.filename}' does not match its index ({exc or type(exc).__name__})") from exc
    except BaseException:
        out.unlink(missing_ok=True)              # never leave a partial temp file
        raise
    return out
