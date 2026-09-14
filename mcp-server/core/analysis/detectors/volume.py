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
    requires: frozenset[str] = frozenset({"spilled_bytes"})
    requires_any: tuple[frozenset[str], ...] = ()

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
                summary=(
                    f"This query ran out of memory and wrote {_bytes(spilled)} "
                    f"to disk to keep going, which is much slower than working "
                    f"in memory."
                ),
                rationale=(
                    "Starburst holds intermediate results in memory. When a query "
                    "needs more than it is allowed, it writes the overflow to disk "
                    "and carries on -- correct, but far slower. Either the query "
                    "is handling more data than it needs to, or the memory limit "
                    "is too low for this kind of work."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Try to reduce how much data the query holds at once: filter "
                    "rows earlier, select only the columns you need, or aggregate "
                    "before joining rather than after. If the query genuinely "
                    "needs this much memory, the limit is a cluster setting, so "
                    "please raise it with your cluster administrator."
                ),
                actual=_bytes(spilled),
                expected="no spill",
            )
        ]


class ScanAmplificationDetector:
    """Enormous scan for a tiny result -- the classic missing-pruning signature.

    Works from rows or bytes. Trino's own event logger records output volume
    in bytes and has no output_rows column at all, so requiring rows would
    silently disable this detector on the most common source there is.
    """

    id = "QRY-SCAN-001"
    requires: frozenset[str] = frozenset()
    requires_any: tuple[frozenset[str], ...] = (
        frozenset({"physical_input_bytes", "total_bytes_scanned"}),
        frozenset({"output_rows", "output_bytes"}),
    )

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        # Physical input is the better numerator when recorded: total_bytes
        # also counts data moved between stages, which is not a scan.
        scanned = query.physical_input_bytes or query.total_bytes_scanned
        if scanned is None:
            return []

        if query.output_rows is not None:
            if query.output_rows < thresholds.min_output_rows_for_amplification:
                return []
            ratio = scanned / max(query.output_rows, 1)
            limit = float(thresholds.scan_amplification_bytes_per_output_row)
            metric = "bytes_scanned_per_output_row"
            unit = f"{_bytes(int(ratio))} per output row"
            expected = (
                f"< {_bytes(thresholds.scan_amplification_bytes_per_output_row)} "
                "per output row"
            )
            returned = (
                f"{query.output_rows:,} row{'' if query.output_rows == 1 else 's'}"
            )
        elif query.output_bytes is not None:
            if query.output_bytes <= 0:
                return []
            ratio = scanned / query.output_bytes
            limit = float(thresholds.scan_amplification_bytes_per_output_byte)
            metric = "bytes_scanned_per_output_byte"
            unit = f"{ratio:,.0f}x more read than returned"
            expected = (
                f"< {thresholds.scan_amplification_bytes_per_output_byte:,}x "
                "read-to-returned ratio"
            )
            returned = _bytes(query.output_bytes)
        else:
            return []

        if ratio < limit:
            return []

        evidence = [
            QueryEvidence(stage_id="query", metric=metric, value=f"{ratio:.0f}")
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
                    f"This query read {_bytes(scanned)} of data to produce "
                    f"{returned}. That is far more than a result of this size "
                    f"should need."
                ),
                rationale=(
                    "Large tables are usually split into partitions -- typically "
                    "by date -- so a query that filters on the partition column "
                    "can skip the parts it does not need. Reading this much for "
                    "so small a result suggests it could not skip anything and "
                    "read the whole table instead."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Check the WHERE clause. Either there is no filter on the "
                    "column the table is partitioned by, or the filter wraps that "
                    "column in a function, which stops Starburst using it. "
                    "Comparing the column directly to a date range -- "
                    "event_date >= DATE '2026-01-01' rather than "
                    "year(event_date) = 2026 -- lets it skip the partitions it "
                    "does not need."
                ),
                actual=unit,
                expected=expected,
                deviation=ratio / limit - 1,
            )
        ]
