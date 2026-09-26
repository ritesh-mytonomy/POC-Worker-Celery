"""app/naming.py: the one definition of file_name_norm and title_norm (upload-ingest-merge U6)."""
import re
from pathlib import Path

import pytest

from app.naming import file_name_norm, normalize, title_norm, title_of

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.mark.parametrize(("name", "norm"), [
    ("Report.DOCX", "report.docx"),
    ("  report.docx  ", "report.docx"),                  # trimmed
    ("Pre-op  Guide.docx", "pre-op guide.docx"),         # whitespace collapsed, punctuation kept
    ("Straße.docx", "strasse.docx"),                     # casefold, not just lower
    ("ﬁle.docx", "file.docx"),                           # NFKC: the ligature is two letters
])
def test_file_name_norm_is_casefolded_and_trimmed(name: str, norm: str) -> None:
    """schema.sql: file_name_norm is "casefolded, trimmed" — punctuation stays, so extensions still count."""
    assert file_name_norm(name) == norm


@pytest.mark.parametrize(("title", "norm"), [
    ("Pre-op Guide", "pre op guide"),
    ("Pre-op_Guide (v2)", "pre op guide v2"),            # _ ( ) are punctuation too
    ("  CARDIOLOGY:  overview. ", "cardiology overview"),
    ("Überblick – Herz", "überblick herz"),              # the en dash is punctuation; accents kept
    ("Café", "café"),
])
def test_title_norm_is_lowercased_and_punctuation_stripped(title: str, norm: str) -> None:
    """schema.sql: title_norm is "lower-cased, punctuation-stripped"; the words stay in order."""
    assert title_norm(title) == norm


def test_the_composed_and_decomposed_forms_normalise_alike() -> None:
    """é typed as one character or as e + combining accent: the same title (NFKC)."""
    assert title_norm("Café") == title_norm("Café")


@pytest.mark.parametrize(("file_name", "title"), [
    ("Pre-op Guide.docx", "Pre-op Guide"), ("archive.tar.gz", "archive.tar"), (".docx", ".docx"),
    ("notes", "notes"), (" spaced .docx", "spaced"),
])
def test_title_of_is_the_file_name_without_its_extension(file_name: str, title: str) -> None:
    """The proposed title (metadata confirmation is out of scope)."""
    assert title_of(file_name) == title


def test_both_norms_come_from_one_function() -> None:
    """file_name_norm and title_norm differ only in the punctuation switch of normalize()."""
    assert file_name_norm("A-b.docx") == normalize("A-b.docx", strip_punctuation=False)
    assert title_norm("A-b") == normalize("A-b", strip_punctuation=True)


def test_no_other_module_normalises_names() -> None:
    """The rules live only in app/naming.py: no other app module casefolds or Unicode-normalises text."""
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py") if p.name != "naming.py"
                 and re.search(r"casefold\(|unicodedata|_norm\s*=\s*.*\.lower\(", p.read_text())]
    assert offenders == []
