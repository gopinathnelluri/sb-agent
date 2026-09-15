"""Detectors that need per-operator statistics.

These are the findings that say *where* a query went wrong rather than that it
did. A query-level total can tell you 21 minutes were spent; only the operator
records say which step spent them.

All of these read the `operator_summaries` payload, so all of them are skipped
-- and reported as skipped -- on a source that does not carry it.
"""

from __future__ import annotations

from core.analysis.detectors.base import Thresholds
from core.analysis.models import QueryInfo
from core.analysis.operators import (
    EXCHANGE_OPERATORS,
    JOIN_OPERATORS,
    OperatorSummary,
)
from core.models import (
    Finding,
    Owner,
    QueryDomain,
    QueryEvidence,
    RationaleSource,
    Severity,
    compute_fingerprint,
)


def _bytes(value: int | None) -> str:
    if value is None:
        return "an unknown amount"
    size = float(value)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:.1f}".rstrip("0").rstrip(".") + unit
        size /= 1024
    raise AssertionError("unreachable")


def _finding(
    *,
    rule_id: str,
    query: QueryInfo,
    operator: OperatorSummary,
    severity: Severity,
    domain: QueryDomain,
    summary: str,
    rationale: str,
    next_step: str,
    metric: str,
    value: str,
    actual: str,
    expected: str,
    deviation: float | None = None,
) -> Finding:
    evidence = [
        QueryEvidence(
            stage_id=operator.stage_id or "unknown",
            operator_id=operator.operator_id,
            metric=metric,
            value=value,
        )
    ]
    return Finding(
        rule_id=rule_id,
        fingerprint=compute_fingerprint(
            rule_id, query.cluster, f"{query.query_id}:{operator.label}", evidence
        ),
        severity=severity,
        domain=domain,
        summary=summary,
        rationale=rationale,
        rationale_source=RationaleSource.RULE_CATALOG,
        evidence=list(evidence),
        subject=operator.label,
        owner=Owner.QUERY_AUTHOR,
        next_step=next_step,
        actual=actual,
        expected=expected,
        deviation=deviation,
    )


class ExplodingJoinDetector:
    """A join producing far more rows than it consumed.

    The clearest signal in the whole set, and invisible from totals: a query
    can read a modest amount of data and still generate hundreds of millions
    of rows in the middle, which is where the time and memory go.
    """

    id = "QRY-JOIN-001"
    requires: frozenset[str] = frozenset({"operators"})
    requires_any: tuple[frozenset[str], ...] = ()

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        findings: list[Finding] = []
        for operator in query.operators:
            if not operator.is_a(*JOIN_OPERATORS):
                continue
            multiplier = operator.row_multiplier
            if multiplier is None or multiplier < thresholds.join_row_multiplier:
                continue
            assert operator.input_rows is not None
            assert operator.output_rows is not None

            findings.append(
                _finding(
                    rule_id=self.id,
                    query=query,
                    operator=operator,
                    severity=Severity.HIGH,
                    domain=QueryDomain.QUERY_SHAPE,
                    summary=(
                        f"A join is multiplying rows: it took in "
                        f"{operator.input_rows:,} rows and produced "
                        f"{operator.output_rows:,}, which is {multiplier:,.0f} "
                        f"times as many."
                    ),
                    rationale=(
                        "A join matches each row on one side against every row "
                        "on the other that shares its key. When the key is not "
                        "unique -- or the condition is missing or too loose -- "
                        "one row can match thousands, and the result grows far "
                        "beyond either input. That growth is usually where a "
                        "query's time and memory go."
                    ),
                    next_step=(
                        "Check the join condition and the keys behind it. Either "
                        "the condition is missing part of what makes a row "
                        "unique, or one side has duplicate keys you did not "
                        "expect -- worth counting them before changing the query."
                    ),
                    metric="row_multiplier",
                    value=f"{multiplier:.1f}",
                    actual=f"{multiplier:,.0f}x more rows out than in",
                    expected=(
                        f"under {thresholds.join_row_multiplier:,.0f}x row growth"
                    ),
                    deviation=multiplier / thresholds.join_row_multiplier - 1,
                )
            )
        return findings


class BroadcastSizeDetector:
    """A broadcast exchange carrying more data than broadcasting is meant for."""

    id = "QRY-BCAST-001"
    requires: frozenset[str] = frozenset({"operators"})
    requires_any: tuple[frozenset[str], ...] = ()

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        findings: list[Finding] = []
        for operator in query.operators:
            if not operator.is_a(*EXCHANGE_OPERATORS):
                continue
            if "broadcast" not in operator.operator_type.lower():
                continue
            size = operator.output_bytes or operator.input_bytes
            if size is None or size < thresholds.broadcast_bytes:
                continue

            findings.append(
                _finding(
                    rule_id=self.id,
                    query=query,
                    operator=operator,
                    severity=Severity.HIGH,
                    domain=QueryDomain.MEMORY,
                    summary=(
                        f"A broadcast join is sending {_bytes(size)} to every "
                        f"worker, which is more than broadcasting is meant for."
                    ),
                    rationale=(
                        "Broadcasting copies one side of a join to every worker "
                        "so each can join its own slice without moving data "
                        "around. That is fast when the copied side is small. "
                        "When it is not, every worker holds the whole thing at "
                        "once, which uses memory across the cluster and can push "
                        "the query into spilling."
                    ),
                    next_step=(
                        "The optimizer expected this side to be smaller than it "
                        "is, which usually means the table's statistics are out "
                        "of date. Ask your cluster owner to run ANALYZE on it. "
                        "If the table really is large, a partitioned join is the "
                        "better strategy."
                    ),
                    metric="broadcast_bytes",
                    value=str(size),
                    actual=_bytes(size),
                    expected=f"under {_bytes(thresholds.broadcast_bytes)}",
                    deviation=size / thresholds.broadcast_bytes - 1,
                )
            )
        return findings


class OperatorSpillDetector:
    """Which step ran out of memory, rather than merely that the query did."""

    id = "QRY-SPILL-002"
    requires: frozenset[str] = frozenset({"operators"})
    requires_any: tuple[frozenset[str], ...] = ()

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        spilling = [
            o
            for o in query.operators
            if o.spilled_bytes is not None and o.spilled_bytes >= thresholds.spill_bytes
        ]
        if not spilling:
            return []

        worst = max(spilling, key=lambda o: o.spilled_bytes or 0)
        total = sum(o.spilled_bytes or 0 for o in spilling)
        return [
            _finding(
                rule_id=self.id,
                query=query,
                operator=worst,
                severity=Severity.MEDIUM,
                domain=QueryDomain.MEMORY,
                summary=(
                    f"The step that ran out of memory is the "
                    f"{worst.operator_type} in stage {worst.stage_id or '?'}, "
                    f"which wrote {_bytes(worst.spilled_bytes)} to disk."
                ),
                rationale=(
                    "Knowing which step spilled narrows the fix considerably. "
                    "A join spilling points at the join's inputs; an aggregation "
                    "spilling points at how many distinct groups it is holding; "
                    "a sort spilling points at how much is being ordered."
                ),
                next_step=(
                    "Look at what feeds this step. Reducing the rows or columns "
                    "reaching it is usually more effective than raising the "
                    "memory limit, and it is something you can do yourself."
                ),
                metric="spilled_bytes",
                value=str(worst.spilled_bytes),
                actual=(
                    f"{_bytes(total)} across {len(spilling)} step(s)"
                    if len(spilling) > 1
                    else _bytes(worst.spilled_bytes)
                ),
                expected="no spill",
            )
        ]


class ScanSelectivityDetector:
    """A scan reading far more rows than it passes on.

    Filtering is expected -- that is what a WHERE clause does. This fires when
    the filter is doing so much work that pushing it down to the storage layer
    would have saved most of the read.
    """

    id = "QRY-SCAN-002"
    requires: frozenset[str] = frozenset({"operators"})
    requires_any: tuple[frozenset[str], ...] = ()

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        findings: list[Finding] = []
        for operator in query.operators:
            read = operator.physical_input_rows or operator.input_rows
            passed = operator.output_rows
            if read is None or passed is None or read <= 0:
                continue
            if read < thresholds.min_rows_for_selectivity:
                continue
            kept = passed / read
            if kept > thresholds.scan_selectivity:
                continue

            discarded = read - passed
            findings.append(
                _finding(
                    rule_id=self.id,
                    query=query,
                    operator=operator,
                    severity=Severity.MEDIUM,
                    domain=QueryDomain.DATA_ACCESS,
                    summary=(
                        f"A scan read {read:,} rows and kept only {passed:,} of "
                        f"them -- {discarded:,} rows were read and thrown away."
                    ),
                    rationale=(
                        "Rows discarded after reading still cost the time to "
                        "read them. When a filter removes almost everything, it "
                        "is usually one the storage layer could have applied "
                        "first, either by skipping partitions or by using the "
                        "file's own min/max information."
                    ),
                    next_step=(
                        "Check whether the filter is on a column the table is "
                        "partitioned or sorted by, and whether it is written in "
                        "a form Starburst can push down -- a direct comparison "
                        "rather than one wrapped in a function."
                    ),
                    metric="rows_kept_fraction",
                    value=f"{kept:.6f}",
                    actual=f"{kept:.2%} of rows read were kept",
                    expected=f"more than {thresholds.scan_selectivity:.1%} kept",
                )
            )
        return findings
