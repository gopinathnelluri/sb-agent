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
        headline=(
            "Starburst had to read the whole table because of how the WHERE "
            "clause is written"
        ),
        rationale=(
            "This table is split into partitions by that column, which normally "
            "lets Starburst read only the parts your filter needs. Wrapping the "
            "column in a function hides its value until after the data is read, "
            "so every partition has to be scanned first. Two separate signals "
            "point to this -- how much data was actually read, and the shape of "
            "the WHERE clause -- so it is the cause rather than a guess."
        ),
        next_step=(
            "Compare the column directly to a date range instead of putting it "
            "inside a function. A rewritten version of your query is included "
            "below; it returns exactly the same rows."
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
        headline=(
            "This query ran out of memory, and a join with no condition is the "
            "likely cause"
        ),
        rationale=(
            "A join without an ON clause pairs every row on one side with every "
            "row on the other, so the result is the two row counts multiplied "
            "together. That grows very quickly, and it is the usual reason a "
            "query needs more memory than it is allowed."
        ),
        next_step=(
            "Add the missing join condition -- usually the columns that link the "
            "two tables. If you did mean to combine every row with every row, "
            "filter both sides down as much as possible first."
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
        headline="Selecting every column is making this query read more than it needs",
        rationale=(
            "These tables store each column separately, so Starburst reads only "
            "the columns a query names. Asking for all of them with SELECT * "
            "means reading every column, including any the query never uses."
        ),
        next_step=(
            "List the columns you actually use instead of SELECT *. Note this "
            "reduces how much is read per row, not how many rows are read -- so "
            "if the query is also scanning too many rows, the filter needs "
            "attention too."
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
                rationale=f"{rule.rationale} ({pattern.explanation}).",
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
