"""Types for static SQL analysis.

Everything here describes what was found in the query *text*. Nothing in this
module talks to a cluster or reads runtime statistics -- correlation with
those happens one layer up, so each signal can be trusted or doubted on its
own terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Confidence(StrEnum):
    """How sure we are that a text pattern is actually a problem.

    ``CONFIRMED`` means table facts back it up -- we know the wrapped column
    really is a partition column. ``SUSPECTED`` means the shape is suspicious
    but we could not verify it, usually because table metadata was
    unavailable. The distinction matters: a suspected finding should be
    phrased as a question to the user, not an accusation.
    """

    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"


class Equivalence(StrEnum):
    """Whether a rewrite provably returns the same rows.

    A rewrite that silently changes results is far worse than no rewrite at
    all, so anything we cannot prove is labelled and carries a caveat rather
    than being presented as a fix.
    """

    EQUIVALENT = "equivalent"
    SUGGESTED = "suggested"


@dataclass(frozen=True)
class TableFacts:
    """What we know about a table, from SHOW STATS / $partitions.

    Supplied by the caller when available. Absent facts downgrade findings to
    SUSPECTED rather than suppressing them.
    """

    name: str
    partition_columns: frozenset[str] = frozenset()
    row_count: int | None = None
    has_stats: bool | None = None


@dataclass(frozen=True)
class SqlPattern:
    """One anti-pattern found in the query text."""

    kind: str
    fragment: str
    explanation: str
    confidence: Confidence
    columns: tuple[str, ...] = ()
    table: str | None = None


@dataclass(frozen=True)
class Rewrite:
    """A proposed change to the query."""

    original_fragment: str
    rewritten_fragment: str
    equivalence: Equivalence
    explanation: str
    caveat: str | None = None


@dataclass(frozen=True)
class SqlAnalysis:
    """Result of reading the query text.

    ``parsed`` is false when the SQL could not be parsed at all -- an unusual
    dialect, a truncated statement from the audit log. Callers report that as
    a blind spot instead of concluding the query is clean.
    """

    parsed: bool
    patterns: list[SqlPattern] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)
    tables: tuple[str, ...] = ()
    parse_error: str | None = None
