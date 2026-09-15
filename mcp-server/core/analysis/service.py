"""Use case 2: analysing one completed query, end to end.

Orchestration only. Fetch the query, run the runtime detectors, read the SQL,
correlate the two, and offer a rewrite where one is provably safe. Every
decision about what counts as a problem lives in ``detectors/`` or
``correlate.py``; every decision about where bytes come from lives in an
adapter.

The whole path is deterministic and does no planning work on the cluster --
one SELECT for the query, at most one cheap metadata probe per table. That is
what keeps it fast enough to sit in front of a user.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.analysis.compare import QueryComparison, compare
from core.analysis.correlate import correlate
from core.analysis.detectors.base import Thresholds
from core.analysis.engine import analyze as run_detectors
from core.analysis.models import QueryInfo
from core.analysis.sql.analyzer import analyze_sql, rewritten_sql
from core.analysis.sql.models import Rewrite, SqlPattern, TableFacts
from core.models import Coverage, Finding, sort_findings
from core.ports import QueryRepository


@dataclass(frozen=True)
class QueryAnalysis:
    """Everything we could determine about one query."""

    query_id: str
    cluster: str
    found: bool
    findings: list[Finding] = field(default_factory=list)
    sql_patterns: list[SqlPattern] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)
    suggested_sql: str | None = None
    query: QueryInfo | None = None
    coverage: Coverage | None = None
    detectors_run: list[str] = field(default_factory=list)
    not_found_reason: str | None = None
    history: QueryComparison | None = None
    previous_run_count: int = 0


class QueryAnalysisService:
    """Implements use case 2 over a query repository."""

    def __init__(
        self,
        repository: QueryRepository,
        thresholds: Thresholds | None = None,
    ) -> None:
        self._repository = repository
        self._thresholds = thresholds or Thresholds()

    def analyze_query(self, cluster: str, query_id: str) -> QueryAnalysis:
        """Analyse one completed query."""
        query = self._repository.get_query(cluster, query_id)
        if query is None:
            retention = self._repository.retention_days()
            window = (
                f" History covers roughly the last {retention} days."
                if retention
                else ""
            )
            return QueryAnalysis(
                query_id=query_id,
                cluster=cluster,
                found=False,
                not_found_reason=(
                    f"No record of query {query_id!r} on cluster {cluster!r}. It "
                    f"may have aged out of the audit history, or the id may be "
                    f"from a different cluster.{window}"
                ),
            )

        # Step 1: runtime statistics. Works with no SQL text at all.
        runtime = run_detectors(query, thresholds=self._thresholds)

        # Step 2: the query text, when the source recorded it.
        facts = self._table_facts(query)
        static = analyze_sql(query.sql, facts) if query.sql else None

        # Step 3: root causes, only where both signals agree.
        roots: list[Finding] = []
        if static is not None:
            roots = correlate(query, runtime.findings, static)

        blind_spots = list(runtime.coverage.blind_spots if runtime.coverage else [])
        if query.sql is None:
            blind_spots.append(
                "the audit record did not include the SQL text, so the query "
                "itself could not be examined"
            )
        elif static is not None and not static.parsed:
            blind_spots.append(
                f"the SQL text could not be parsed ({static.parse_error}), so "
                "no query-text analysis was possible"
            )

        findings = sort_findings(roots + runtime.findings)
        history, previous_count = self._history(cluster, query, findings)

        return QueryAnalysis(
            query_id=query_id,
            cluster=cluster,
            found=True,
            findings=findings,
            sql_patterns=list(static.patterns) if static else [],
            rewrites=list(static.rewrites) if static else [],
            suggested_sql=rewritten_sql(query.sql) if query.sql else None,
            query=query,
            detectors_run=list(runtime.detectors_run),
            coverage=Coverage.build(
                rules_evaluated=len(runtime.detectors_run),
                rules_skipped_missing_input=len(runtime.detectors_skipped),
                blind_spots=blind_spots,
            ),
            history=history,
            previous_run_count=previous_count,
        )

    def _history(
        self, cluster: str, query: QueryInfo, findings: list[Finding]
    ) -> tuple[QueryComparison | None, int]:
        """Compare this run against the last time the same SQL ran.

        Context is what turns a threshold into a measurement. "This read 4TB"
        invites the question "is that a lot?", and the answer is a judgement.
        "This read 4TB; last month the same query read 210GB" is not a
        judgement at all -- and it tells the user nothing is wrong with what
        they wrote, which is usually what they want to know first.

        Always best-effort. A query with no earlier runs is the normal case
        for ad-hoc work, and a lookup that fails must not cost the user their
        analysis.
        """
        try:
            earlier = self._repository.previous_runs(cluster, query, limit=5)
        except Exception:  # noqa: BLE001 - context is a bonus, not a dependency
            return None, 0
        if not earlier:
            return None, 0

        baseline = earlier[0]
        baseline_findings = run_detectors(
            baseline, thresholds=self._thresholds
        ).findings
        return (
            compare(baseline, query, baseline_findings, findings),
            len(earlier),
        )

    def compare_queries(
        self, cluster: str, query_id_a: str, query_id_b: str
    ) -> QueryComparison:
        """Compare two executions and say what changed between them.

        Each side is analysed exactly as it would be on its own, then the two
        results are diffed. That keeps one code path for "what is wrong with
        this query" and means a comparison can never disagree with the
        individual analyses a user might run alongside it.
        """
        missing = [
            qid
            for qid in (query_id_a, query_id_b)
            if self._repository.get_query(cluster, qid) is None
        ]
        if missing:
            retention = self._repository.retention_days()
            window = (
                f" History covers roughly the last {retention} days."
                if retention
                else ""
            )
            return QueryComparison(
                cluster=cluster,
                query_id_a=query_id_a,
                query_id_b=query_id_b,
                found=False,
                not_found_reason=(
                    f"No record of {', '.join(repr(q) for q in missing)} on "
                    f"cluster {cluster!r}. Both queries must be in the history "
                    f"to compare them.{window}"
                ),
            )

        first = self.analyze_query(cluster, query_id_a)
        second = self.analyze_query(cluster, query_id_b)
        assert first.query is not None
        assert second.query is not None
        return compare(first.query, second.query, first.findings, second.findings)

    def _table_facts(self, query: QueryInfo) -> dict[str, TableFacts]:
        """Look up partition columns for the tables the query touched.

        Facts are what turn a suspected pattern into a confirmed one. A lookup
        that fails returns empty facts rather than raising -- the analysis then
        hedges instead of asserting.
        """
        if not query.sql:
            return {}
        from core.analysis.sql.analyzer import analyze_sql as parse_only

        names = parse_only(query.sql).tables
        facts: dict[str, TableFacts] = {}
        for name in names:
            facts[name] = self._repository.table_facts(
                name, query.session_catalog, query.session_schema
            )
        return facts
