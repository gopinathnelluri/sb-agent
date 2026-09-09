"""Entry point for static SQL analysis: parse, find patterns, offer rewrites."""

from __future__ import annotations

from collections.abc import Mapping

from core.analysis.sql.models import SqlAnalysis, TableFacts
from core.analysis.sql.parse import parse, table_names
from core.analysis.sql.patterns import ALL_PATTERNS
from core.analysis.sql.rewrite import apply


def analyze_sql(sql: str, facts: Mapping[str, TableFacts] | None = None) -> SqlAnalysis:
    """Read the query text. Never raises; unparseable SQL is reported, not hidden."""
    tree, error = parse(sql)
    if tree is None:
        return SqlAnalysis(parsed=False, parse_error=error)

    known = dict(facts or {})
    patterns = [p for check in ALL_PATTERNS for p in check(tree, known)]
    _, rewrites = apply(tree)

    return SqlAnalysis(
        parsed=True,
        patterns=patterns,
        rewrites=rewrites,
        tables=table_names(tree),
    )


def rewritten_sql(sql: str) -> str | None:
    """The query with every safe rewrite applied, or None if none applied."""
    tree, _ = parse(sql)
    if tree is None:
        return None
    rewritten, _ = apply(tree)
    return rewritten
