"""End-to-end tests for query analysis, and for the correlation layer.

Correlation is the piece worth testing hardest: it is what upgrades two weak
signals into a root cause, so it must stay silent when only one of them is
present.
"""

from __future__ import annotations

from core.analysis.correlate import correlate
from core.analysis.engine import analyze as run_detectors
from core.analysis.models import QueryInfo, QueryState
from core.analysis.service import QueryAnalysisService
from core.analysis.sql.analyzer import analyze_sql
from core.analysis.sql.models import TableFacts
from core.models import Owner
from tests.fakes import FakeQueryRepository

BAD_SQL = (
    "SELECT c.name, sum(o.amount) AS total FROM orders o "
    "JOIN customers c ON o.cust_id = c.id "
    "WHERE year(o.event_date) = 2026 GROUP BY c.name"
)
GOOD_SQL = "SELECT id FROM orders WHERE event_date >= DATE '2026-01-01' LIMIT 10"


def _slow_query(sql: str | None = BAD_SQL, **overrides: object) -> QueryInfo:
    base: dict[str, object] = {
        "query_id": "20260909_00042",
        "cluster": "prod",
        "source": "audit:test",
        "state": QueryState.FINISHED,
        "sql": sql,
        "session_catalog": "hive",
        "session_schema": "sales",
        "elapsed_ms": 725_000,
        "queued_ms": 41_000,
        "total_bytes_scanned": 4_400_000_000_000,
        "output_rows": 112,
    }
    base.update(overrides)
    return QueryInfo(**base)  # type: ignore[arg-type]


def _service(
    query: QueryInfo | None = None, partitioned: bool = True
) -> QueryAnalysisService:
    q = query or _slow_query()
    return QueryAnalysisService(
        FakeQueryRepository(
            queries={q.query_id: q},
            partitions={"orders": frozenset({"event_date"})} if partitioned else {},
        )
    )


class TestCorrelation:
    def test_root_cause_when_both_signals_agree(self) -> None:
        query = _slow_query()
        runtime = run_detectors(query).findings
        static = analyze_sql(
            BAD_SQL, {"orders": TableFacts("orders", frozenset({"event_date"}))}
        )
        roots = correlate(query, runtime, static)
        assert [f.rule_id for f in roots] == ["QRY-ROOT-001"]
        assert roots[0].owner is Owner.QUERY_AUTHOR

    def test_silent_when_only_the_runtime_signal_fires(self) -> None:
        """A big scan with clean SQL is not evidence of a pruning failure."""
        query = _slow_query(sql=GOOD_SQL)
        runtime = run_detectors(query).findings
        static = analyze_sql(GOOD_SQL)
        assert correlate(query, runtime, static) == []

    def test_silent_when_only_the_text_signal_fires(self) -> None:
        """Suspicious SQL that performed fine is not a finding."""
        query = _slow_query(total_bytes_scanned=1_000, output_rows=1_000)
        runtime = run_detectors(query).findings
        static = analyze_sql(
            BAD_SQL, {"orders": TableFacts("orders", frozenset({"event_date"}))}
        )
        assert correlate(query, runtime, static) == []

    def test_confirmed_evidence_required_for_the_pruning_root_cause(self) -> None:
        """Without partition facts the pattern is suspected, so no root cause."""
        query = _slow_query()
        runtime = run_detectors(query).findings
        static = analyze_sql(BAD_SQL)  # no table facts
        assert [f.rule_id for f in correlate(query, runtime, static)] == []

    def test_unparseable_sql_produces_no_root_cause(self) -> None:
        query = _slow_query(sql="*** not sql ***")
        runtime = run_detectors(query).findings
        static = analyze_sql("*** not sql ***")
        assert correlate(query, runtime, static) == []


class TestService:
    def test_analyses_a_known_query(self) -> None:
        result = _service().analyze_query("prod", "20260909_00042")
        assert result.found is True
        assert result.findings

    def test_root_cause_is_ranked_above_the_symptom(self) -> None:
        result = _service().analyze_query("prod", "20260909_00042")
        ranks = [f.severity.rank for f in result.findings]
        assert ranks == sorted(ranks)
        assert any(f.rule_id == "QRY-ROOT-001" for f in result.findings)

    def test_offers_an_equivalent_rewrite(self) -> None:
        result = _service().analyze_query("prod", "20260909_00042")
        assert result.suggested_sql is not None
        assert "2026-01-01" in result.suggested_sql
        assert "YEAR(" not in result.suggested_sql.upper()

    def test_looks_up_partition_facts_for_each_table(self) -> None:
        repo = FakeQueryRepository(
            queries={"20260909_00042": _slow_query()},
            partitions={"orders": frozenset({"event_date"})},
        )
        QueryAnalysisService(repo).analyze_query("prod", "20260909_00042")
        assert set(repo.facts_requested) == {"orders", "customers"}

    def test_unknown_query_explains_why_rather_than_erroring(self) -> None:
        result = _service().analyze_query("prod", "nope")
        assert result.found is False
        assert result.not_found_reason is not None
        assert "aged out" in result.not_found_reason
        assert "30 days" in result.not_found_reason

    def test_wrong_cluster_is_not_found(self) -> None:
        assert _service().analyze_query("other", "20260909_00042").found is False

    def test_query_without_sql_still_analyses_runtime(self) -> None:
        """A thin audit row must still produce what it can."""
        result = _service(_slow_query(sql=None)).analyze_query("prod", "20260909_00042")
        assert result.found is True
        assert result.findings
        assert result.sql_patterns == []
        assert result.coverage is not None
        assert any("did not include the SQL" in b for b in result.coverage.blind_spots)

    def test_unparseable_sql_is_a_blind_spot_not_a_crash(self) -> None:
        result = _service(_slow_query(sql="*** bad ***")).analyze_query(
            "prod", "20260909_00042"
        )
        assert result.found is True
        assert result.coverage is not None
        assert any("could not be parsed" in b for b in result.coverage.blind_spots)

    def test_healthy_query_yields_no_findings(self) -> None:
        healthy = _slow_query(
            sql=GOOD_SQL,
            elapsed_ms=8_000,
            queued_ms=200,
            total_bytes_scanned=50_000_000,
            output_rows=10_000,
        )
        result = _service(healthy).analyze_query("prod", "20260909_00042")
        assert result.findings == []

    def test_coverage_never_claims_completeness_on_a_thin_source(self) -> None:
        result = _service().analyze_query("prod", "20260909_00042")
        assert result.coverage is not None
        assert result.coverage.complete is False

    def test_deterministic_across_runs(self) -> None:
        first = _service().analyze_query("prod", "20260909_00042")
        second = _service().analyze_query("prod", "20260909_00042")
        assert [f.fingerprint for f in first.findings] == [
            f.fingerprint for f in second.findings
        ]


class TestOwnerSplit:
    def test_queueing_is_attributed_to_the_platform(self) -> None:
        """An analyst must be able to tell their problem from the platform's."""
        query = _slow_query(elapsed_ms=700_000, queued_ms=600_000)
        result = _service(query).analyze_query("prod", "20260909_00042")
        queue = next(f for f in result.findings if f.rule_id == "QRY-QUEUE-001")
        assert queue.owner is Owner.PLATFORM_TEAM

    def test_query_problems_are_attributed_to_the_author(self) -> None:
        query = _slow_query(elapsed_ms=700_000, queued_ms=5_000)
        result = _service(query).analyze_query("prod", "20260909_00042")
        assert result.findings
        assert all(
            f.owner is Owner.QUERY_AUTHOR
            for f in result.findings
            if f.rule_id != "QRY-QUEUE-001"
        )

    def test_root_cause_outranks_the_symptom_it_explains(self) -> None:
        """A diagnosis must not sort below the symptom it accounts for."""
        result = _service().analyze_query("prod", "20260909_00042")
        ids = [f.rule_id for f in result.findings]
        assert ids.index("QRY-ROOT-001") < ids.index("QRY-SCAN-001")
