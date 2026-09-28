"""The ONE place names are normalised (upload-ingest-merge U6): titles and file names, for duplicate checks.

schema.sql: documents.file_name_norm is "casefolded, trimmed"; title_norm is "lower-cased, punctuation-stripped".
Both come from normalize(); check-duplicate, initiate, candidate upsert and commit all call these functions.
"""
import unicodedata
from pathlib import PurePosixPath


def normalize(text: str, *, strip_punctuation: bool) -> str:
    """NFKC, casefold, (titles only) every punctuation character to a space, collapse whitespace, trim."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    if strip_punctuation:
        folded = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in folded)
    return " ".join(folded.split())


def file_name_norm(file_name: str) -> str:
    """documents.file_name_norm: "  Report.DOCX " → "report.docx" (the name half of the name+size check)."""
    return normalize(file_name, strip_punctuation=False)


def title_of(file_name: str) -> str:
    """The proposed title: the file name without its extension — "Pre-op Guide.docx" → "Pre-op Guide"."""
    stem = PurePosixPath(file_name).stem.strip()
    return stem or file_name.strip()


def title_norm(title: str) -> str:
    """documents.title_norm and staged_document.title_norm: "Pre-op_Guide (v2)" → "pre op guide v2"."""
    return normalize(title, strip_punctuation=True)
