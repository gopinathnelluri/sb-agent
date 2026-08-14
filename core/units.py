"""Normalisation of the value formats Trino and the JVM use.

Rules compare quantities, not strings: ``40GB`` and ``40960MB`` are the same
setting and must never produce different findings. Everything is normalised
to a base unit here (bytes, seconds) before any comparison happens.

Trino data sizes are base-1024 throughout, including ``kB``. JVM flags use
single-letter suffixes (``-Xmx80G``). Both are accepted.
"""

from __future__ import annotations

import re
from typing import Final

_DATA_SIZE_UNITS: Final[dict[str, int]] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
}

_DURATION_UNITS: Final[dict[str, float]] = {
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}

_QUANTITY = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


class UnitParseError(ValueError):
    """A value could not be read as the quantity a rule expected."""


def parse_data_size(text: str) -> int:
    """Return a data size in bytes.

    Accepts Trino form (``40GB``, ``512kB``) and JVM form (``80G``, ``81920m``).
    """
    number, unit = _split(text, "data size")
    factor = _DATA_SIZE_UNITS.get(unit)
    if factor is None:
        raise UnitParseError(
            f"Unknown data size unit {unit!r} in {text!r}. "
            f"Expected one of: B, kB, MB, GB, TB, PB."
        )
    return int(number * factor)


def parse_duration(text: str) -> float:
    """Return a duration in seconds. Accepts ``30s``, ``5m``, ``1h``, ``500ms``."""
    number, unit = _split(text, "duration")
    factor = _DURATION_UNITS.get(unit or "s")
    if factor is None:
        raise UnitParseError(
            f"Unknown duration unit {unit!r} in {text!r}. "
            f"Expected one of: ns, us, ms, s, m, h, d."
        )
    return number * factor


def parse_number(text: str) -> float:
    """Return a bare numeric value, rejecting anything with a unit suffix."""
    number, unit = _split(text, "number")
    if unit:
        raise UnitParseError(f"Expected a plain number, got {text!r}.")
    return number


def parse_boolean(text: str) -> bool:
    """Return a boolean from Trino's accepted spellings."""
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "on", "1"}:
        return True
    if lowered in {"false", "no", "off", "0"}:
        return False
    raise UnitParseError(f"Expected a boolean, got {text!r}.")


def format_data_size(num_bytes: float) -> str:
    """Render bytes back into the largest unit that stays readable.

    Used for the ``expected`` text on a finding, so a threshold reads as
    ``24GB`` rather than ``25769803776``.
    """
    value = float(num_bytes)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            rendered = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{rendered}{unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _split(text: str, what: str) -> tuple[float, str]:
    match = _QUANTITY.match(text)
    if not match:
        raise UnitParseError(f"Cannot read {text!r} as a {what}.")
    return float(match.group(1)), match.group(2).lower()
