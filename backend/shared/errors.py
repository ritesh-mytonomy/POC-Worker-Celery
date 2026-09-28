"""Errors shared by the API and workers. Workers import this module: no SQLAlchemy or FastAPI imports."""


class ClaimSuperseded(Exception):
    """A fenced write matched no row: the token is stale or the file is no longer processing (R4.4)."""

    def __init__(self, file_id: object) -> None:
        """Record which file the caller no longer owns."""
        super().__init__(f"claim superseded for file {file_id}")
        self.file_id = file_id


class InvalidInput(ValueError):
    """A request the schema would accept but that makes no sense; the caller's mistake, never retried (400)."""


class CandidateIdentityMismatch(ValueError):
    """A replayed candidate's entry_index, file_name or file_ext differs from the stored row."""
