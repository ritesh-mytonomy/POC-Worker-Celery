"""fixtures/make_fixtures.py builds every design.md §10.1 fixture with the property it exists for (task 5.1)."""
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

EXPECTED = ["valid.docx", "renamed_exe.docx", "renamed_zip.docx", "mixed.zip", "big30.zip", "bomb.zip",
            "slip.zip", "encrypted.zip", "lying.zip", "dupnames.zip", "lying3.zip", "inner_bomb.zip", "big100.zip",
            "mac_deep.zip"]


def test_builds_every_fixture(fixtures_dir: Path) -> None:
    """All fourteen files exist (§10.1 rev 1.3 added dupnames, lying3, inner_bomb, big100; the merge mac_deep)."""
    assert sorted(p.name for p in fixtures_dir.iterdir()) == sorted(EXPECTED)


def test_cli_writes_to_given_directory(tmp_path: Path) -> None:
    """`python fixtures/make_fixtures.py OUT` builds into OUT."""
    script = Path(__file__).resolve().parents[1] / "fixtures" / "make_fixtures.py"
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, capture_output=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(EXPECTED)


def test_fixture_properties(fixtures_dir: Path) -> None:
    """Each fixture has the defect (or content) its tests rely on."""
    f = fixtures_dir
    assert f.joinpath("renamed_exe.docx").read_bytes()[:2] == b"MZ" and f.joinpath("renamed_exe.docx").stat().st_size == 4096
    with zipfile.ZipFile(f / "renamed_zip.docx") as zf:
        assert zf.namelist() == ["one.txt", "two.txt"]
    with zipfile.ZipFile(f / "mixed.zip") as zf:
        assert zf.namelist() == ["doc1.docx", "doc2.docx", "doc3.docx", "notes.pdf", "readme.txt"]
    with zipfile.ZipFile(f / "big30.zip") as zf:
        assert len(zf.namelist()) == 30
    with zipfile.ZipFile(f / "bomb.zip") as zf:
        (bomb,) = zf.infolist()
        assert bomb.file_size == 200 * 1024 * 1024 and bomb.file_size / bomb.compress_size > 200
    with zipfile.ZipFile(f / "slip.zip") as zf:
        assert zf.namelist() == ["../../evil.docx"]
    data = f.joinpath("encrypted.zip").read_bytes()
    assert struct.unpack_from("<H", data, 6)[0] & 0x1                                  # local header
    with zipfile.ZipFile(f / "encrypted.zip") as zf:
        assert zf.infolist()[0].flag_bits & 0x1                                         # central directory
    with zipfile.ZipFile(f / "lying.zip") as zf:
        (entry,) = zf.infolist()
        assert entry.file_size == 100 * 1024 < entry.compress_size                      # declared < real
    with zipfile.ZipFile(f / "dupnames.zip") as zf:
        assert zf.namelist() == ["a.docx", "a.docx"]
    with zipfile.ZipFile(f / "lying3.zip") as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["d0.docx", "d1.docx", "d2.docx"]
        assert zf.read("d0.docx") and zf.read("d1.docx")                               # the first two are honest
    with zipfile.ZipFile(f / "big100.zip") as zf:
        infos = zf.infolist()
        assert len(infos) == 100 and all(40 * 1024 <= i.file_size <= 64 * 1024 for i in infos)   # ~50 KB each
    with zipfile.ZipFile(f / "inner_bomb.zip") as zf:
        assert zf.namelist() == ["a.docx", "bomb.docx", "c.docx"]
        assert zf.getinfo("bomb.docx").compress_type == zipfile.ZIP_STORED
    with zipfile.ZipFile(f / "mac_deep.zip") as zf:
        assert zf.namelist() == ["a.docx", "__MACOSX/._a.docx", "docs/b.docx", ".DS_Store", "__MACOSX/docs/._b.docx",
                                 "docs/deep/c.docx"]
