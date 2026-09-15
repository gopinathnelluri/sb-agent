"""Comparing two query executions.

A single query analysed alone can only be judged against fixed thresholds --
is 4TB a lot? It depends entirely on the table. Two executions judge each
other, which is both more accurate and closer to the question people actually
ask: "it was fine yesterday, what changed?"

Two shapes of comparison matter, and the same code serves both:

* **Same SQL, two runs.** Something outside the query changed -- the data
  grew, the cluster got busier, statistics went stale. The SQL is a constant,
  so the difference is evidence about everything else.
* **Different SQL.** Usually someone rewrote a query and wants to know whether
  it helped. That is the loop this analyser exists to close: it suggests a
  rewrite, the user runs it, and this says whether the suggestion was right.

Telling the two apart matters, because the same numbers mean different things.
A query reading half as much after a rewrite is a success; the same drop with
unchanged SQL means the data changed underneath you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.analysis.models import QueryInfo
from core.models import Finding


class ChangeDirection(StrEnum):
    """Which way a metric moved, in terms of whether it is good news."""

    IMPROVED = "improved"
    WORSENED = "worsened"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class MetricChange:
    """One measurement, before and after.

    ``direction`` is about outcome, not arithmetic: less time and less data
    read are improvements, so a fall in either is ``IMPROVED``.
    """

    name: str
    label: str
    before: int | None
    after: int | None
    unit: str
    direction: ChangeDirection = ChangeDirection.UNCHANGED
    factor: float | None = None
    summary: str = ""

    @property
    def comparable(self) -> bool:
        return self.before is not None and self.after is not None


@dataclass(frozen=True)
class QueryComparison:
    """How two executions differ, and what that suggests."""

    cluster: str
    query_id_a: str
    query_id_b: str
    found: bool = True
    same_sql: bool = False
    verdict: str = ""
    changes: list[MetricChange] = field(default_factory=list)
    findings_only_in_a: list[Finding] = field(default_factory=list)
    findings_only_in_b: list[Finding] = field(default_factory=list)
    findings_in_both: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    not_found_reason: str | None = None


# Metrics worth comparing, and which direction is good news. Ordered so the
# most decisive appear first in a report.
_METRICS: tuple[tuple[str, str, str, bool], ...] = (
    ("elapsed_ms", "total time", "duration", True),
    ("queued_ms", "time spent queueing", "duration", True),
    ("physical_input_bytes", "data read from storage", "bytes", True),
    ("total_bytes_scanned", "data scanned", "bytes", True),
    ("spilled_bytes", "data spilled to disk", "bytes", True),
    ("peak_memory_bytes", "peak memory", "bytes", True),
    ("cpu_ms", "CPU time", "duration", True),
    ("total_rows", "rows processed", "count", True),
    ("output_rows", "rows returned", "count", False),
    ("output_bytes", "data returned", "bytes", False),
)

# Below this, a difference is noise rather than news.
MATERIAL_CHANGE = 0.10


def _fmt(value: int | None, unit: str) -> str:
    if value is None:
        return "not recorded"
    if unit == "duration":
        if value < 1000:
            return f"{value}ms"
        if value < 60_000:
            return f"{value / 1000:.1f}s"
        return f"{value / 60_000:.1f}min"
    if unit == "bytes":
        size = float(value)
        for suffix in ("B", "kB", "MB", "GB", "TB", "PB"):
            if abs(size) < 1024 or suffix == "PB":
                return f"{size:.1f}".rstrip("0").rstrip(".") + suffix
            size /= 1024
    return f"{value:,}"


def _describe(label: str, before: int, after: int, unit: str, factor: float) -> str:
    """A phrase a person would actually say about the change."""
    if factor >= 1:
        return (
            f"{label} went from {_fmt(before, unit)} to {_fmt(after, unit)} "
            f"-- {factor:.1f}x more"
        )
    return (
        f"{label} went from {_fmt(before, unit)} to {_fmt(after, unit)} "
        f"-- {1 / factor:.1f}x less"
    )


def _compare_metric(
    name: str, label: str, unit: str, lower_is_better: bool, a: QueryInfo, b: QueryInfo
) -> MetricChange:
    before = getattr(a, name)
    after = getattr(b, name)
    change = MetricChange(name=name, label=label, before=before, after=after, unit=unit)
    if before is None or after is None or before == 0:
        return change

    factor = after / before
    if abs(factor - 1) < MATERIAL_CHANGE:
        return MetricChange(
            name=name,
            label=label,
            before=before,
            after=after,
            unit=unit,
            direction=ChangeDirection.UNCHANGED,
            factor=factor,
            summary=f"{label} barely changed ({_fmt(after, unit)})",
        )

    got_smaller = factor < 1
    improved = got_smaller if lower_is_better else not got_smaller
    return MetricChange(
        name=name,
        label=label,
        before=before,
        after=after,
        unit=unit,
        direction=(ChangeDirection.IMPROVED if improved else ChangeDirection.WORSENED),
        factor=factor,
        summary=_describe(label, before, after, unit, factor),
    )


def _normalise(sql: str | None) -> str:
    """Collapse whitespace so formatting alone does not read as a change."""
    return " ".join((sql or "").split()).lower()


def compare(
    a: QueryInfo,
    b: QueryInfo,
    findings_a: list[Finding],
    findings_b: list[Finding],
) -> QueryComparison:
    """Compare two executions and say what changed.

    ``a`` is the earlier or baseline run, ``b`` the one being judged.
    """
    changes = [
        _compare_metric(name, label, unit, lower_is_better, a, b)
        for name, label, unit, lower_is_better in _METRICS
    ]
    material = [c for c in changes if c.direction is not ChangeDirection.UNCHANGED]

    same_sql = bool(a.sql and b.sql and _normalise(a.sql) == _normalise(b.sql))

    # Findings are matched on rule id: the same problem in both runs is not
    # news, one that appeared or went away is.
    ids_a = {f.rule_id: f for f in findings_a}
    ids_b = {f.rule_id: f for f in findings_b}
    only_a = [f for rid, f in ids_a.items() if rid not in ids_b]
    only_b = [f for rid, f in ids_b.items() if rid not in ids_a]
    both = [f for rid, f in ids_b.items() if rid in ids_a]

    notes: list[str] = []
    if same_sql:
        notes.append(
            "Both runs used the same SQL, so anything that changed is outside "
            "the query -- the data it reads, the cluster's load, or the "
            "statistics the optimizer works from."
        )
    elif a.sql and b.sql:
        notes.append(
            "The SQL differs between the two runs, so the comparison reflects "
            "the rewrite as well as any change in the data."
        )
    else:
        notes.append(
            "The history did not record SQL for at least one run, so whether "
            "the query itself changed could not be determined."
        )

    if only_b:
        notes.append(
            f"{len(only_b)} problem(s) present in the later run were absent "
            f"from the earlier one."
        )
    if only_a:
        notes.append(
            f"{len(only_a)} problem(s) from the earlier run are no longer present."
        )

    return QueryComparison(
        cluster=b.cluster,
        query_id_a=a.query_id,
        query_id_b=b.query_id,
        same_sql=same_sql,
        verdict=_verdict(a, b, material, same_sql),
        changes=[c for c in changes if c.comparable],
        findings_only_in_a=only_a,
        findings_only_in_b=only_b,
        findings_in_both=both,
        notes=notes,
    )


def _verdict(
    a: QueryInfo, b: QueryInfo, material: list[MetricChange], same_sql: bool
) -> str:
    """One sentence answering the question that prompted the comparison."""
    elapsed = next((c for c in material if c.name == "elapsed_ms"), None)
    if elapsed is None or elapsed.factor is None:
        if not material:
            return "The two runs performed about the same."
        return "The two runs differ, though not in total time."

    faster = elapsed.direction is ChangeDirection.IMPROVED
    ratio = (1 / elapsed.factor) if faster else elapsed.factor
    direction = "faster" if faster else "slower"
    before = _fmt(elapsed.before, "duration")
    after = _fmt(elapsed.after, "duration")

    lead = "The same query" if same_sql else "The second version"
    return f"{lead} was {ratio:.1f}x {direction} -- {before} then, {after} now."
