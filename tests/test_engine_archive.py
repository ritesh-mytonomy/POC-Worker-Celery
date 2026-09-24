"""Tests for engine.archive guards (design.md §8.5; R6.1–R6.6, R6.9) and extract_streaming (R6.7, R6.8)."""
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from engine.archive import assert_safe_path, inspect_archive
from engine.errors import Rejected
from tests.engine_helpers import POC_LIMITS, write_zip


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
