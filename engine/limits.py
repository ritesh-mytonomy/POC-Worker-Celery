"""Archive and detection limits, passed into the engine as a value — the engine never reads settings (rev 1.2)."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Limits:
    """The R6 guards' thresholds and the allowed archive entry extensions."""

    max_entries: int
    max_uncompressed_bytes: int
    max_compression_ratio: float
    allowed_entry_ext: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Any) -> "Limits":
        """Build from any object with the design.md §9 attribute names (the app's Settings, in practice)."""
        return cls(
            max_entries=settings.MAX_ZIP_ENTRIES,
            max_uncompressed_bytes=settings.MAX_ZIP_UNCOMPRESSED_BYTES,
            max_compression_ratio=settings.MAX_COMPRESSION_RATIO,
            allowed_entry_ext=frozenset(settings.ALLOWED_ZIP_ENTRY_EXT),
        )
