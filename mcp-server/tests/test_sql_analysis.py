"""Tests for static SQL analysis: parsing, patterns, and rewrites.

Two properties carry the weight here. Unparseable SQL must be reported rather
than read as clean; and a rewrite marked ``equivalent`` must actually be
equivalent, because the user has no reason to re-check one the tool vouched
for.
"""

from __future__ import annotations

import pytest
import sqlglot

from core.analysis.sql.analyzer import analyze_sql, rewritten_sql
from core.analysis.sql.models import Confidence, Equivalence, TableFacts
from core.analysis.sql.parse import parse, table_names

PARTITIONED = {"orders": TableFacts("orders", frozenset({"event_date"}))}


class TestParsing:
    def test_parses_trino_sql(self) -> None:
        tree, error = parse("SELECT a FROM t WHERE b = 1")
        assert tree is not None
        assert error is None

    def test_reports_unparseable_sql_instead_of_raising(self) -> None:
        tree, error = parse("SELECT FROM WHERE ***")
        assert tree is None
        assert error is not None

    @pytest.mark.parametrize("sql", ["", "   ", "\n"])
    def test_empty_statement_is_an_error_not_a_crash(self, sql: str) -> None:
        assert parse(sql)[0] is None

    def test_oversized_statement_is_refused(self) -> None:
        """Refused on length before parsing, so a huge statement is cheap to reject."""
        from core.analysis.sql.parse import MAX_SQL_CHARS

        tree, error = parse("SELECT a FROM t -- " + "x" * MAX_SQL_CHARS)
        assert tree is None
        assert error is not None
        assert "exceeds" in error

    def test_finds_table_names(self) -> None:
        tree, _ = parse("SELECT * FROM a JOIN b ON a.id = b.id")
        assert tree is not None
        assert table_names(tree) == ("a", "b")


class TestUnparseableIsNotClean:
    def test_analysis_marks_failure_explicitly(self) -> None:
        """The failure mode that would let a broken parse look like a clean query."""
        result = analyze_sql("NOT ACTUALLY ;; SQL ***")
        assert result.parsed is False
        assert result.parse_error is not None
        assert result.patterns == []


class TestPatterns:
    def test_function_on_partition_column_is_confirmed(self) -> None:
        result = analyze_sql(
            "SELECT * FROM orders WHERE year(event_date) = 2026", PARTITIONED
        )
        hits = [p for p in result.patterns if p.kind == "function_on_filter_column"]
        assert len(hits) == 1
        assert hits[0].confidence is Confidence.CONFIRMED

    def test_same_pattern_only_suspected_without_facts(self) -> None:
        """Without table metadata we must hedge, not accuse."""
        result = analyze_sql("SELECT * FROM orders WHERE year(event_date) = 2026")
        hits = [p for p in result.patterns if p.kind == "function_on_filter_column"]
        assert hits[0].confidence is Confidence.SUSPECTED

    def test_harmless_functions_are_not_flagged(self) -> None:
        result = analyze_sql("SELECT * FROM orders WHERE lower(region) = 'west'")
        assert not [p for p in result.patterns if p.kind == "function_on_filter_column"]

    def test_cast_on_join_key(self) -> None:
        result = analyze_sql("SELECT * FROM a JOIN b ON CAST(a.id AS VARCHAR) = b.id")
        assert any(p.kind == "cast_on_join_key" for p in result.patterns)

    def test_leading_wildcard_like(self) -> None:
        result = analyze_sql("SELECT * FROM t WHERE name LIKE '%smith'")
        assert any(p.kind == "leading_wildcard_like" for p in result.patterns)

    def test_trailing_wildcard_is_fine(self) -> None:
        result = analyze_sql("SELECT a FROM t WHERE name LIKE 'smith%'")
        assert not any(p.kind == "leading_wildcard_like" for p in result.patterns)

    def test_select_star(self) -> None:
        assert any(
            p.kind == "select_star" for p in analyze_sql("SELECT * FROM t").patterns
        )

    def test_named_columns_are_fine(self) -> None:
        result = analyze_sql("SELECT a, b FROM t")
        assert not any(p.kind == "select_star" for p in result.patterns)

    def test_order_by_without_limit(self) -> None:
        result = analyze_sql("SELECT a FROM t ORDER BY a")
        assert any(p.kind == "order_by_without_limit" for p in result.patterns)

    def test_order_by_with_limit_is_fine(self) -> None:
        result = analyze_sql("SELECT a FROM t ORDER BY a LIMIT 10")
        assert not any(p.kind == "order_by_without_limit" for p in result.patterns)

    def test_cross_join(self) -> None:
        result = analyze_sql("SELECT * FROM a CROSS JOIN b")
        assert any(p.kind == "cross_join" for p in result.patterns)

    def test_clean_query_yields_no_patterns(self) -> None:
        result = analyze_sql(
            "SELECT id, amount FROM orders "
            "WHERE event_date >= DATE '2026-01-01' LIMIT 100",
            PARTITIONED,
        )
        assert result.patterns == []


class TestRewrites:
    def test_year_equals_becomes_a_date_range(self) -> None:
        result = analyze_sql(
            "SELECT a FROM orders WHERE year(event_date) = 2026", PARTITIONED
        )
        assert len(result.rewrites) == 1
        assert result.rewrites[0].equivalence is Equivalence.EQUIVALENT

    def test_rewrite_uses_a_half_open_range(self) -> None:
        """Half-open avoids double-counting the boundary."""
        sql = rewritten_sql("SELECT a FROM orders WHERE year(event_date) = 2026")
        assert sql is not None
        assert ">= CAST('2026-01-01' AS DATE)" in sql
        assert "< CAST('2027-01-01' AS DATE)" in sql

    def test_rewrite_carries_its_caveat(self) -> None:
        """An assumption stated is an assumption the user can check."""
        result = analyze_sql("SELECT a FROM orders WHERE year(event_date) = 2026")
        assert result.rewrites[0].caveat is not None
        assert "TIME ZONE" in result.rewrites[0].caveat

    def test_rewritten_sql_still_parses(self) -> None:
        sql = rewritten_sql(
            "SELECT c.name FROM orders o JOIN customers c ON o.cid = c.id "
            "WHERE year(o.event_date) = 2026 GROUP BY c.name"
        )
        assert sql is not None
        assert sqlglot.parse_one(sql, dialect="trino") is not None

    def test_rewrite_preserves_the_rest_of_the_query(self) -> None:
        sql = rewritten_sql(
            "SELECT c.name, sum(o.amount) AS total FROM orders o "
            "JOIN customers c ON o.cid = c.id "
            "WHERE year(o.event_date) = 2026 GROUP BY c.name"
        )
        assert sql is not None
        for fragment in ("SUM(o.amount)", "AS total", "JOIN", "GROUP BY"):
            assert fragment in sql

    def test_no_rewrite_when_nothing_is_safely_rewritable(self) -> None:
        """Silence is correct when we cannot prove a transform."""
        assert rewritten_sql("SELECT a FROM t WHERE name LIKE '%x'") is None

    def test_unparseable_sql_yields_no_rewrite(self) -> None:
        assert rewritten_sql("*** not sql ***") is None

    def test_original_is_not_mutated(self) -> None:
        """Rewriting works on a copy; the caller's tree must be untouched."""
        original = "SELECT a FROM orders WHERE year(event_date) = 2026"
        rewritten_sql(original)
        assert analyze_sql(original).rewrites[0].original_fragment.startswith("YEAR")
