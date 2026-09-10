"""Joining runtime evidence to query text.

A runtime statistic says *what* happened; a text pattern says what looks
*suspicious*. Neither is conclusive alone -- a full scan is correct when you
genuinely want the whole table, and a function in a WHERE clause is harmless
on an unpartitioned one. When both point at the same cause, the diagnosis is
specific enough to act on, and that is the only case this module reports.

Correlations are declared as data. Adding one is an entry in the table below.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.analysis.models import QueryInfo
from core.analysis.sql.models import Confidence, SqlAnalysis, SqlPattern
from core.models import (
    Finding,
    Owner,
    QueryDomain,
    QueryEvidence,
    RationaleSource,
    Severity,
    compute_fingerprint,
)


@dataclass(frozen=True)
class Correlation:
    """A runtime finding and a text pattern that together explain a cause."""

    id: str
    runtime_rule_id: str
    pattern_kind: str
    min_confidence: Confidence
    severity: Severity
    domain: QueryDomain
    owner: Owner
    headline: str
    rationale: str
    next_step: str


CORRELATIONS: tuple[Correlation, ...] = (
    Correlation(
        id="QRY-ROOT-001",
        runtime_rule_id="QRY-SCAN-001",
        pattern_kind="function_on_filter_column",
        min_confidence=Confidence.CONFIRMED,
        severity=Severity.HIGH,
        domain=QueryDomain.DATA_ACCESS,
        owner=Owner.QUERY_AUTHOR,
        headline="Partition pruning was defeated by a function in the WHERE clause",
        rationale=(
            "Two independent signals agree. The runtime statistics show far more "
            "data was read than the result needed, and the query text wraps a "
            "partition column in a function -- which is exactly what stops the "
            "engine skipping partitions. This is the cause, not a guess."
        ),
        next_step=(
            "Compare the column to a date range instead of transforming it. A "
            "rewrite is included below and returns the same rows."
        ),
    ),
    Correlation(
        id="QRY-ROOT-002",
        runtime_rule_id="QRY-SPILL-001",
        pattern_kind="cross_join",
        min_confidence=Confidence.SUSPECTED,
        severity=Severity.HIGH,
        domain=QueryDomain.QUERY_SHAPE,
        owner=Owner.QUERY_AUTHOR,
        headline="Spilling is likely caused by a join with no condition",
        rationale=(
            "The query ran out of memory and spilled to disk, and it contains a "
            "join with no ON clause. An unconditioned join produces the product "
            "of its two inputs, which is the usual way a query outgrows memory."
        ),
        next_step=(
            "Add the join condition. If the cross join is deliberate, filter both "
            "sides down before joining them."
        ),
    ),
    Correlation(
        id="QRY-ROOT-003",
        runtime_rule_id="QRY-SCAN-001",
        pattern_kind="select_star",
        min_confidence=Confidence.SUSPECTED,
        severity=Severity.MEDIUM,
        domain=QueryDomain.DATA_ACCESS,
        owner=Owner.QUERY_AUTHOR,
        headline="Large scan may be inflated by SELECT *",
        rationale=(
            "The query read a lot of data relative to what it returned, and it "
            "selects every column. On a columnar format the engine reads only the "
            "columns you name, so naming them can cut the scan substantially."
        ),
        next_step=(
            "Replace SELECT * with the columns you actually use. This does not "
            "reduce the rows read, only the bytes per row -- if the row count is "
            "also wrong, fix the filter as well."
        ),
    ),
)


def correlate(
    query: QueryInfo, runtime: list[Finding], static: SqlAnalysis
) -> list[Finding]:
    """Emit a root-cause finding wherever runtime and text signals agree."""
    if not static.parsed:
        return []

    fired = {f.rule_id for f in runtime}
    by_kind: dict[str, list[SqlPattern]] = {}
    for pattern in static.patterns:
        by_kind.setdefault(pattern.kind, []).append(pattern)

    findings: list[Finding] = []
    for rule in CORRELATIONS:
        if rule.runtime_rule_id not in fired:
            continue
        candidates = [
            p
            for p in by_kind.get(rule.pattern_kind, [])
            if rule.min_confidence is Confidence.SUSPECTED
            or p.confidence is Confidence.CONFIRMED
        ]
        if not candidates:
            continue

        pattern = candidates[0]
        evidence = [
            QueryEvidence(
                stage_id="query",
                metric="sql_fragment",
                value=pattern.fragment,
            )
        ]
        findings.append(
            Finding(
                rule_id=rule.id,
                fingerprint=compute_fingerprint(
                    rule.id, query.cluster, query.query_id, evidence
                ),
                severity=rule.severity,
                domain=rule.domain,
                summary=f"{rule.headline}: {pattern.fragment}",
                rationale=f"{rule.rationale} {pattern.explanation.capitalize()}.",
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=rule.owner,
                next_step=rule.next_step,
                is_root_cause=True,
                actual=pattern.fragment,
                expected="a predicate the engine can push down to partition metadata",
            )
        )
    return findings
