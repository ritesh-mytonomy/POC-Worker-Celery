"""What a file's bytes are, regardless of its name (design.md §8.4; R5)."""
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from engine.archive import inspect_archive
from engine.errors import Rejected
from engine.limits import Limits

ZIP = b"PK\x03\x04"
PDF = b"%PDF-"
WORDML = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

# Ways zipfile reports unreadable content, beyond BadZipFile: a broken deflate stream, a truncated member,
# an unsupported compression method, a zip64 record it cannot handle.
_UNREADABLE = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, NotImplementedError)


@dataclass(frozen=True)
class Detection:
    """What the bytes are: docx | zip | pdf | unknown | unsafe | corrupt, with a reason for the last two."""

    type: str
    reason: str | None = None


def detect_file_type(path: Path, limits: Limits) -> Detection:
    """Return what the file's bytes are, regardless of its name. Never raises for bad content."""
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
                if WORDML in zf.read("[Content_Types].xml").decode("utf-8", "ignore"):
                    return Detection("docx")
            return Detection("zip")
    except Rejected as r:
        return Detection("unsafe", str(r))
    except _UNREADABLE as exc:
        return Detection("corrupt", str(exc) or type(exc).__name__)


def mismatch_reason(declared: str, d: Detection) -> str:
    """Plain-language reason a file's content is not what its name claims."""
    if d.type == "unsafe":
        return f"File is unsafe: {d.reason}"
    if d.type == "corrupt":
        return "File is damaged and cannot be read"
    return f"File content does not match .{declared} — it appears to be {d.type}"
