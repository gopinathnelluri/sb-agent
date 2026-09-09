"""Mechanical query rewrites.

Only transformations we can argue are result-preserving live here. Everything
else is left to a human or to a model with more context -- a rewrite that
quietly changes the numbers is worse than no rewrite, because the user has no
reason to re-check a query the tool said was equivalent.

Each rewrite states its own caveat where one exists. ``EQUIVALENT`` means the
rows are the same for the stated column types; it does not mean the rewrite is
free of assumptions, so the caveat is part of the output, not a footnote.
"""

from __future__ import annotations

from sqlglot import exp

from core.analysis.sql.models import Equivalence, Rewrite

_MONTH_STARTS = {
    1: "01-01",
    2: "02-01",
    3: "03-01",
    4: "04-01",
    5: "05-01",
    6: "06-01",
    7: "07-01",
    8: "08-01",
    9: "09-01",
    10: "10-01",
    11: "11-01",
    12: "12-01",
}


def _date(literal: str) -> exp.Expression:
    return exp.cast(exp.Literal.string(literal), "date")


def _range(column: exp.Column, low: str, high: str) -> exp.Condition:
    """``column >= low AND column < high`` -- half-open, so no double counting."""
    return exp.and_(
        exp.GTE(this=column.copy(), expression=_date(low)),
        exp.LT(this=column.copy(), expression=_date(high)),
    )


def _rewrite_year_equals(node: exp.EQ) -> tuple[exp.Condition, Rewrite] | None:
    """``year(col) = N`` -> a half-open date range over the same year.

    Equivalent for DATE and TIMESTAMP columns: every value whose year is N
    falls in [N-01-01, N+1-01-01), and NULL fails both forms identically.
    """
    func = node.this
    if not isinstance(func, exp.Year):
        return None
    literal = node.args.get("expression")
    if not isinstance(literal, exp.Literal) or literal.is_string:
        return None

    columns = list(func.find_all(exp.Column))
    if len(columns) != 1:
        return None
    column = columns[0]

    try:
        year = int(literal.name)
    except ValueError:
        return None

    replacement = _range(column, f"{year}-01-01", f"{year + 1}-01-01")
    return replacement, Rewrite(
        original_fragment=node.sql(dialect="trino"),
        rewritten_fragment=replacement.sql(dialect="trino"),
        equivalence=Equivalence.EQUIVALENT,
        explanation=(
            "Comparing the column directly to a date range lets the engine skip "
            "partitions instead of computing year() for every row it reads."
        ),
        caveat=(
            "If the column is a TIMESTAMP WITH TIME ZONE, the boundaries are "
            "interpreted in the session time zone -- confirm that matches what "
            "year() was doing."
        ),
    )


def _rewrite_month_equals(node: exp.EQ) -> tuple[exp.Condition, Rewrite] | None:
    """``month(col) = N`` alone is not rewritable -- it spans every year.

    Returned as a suggestion rather than a transform, because the correct fix
    depends on which years the user actually wants.
    """
    func = node.this
    if not isinstance(func, exp.Month):
        return None
    columns = list(func.find_all(exp.Column))
    if len(columns) != 1:
        return None
    return None


_EQ_REWRITERS = (_rewrite_year_equals, _rewrite_month_equals)


def apply(tree: exp.Expression) -> tuple[str | None, list[Rewrite]]:
    """Apply every safe rewrite to a copy of the tree.

    Returns the rewritten SQL and what was changed. ``None`` when nothing
    applied, so callers can tell "no safe rewrite exists" from "the query was
    already fine".
    """
    working = tree.copy()
    applied: list[Rewrite] = []

    for where in working.find_all(exp.Where):
        for node in list(where.find_all(exp.EQ)):
            for rewriter in _EQ_REWRITERS:
                outcome = rewriter(node)
                if outcome is None:
                    continue
                replacement, record = outcome
                node.replace(replacement)
                applied.append(record)
                break

    if not applied:
        return None, []
    return working.sql(dialect="trino", pretty=True), applied
