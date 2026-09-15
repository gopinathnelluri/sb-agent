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
    QueryDomain,
    QueryEvidence,
    RationaleSource,
    Severity,
    compute_fingerprint,
)


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
    requires: frozenset[str] = frozenset({"elapsed_ms", "queued_ms"})
    requires_any: tuple[frozenset[str], ...] = ()

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
                domain=QueryDomain.SCHEDULING,
                summary=(
                    f"This query waited {_ms(queued)} before it started running. "
                    f"The work itself took only {_ms(running)}, so almost all of "
                    f"the {_ms(elapsed)} you waited was queueing, not the query."
                ),
                rationale=(
                    "A query queues when the cluster has no free capacity to start "
                    "it, usually because other queries are already using what "
                    "there is. The SQL itself performed normally once it got "
                    "going, so changing it would not shorten the wait."
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.CLUSTER_OWNER,
                next_step=(
                    "There is nothing to fix in your SQL. If this keeps "
                    "happening, it is worth raising with your cluster owner: the "
                    "cluster may need more capacity for this workload, or your "
                    "queries may be running under a resource group that limits "
                    "how many can run at once."
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
    requires_any: tuple[frozenset[str], ...] = ()

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
                domain=QueryDomain.EXECUTION,
                summary=(
                    f"This query did not finish. It failed with "
                    f"{query.error_code or 'an error the history did not record'}."
                ),
                rationale=(
                    "Because the query stopped early, its timing and data-volume "
                    "figures describe only the part that ran. They are not a fair "
                    "picture of how it would perform if it completed"
                    f"{detail}"
                ),
                rationale_source=RationaleSource.RULE_CATALOG,
                evidence=list(evidence),
                subject=query.query_id,
                owner=Owner.QUERY_AUTHOR,
                next_step=(
                    "Fix the cause of the failure and run the query again. It is "
                    "worth doing that before looking at performance, since the "
                    "numbers from a run that stopped early are not comparable."
                ),
                actual=query.error_code or "failed",
                expected="finished",
            )
        ]
