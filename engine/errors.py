"""Engine outcomes: Rejected is deterministic (never retried), Transient may succeed on retry (design.md §8.1)."""


class Rejected(Exception):
    """The content is unacceptable; retrying cannot help (R11.2)."""


class Transient(Exception):
    """A temporary failure (S3 unavailable, Internal API 5xx, connection reset); retry (R11.1)."""
