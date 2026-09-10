"""Detectors that need volume columns (bytes, rows, spill)."""

from __future__ import annotations

from core.analysis.detectors.base import Thresholds
from core.analysis.models import QueryInfo
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
        return "unknown"
    size = float(value)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:.1f}".rstrip("0").rstrip(".") + unit
        size /= 1024
    raise AssertionError("unreachable")


class SpillDetector:
    """The query spilled to disk -- it did not fit in memory."""

    id = "QRY-SPILL-001"
    requires = frozenset({"spilled_bytes"})

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        spilled = query.spilled_bytes
        if spilled is None or spilled < thresholds.spill_bytes:
            return []

        evidence = [
            QueryEvidence(stage_id="query", metric="spilled_bytes", value=str(spilled))
        ]
        return [
            Finding(
                rule_id=self.id,
                fingerprint=compute_fingerprint(
                    self.id, query.cluster, query.query_id, evidence
                ),
                severity=Severity.MEDIUM,
                domain=QueryDomain.MEMORY,
                summary=f"Query spilled {_bytes(spilled)} to disk",
                rationale=(
                    "Spilling means the query exceeded available memory and fell "
                    "back to disk, which is far slower. Either the query processes "
                    "more data than it needs to, or the per-node memory limits are "
                    "too low for this workload."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Try reducing how much data the query holds in memory at "
                    "once: filter earlier, select fewer columns, or aggregate "
                    "before joining rather than after. If the query genuinely "
                    "needs this much memory, raise it with your platform team."
                ),
                actual=_bytes(spilled),
                expected="no spill",
            )
        ]


class ScanAmplificationDetector:
    """Enormous scan for a tiny result -- the classic missing-pruning signature."""

    id = "QRY-SCAN-001"
    requires = frozenset({"total_bytes_scanned", "output_rows"})

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        scanned = query.total_bytes_scanned
        output = query.output_rows
        if scanned is None or output is None:
            return []
        if output < thresholds.min_output_rows_for_amplification:
            return []

        per_row = scanned / max(output, 1)
        if per_row < thresholds.scan_amplification_bytes_per_output_row:
            return []

        evidence = [
            QueryEvidence(
                stage_id="query",
                metric="bytes_scanned_per_output_row",
                value=f"{per_row:.0f}",
            )
        ]
        return [
            Finding(
                rule_id=self.id,
                fingerprint=compute_fingerprint(
                    self.id, query.cluster, query.query_id, evidence
                ),
                severity=Severity.HIGH,
                domain=QueryDomain.DATA_ACCESS,
                summary=(
                    f"Scanned {_bytes(scanned)} to return {output:,} row(s) -- "
                    f"{_bytes(int(per_row))} read per row of output"
                ),
                rationale=(
                    "Reading far more data than the result needs usually means the "
                    "scan is not being pruned: a missing partition filter, a "
                    "predicate that wraps the partition column in a function, or "
                    "stale table statistics leading the optimizer to a poor plan."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Check the WHERE clause. The usual cause is no filter on the "
                    "table's partition column, or a filter that hides it inside a "
                    "function -- write date_col >= DATE '2026-01-01' rather than "
                    "year(date_col) = 2026, so the engine can skip partitions "
                    "instead of reading them all."
                ),
                actual=f"{_bytes(int(per_row))} per output row",
                expected=(
                    f"< {_bytes(thresholds.scan_amplification_bytes_per_output_row)} "
                    "per output row"
                ),
                deviation=per_row / thresholds.scan_amplification_bytes_per_output_row
                - 1,
            )
        ]
