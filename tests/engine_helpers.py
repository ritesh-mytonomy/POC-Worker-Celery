"""Helpers for the engine tests: POC limits and small zip builders. No services, no settings."""
import io
import zipfile
from pathlib import Path

from engine.limits import Limits

POC_LIMITS = Limits(max_entries=500, max_uncompressed_bytes=500 * 1024 * 1024, max_compression_ratio=200,
                    allowed_entry_ext=frozenset({"docx"}))


def write_zip(path: Path, members: list[tuple[str, bytes]], compress: int = zipfile.ZIP_DEFLATED) -> Path:
    """Write a zip of (name, data) members to path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as zf:
        for name, data in members:
            zf.writestr(name, data)
    path.write_bytes(buf.getvalue())
    return path
