"""Archive guards (R6): inspect the central directory before extracting anything (design.md §8.5)."""
import os
import re
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from engine.errors import Rejected
from engine.limits import Limits

CHUNK = 64 * 1024                        # R6.7 — at most 64 KB per read

# How zipfile reports content that does not match its index, beyond BadZipFile: a broken deflate stream,
# a truncated member, an unsupported compression method, a zip64 record it cannot handle. FORMAT errors only:
# OSError and MemoryError are environment problems (a full disk) and must stay retryable, never Rejected.
UNREADABLE = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, NotImplementedError)


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
    return entries


def assert_safe_path(name: str) -> None:
    """Raise Rejected for an absolute path, a '..' component or a drive letter (R6.5)."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
        raise Rejected(f"'{name}' has an unsafe path")


def tmp_path(tmp_dir: Path | None = None) -> Path:
    """Create an empty temporary file for one extracted entry and return its path."""
    fd, name = tempfile.mkstemp(prefix="clinsync-entry-", dir=tmp_dir)
    os.close(fd)
    return Path(name)


def extract_streaming(zf: zipfile.ZipFile, e: zipfile.ZipInfo, chunk: int = CHUNK,
                      tmp_dir: Path | None = None) -> Path:
    """Extract one entry; an entry whose bytes disagree with its index is rejected (R6.7, R6.8).

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
    except UNREADABLE as exc:                    # R6.8 — how a lying index actually surfaces (Bad CRC-32)
        out.unlink(missing_ok=True)
        raise Rejected(f"'{e.filename}' does not match its index ({exc or type(exc).__name__})") from exc
    except BaseException:
        out.unlink(missing_ok=True)              # never leave a partial temp file
        raise
    return out
