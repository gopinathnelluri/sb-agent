"""Detectors that only need timing columns.

The most valuable detector in the set lives here. A large share of "this
query was slow" reports are queueing or a busy cluster, and an analyser that
always blames the SQL loses user trust fast (SPEC.md). It also needs nothing
but two columns every audit schema carries, so it works on the thinnest
possible source.
"""

from __future__ import annotations

from core.analysis.detectors.base import Thresholds
from core.analysis.models import QueryInfo
from core.models import (
    Finding,
    Owner,
    QueryEvidence,
    RationaleSource,
    Severity,
    compute_fingerprint,
)
from core.scopes import Scope


def _ms(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 1000:
        return f"{value}ms"
    if value < 60_000:
        return f"{value / 1000:.1f}s"
    return f"{value / 60_000:.1f}min"


class QueueDominatedDetector:
    """Queued time dominates elapsed -- the query waited, it did not run slowly."""

    id = "QRY-QUEUE-001"
    requires = frozenset({"elapsed_ms", "queued_ms"})

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        elapsed = query.elapsed_ms
        queued = query.queued_ms
        if elapsed is None or queued is None or elapsed <= 0:
            return []
        if elapsed < thresholds.min_elapsed_ms_to_judge:
            return []

        fraction = queued / elapsed
        if fraction < thresholds.queued_fraction:
            return []

        evidence = [
            QueryEvidence(
                stage_id="query",
                metric="queued_time_ms",
                value=str(queued),
            )
        ]
        running = elapsed - queued
        return [
            Finding(
                rule_id=self.id,
                fingerprint=compute_fingerprint(
                    self.id, query.cluster, query.query_id, evidence
                ),
                severity=Severity.HIGH,
                scope=Scope.RESOURCE_GROUPS,
                summary=(
                    f"Query spent {_ms(queued)} of {_ms(elapsed)} waiting in the "
                    f"queue ({fraction:.0%}); only {_ms(running)} was actual execution"
                ),
                rationale=(
                    "The query was not slow -- it was waiting for cluster capacity. "
                    "Rewriting the SQL will not help. Look at concurrency limits, "
                    "resource group configuration, or what else was running at the "
                    "time."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.PLATFORM_TEAM,
                next_step=(
                    "No change to your SQL will help this one. If it keeps "
                    "happening, ask your platform team whether the cluster is "
                    "under-provisioned or your queries are landing in a "
                    "resource group with a low concurrency limit."
                ),
                actual=f"{fraction:.0%} queued",
                expected=f"< {thresholds.queued_fraction:.0%} queued",
                deviation=fraction - thresholds.queued_fraction,
            )
        ]


class QueryFailedDetector:
    """The query did not finish. Report why before analysing performance."""

    id = "QRY-FAIL-001"
    requires: frozenset[str] = frozenset()

    def detect(self, query: QueryInfo, thresholds: Thresholds) -> list[Finding]:
        from core.analysis.models import QueryState

        if query.state is not QueryState.FAILED:
            return []

        evidence = [
            QueryEvidence(
                stage_id="query",
                metric="error_code",
                value=query.error_code or "unknown",
            )
        ]
        detail = f": {query.error_message}" if query.error_message else ""
        return [
            Finding(
                rule_id=self.id,
                fingerprint=compute_fingerprint(
                    self.id, query.cluster, query.query_id, evidence
                ),
                severity=Severity.HIGH,
                scope=Scope.RESOURCE_GROUPS,
                summary=f"Query failed with {query.error_code or 'an unknown error'}",
                rationale=(
                    "The query did not complete, so its runtime statistics describe "
                    "a partial execution. Resolve the failure before drawing "
                    f"performance conclusions{detail}"
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Read the error message above and fix the query, then re-run "
                    "it. Performance numbers from a failed run are not meaningful."
                ),
                actual=query.error_code or "failed",
                expected="finished",
            )
        ]
