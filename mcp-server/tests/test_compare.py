"""Tests for comparing two query runs.

The property that matters most: a comparison must distinguish a change the
user made from a change that happened to them. Same numbers, opposite
meanings -- a query reading half as much after a rewrite is a success, the
same drop with unchanged SQL means the data moved underneath them.
"""

from __future__ import annotations

from core.analysis.compare import ChangeDirection, QueryComparison, compare
from core.analysis.models import QueryInfo, QueryState
from core.analysis.service import QueryAnalysisService
from tests.fakes import FakeQueryRepository

SLOW_SQL = (
    "SELECT c.region, sum(o.amount) FROM orders o "
    "JOIN customers c ON o.cid = c.id "
    "WHERE year(o.order_date) = 2026 GROUP BY c.region"
)
FAST_SQL = (
    "SELECT c.region, sum(o.amount) FROM orders o "
    "JOIN customers c ON o.cid = c.id "
    "WHERE o.order_date >= DATE '2026-01-01' "
    "AND o.order_date < DATE '2027-01-01' GROUP BY c.region"
)


def _query(query_id: str, sql: str | None, **kwargs: object) -> QueryInfo:
    base: dict[str, object] = {
        "query_id": query_id,
        "cluster": "prod",
        "source": "audit:test",
        "state": QueryState.FINISHED,
        "sql": sql,
        "session_catalog": "hive",
        "session_schema": "sales",
        "output_rows": 6,
    }
    base.update(kwargs)
    return QueryInfo(**base)  # type: ignore[arg-type]


def _service_with(*queries: QueryInfo) -> QueryAnalysisService:
    return QueryAnalysisService(
        FakeQueryRepository(
            queries={q.query_id: q for q in queries},
            partitions={"orders": frozenset({"order_date"})},
        )
    )


class TestMetricChanges:
    def test_reports_a_slowdown(self) -> None:
        result = compare(
            _query("a", SLOW_SQL, elapsed_ms=60_000),
            _query("b", SLOW_SQL, elapsed_ms=600_000),
            [],
            [],
        )
        elapsed = next(c for c in result.changes if c.name == "elapsed_ms")
        assert elapsed.direction is ChangeDirection.WORSENED
        assert "10.0x" in result.verdict

    def test_reports_a_speedup(self) -> None:
        result = compare(
            _query("a", SLOW_SQL, elapsed_ms=600_000),
            _query("b", FAST_SQL, elapsed_ms=60_000),
            [],
            [],
        )
        assert "faster" in result.verdict

    def test_reading_less_data_counts_as_improvement(self) -> None:
        """Direction is about outcome, not which way the number moved."""
        result = compare(
            _query("a", SLOW_SQL, total_bytes_scanned=4_000_000_000_000),
            _query("b", SLOW_SQL, total_bytes_scanned=8_000_000_000),
            [],
            [],
        )
        scanned = next(c for c in result.changes if c.name == "total_bytes_scanned")
        assert scanned.direction is ChangeDirection.IMPROVED

    def test_returning_fewer_rows_is_not_an_improvement(self) -> None:
        """Less output is a behaviour change, not a win."""
        result = compare(
            _query("a", SLOW_SQL, output_rows=1_000),
            _query("b", SLOW_SQL, output_rows=10),
            [],
            [],
        )
        rows = next(c for c in result.changes if c.name == "output_rows")
        assert rows.direction is ChangeDirection.WORSENED

    def test_small_differences_are_noise(self) -> None:
        result = compare(
            _query("a", SLOW_SQL, elapsed_ms=100_000),
            _query("b", SLOW_SQL, elapsed_ms=104_000),
            [],
            [],
        )
        elapsed = next(c for c in result.changes if c.name == "elapsed_ms")
        assert elapsed.direction is ChangeDirection.UNCHANGED

    def test_metrics_absent_from_either_run_are_omitted(self) -> None:
        result = compare(
            _query("a", SLOW_SQL, elapsed_ms=100),
            _query("b", SLOW_SQL, elapsed_ms=200),
            [],
            [],
        )
        assert all(c.comparable for c in result.changes)


class TestSameSqlDetection:
    def test_identical_sql_is_recognised(self) -> None:
        result = compare(_query("a", SLOW_SQL), _query("b", SLOW_SQL), [], [])
        assert result.same_sql is True

    def test_formatting_alone_is_not_a_change(self) -> None:
        reformatted = SLOW_SQL.replace(" ", "\n  ").upper()
        result = compare(_query("a", SLOW_SQL), _query("b", reformatted), [], [])
        assert result.same_sql is True

    def test_different_sql_is_recognised(self) -> None:
        result = compare(_query("a", SLOW_SQL), _query("b", FAST_SQL), [], [])
        assert result.same_sql is False

    def test_same_sql_says_the_cause_is_external(self) -> None:
        """The distinction that stops us blaming a user for a data change."""
        result = compare(_query("a", SLOW_SQL), _query("b", SLOW_SQL), [], [])
        assert any("outside the query" in note for note in result.notes)

    def test_missing_sql_is_stated_not_assumed(self) -> None:
        result = compare(_query("a", None), _query("b", SLOW_SQL), [], [])
        assert result.same_sql is False
        assert any("did not record SQL" in note for note in result.notes)


class TestRewriteLoop:
    """The loop this analyser exists to close: suggest, apply, verify."""

    def _comparison(self) -> QueryComparison:
        before = _query(
            "before",
            SLOW_SQL,
            elapsed_ms=1_284_000,
            total_bytes_scanned=4_400_000_000_000,
        )
        after = _query(
            "after", FAST_SQL, elapsed_ms=41_000, total_bytes_scanned=9_200_000_000
        )
        return _service_with(before, after).compare_queries("prod", "before", "after")

    def test_verdict_states_the_improvement(self) -> None:
        result = self._comparison()
        assert "faster" in result.verdict

    def test_the_root_cause_is_reported_as_resolved(self) -> None:
        result = self._comparison()
        resolved = {f.rule_id for f in result.findings_only_in_a}
        assert "QRY-ROOT-001" in resolved

    def test_no_new_problems_were_introduced(self) -> None:
        result = self._comparison()
        assert result.findings_only_in_b == []


class TestServiceLevel:
    def test_unknown_query_explains_which_one_is_missing(self) -> None:
        service = _service_with(_query("known", SLOW_SQL, elapsed_ms=1_000))
        result = service.compare_queries("prod", "known", "missing")
        assert result.found is False
        assert result.not_found_reason is not None
        assert "'missing'" in result.not_found_reason

    def test_both_missing_names_both(self) -> None:
        result = _service_with().compare_queries("prod", "one", "two")
        assert result.not_found_reason is not None
        assert "'one'" in result.not_found_reason
        assert "'two'" in result.not_found_reason

    def test_comparison_agrees_with_the_individual_analyses(self) -> None:
        """One code path, so a comparison can never contradict an analysis."""
        before = _query(
            "before",
            SLOW_SQL,
            elapsed_ms=900_000,
            total_bytes_scanned=4_000_000_000_000,
        )
        after = _query("after", FAST_SQL, elapsed_ms=30_000, total_bytes_scanned=1_000)
        service = _service_with(before, after)
        alone = {f.rule_id for f in service.analyze_query("prod", "before").findings}
        comparison = service.compare_queries("prod", "before", "after")
        together = {f.rule_id for f in comparison.findings_only_in_a} | {
            f.rule_id for f in comparison.findings_in_both
        }
        assert alone == together


class TestAutomaticHistory:
    """Context arrives with the analysis, not on a second call.

    A finding on its own invites "is that a lot?". The same finding beside
    the query's own past is a measurement rather than a judgement, and it is
    what tells a user nothing is wrong with what they wrote.
    """

    def _service(self) -> QueryAnalysisService:
        earlier = _query(
            "earlier",
            SLOW_SQL,
            elapsed_ms=94_000,
            total_bytes_scanned=210_000_000_000,
            ended_at="2026-08-12T09:01:35Z",
        )
        today = _query(
            "today",
            SLOW_SQL,
            elapsed_ms=1_284_000,
            total_bytes_scanned=4_400_000_000_000,
            ended_at="2026-09-15T09:55:36Z",
        )
        return _service_with(earlier, today)

    def test_analysis_includes_the_previous_run(self) -> None:
        result = self._service().analyze_query("prod", "today")
        assert result.history is not None
        assert result.previous_run_count == 1

    def test_history_says_the_sql_did_not_change(self) -> None:
        """Which is what tells the user they did not break it."""
        result = self._service().analyze_query("prod", "today")
        assert result.history is not None
        assert result.history.same_sql is True

    def test_history_quantifies_the_regression(self) -> None:
        result = self._service().analyze_query("prod", "today")
        assert result.history is not None
        assert "slower" in result.history.verdict

    def test_no_history_for_a_first_run(self) -> None:
        """Normal for ad-hoc work, and not an error."""
        only = _query("only", SLOW_SQL, elapsed_ms=1_000)
        result = _service_with(only).analyze_query("prod", "only")
        assert result.history is None
        assert result.previous_run_count == 0

    def test_a_different_query_is_not_a_baseline(self) -> None:
        """A baseline drawn from other SQL is worse than none."""
        other = _query("other", FAST_SQL, elapsed_ms=1_000)
        mine = _query("mine", SLOW_SQL, elapsed_ms=900_000)
        result = _service_with(other, mine).analyze_query("prod", "mine")
        assert result.history is None

    def test_history_failure_does_not_cost_the_analysis(self) -> None:
        """Context is a bonus; losing it must not lose the findings."""

        class Broken(FakeQueryRepository):
            def previous_runs(self, cluster, query, limit=5):  # type: ignore[no-untyped-def]
                raise RuntimeError("history lookup failed")

        query = _query(
            "q", SLOW_SQL, elapsed_ms=900_000, total_bytes_scanned=4_000_000_000_000
        )
        service = QueryAnalysisService(
            Broken(
                queries={"q": query},
                partitions={"orders": frozenset({"order_date"})},
            )
        )
        result = service.analyze_query("prod", "q")
        assert result.findings
        assert result.history is None
