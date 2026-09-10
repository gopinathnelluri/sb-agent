"""What a detector is.

Each detector declares the ``QueryInfo`` fields it needs. The engine compares
that against what the query actually carries and either runs it or records a
skip -- so a thin audit row yields fewer findings with a stated reason, never
a wrong finding or a crash.

Thresholds arrive as an argument rather than being hardcoded, so a fleet can
tune them without a code change (SPEC.md: "Thresholds live in config").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.analysis.models import QueryInfo
from core.models import Finding


@dataclass(frozen=True)
class Thresholds:
    """Tunable limits. Defaults are starting points, not fleet truth."""

    queued_fraction: float = 0.5
    min_elapsed_ms_to_judge: int = 5_000
    spill_bytes: int = 1
    scan_amplification_bytes_per_output_row: int = 10_000_000
    scan_amplification_bytes_per_output_byte: int = 100_000
    min_output_rows_for_amplification: int = 1
    cpu_to_elapsed_ratio_low: float = 0.1

    @classmethod
    def from_mapping(cls, raw: dict[str, object] | None) -> Thresholds:
        """Build from config, ignoring nothing silently."""
        if not raw:
            return cls()
        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"Unknown threshold(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}."
            )
        base = cls()
        return cls(**{k: type(getattr(base, k))(v) for k, v in raw.items()})


class Detector(Protocol):
    """One finding type, computed from a normalised query.

    ``requires`` names fields that must all be present. ``requires_any`` names
    groups where any one member will do -- some sources record output volume
    in rows, others in bytes, and a detector that can work from either should
    not be skipped for want of one particular spelling.
    """

    id: str
    requires: frozenset[str]
    requires_any: tuple[frozenset[str], ...]

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        """Return zero or more findings. Never raises on missing optional data."""
        ...
