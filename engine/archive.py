"""Archive guards (R6): inspect the central directory before extracting anything (design.md §8.5)."""
import re
import zipfile
from pathlib import PurePosixPath

from engine.errors import Rejected
from engine.limits import Limits


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
