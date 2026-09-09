"""Anti-patterns visible in the query text alone.

Each check is a pure function over the parsed tree. None of them prove a
performance problem on their own -- a full scan is correct when you really do
want the whole table. They become conclusive when a runtime signal agrees,
which is what ``correlate.py`` does with them.

Where table facts are supplied, a pattern is CONFIRMED; without them it stays
SUSPECTED, so the phrasing downstream can hedge honestly.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import exp

from core.analysis.sql.models import Confidence, SqlPattern, TableFacts

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)

# Functions that are cheap and do not defeat pruning on their own.
_HARMLESS_FUNCS = {"lower", "upper", "trim", "coalesce"}


def _partition_columns(facts: Mapping[str, TableFacts]) -> set[str]:
    return {col for f in facts.values() for col in f.partition_columns}


def function_on_filter_column(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """A WHERE predicate wraps a column in a function.

    This is the single most common cause of a full scan on a partitioned
    table: the engine cannot evaluate the function against partition metadata,
    so it reads every partition and filters afterwards.
    """
    partition_cols = _partition_columns(facts)
    found: list[SqlPattern] = []

    for where in tree.find_all(exp.Where):
        for comparison in where.find_all(*_COMPARISONS):
            for side in (comparison.this, comparison.args.get("expression")):
                if not isinstance(side, exp.Func) or isinstance(side, exp.Cast):
                    continue
                if side.key.lower() in _HARMLESS_FUNCS:
                    continue
                columns = tuple(
                    sorted({c.name for c in side.find_all(exp.Column) if c.name})
                )
                if not columns:
                    continue
                hits_partition = bool(set(columns) & partition_cols)
                detail = (
                    f"'{columns[0]}' is a partition column, so wrapping it in "
                    f"{side.key.upper()}() forces every partition to be read"
                    if hits_partition
                    else (
                        f"wrapping '{columns[0]}' in {side.key.upper()}() prevents "
                        "the engine from using column statistics or partition "
                        "metadata to skip data"
                    )
                )
                found.append(
                    SqlPattern(
                        kind="function_on_filter_column",
                        fragment=comparison.sql(dialect="trino"),
                        explanation=detail,
                        confidence=(
                            Confidence.CONFIRMED
                            if hits_partition
                            else Confidence.SUSPECTED
                        ),
                        columns=columns,
                    )
                )
    return found


def cast_on_join_key(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """A join condition casts one side, which blocks efficient join strategies."""
    found: list[SqlPattern] = []
    for join in tree.find_all(exp.Join):
        condition = join.args.get("on")
        if condition is None:
            continue
        for cast in condition.find_all(exp.Cast):
            columns = tuple(
                sorted({c.name for c in cast.find_all(exp.Column) if c.name})
            )
            if not columns:
                continue
            found.append(
                SqlPattern(
                    kind="cast_on_join_key",
                    fragment=cast.sql(dialect="trino"),
                    explanation=(
                        f"casting '{columns[0]}' inside the join condition stops the "
                        "engine matching on the raw column, which rules out dynamic "
                        "filtering and can force a much larger join"
                    ),
                    confidence=Confidence.SUSPECTED,
                    columns=columns,
                )
            )
    return found


def leading_wildcard_like(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """``LIKE '%foo'`` cannot use any statistics to narrow the scan."""
    found: list[SqlPattern] = []
    for like in tree.find_all(exp.Like):
        pattern = like.args.get("expression")
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            continue
        if not pattern.this.startswith("%"):
            continue
        columns = tuple(sorted({c.name for c in like.find_all(exp.Column) if c.name}))
        found.append(
            SqlPattern(
                kind="leading_wildcard_like",
                fragment=like.sql(dialect="trino"),
                explanation=(
                    "a pattern starting with '%' has to be tested against every "
                    "row; min/max column statistics cannot narrow it down"
                ),
                confidence=Confidence.SUSPECTED,
                columns=columns,
            )
        )
    return found


def select_star(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """``SELECT *`` reads every column, including ones never used."""
    if not any(isinstance(e, exp.Star) for e in tree.expressions):
        return []
    return [
        SqlPattern(
            kind="select_star",
            fragment="SELECT *",
            explanation=(
                "columnar formats let the engine read only the columns you name; "
                "SELECT * gives up that saving and reads all of them"
            ),
            confidence=Confidence.SUSPECTED,
        )
    ]


def order_by_without_limit(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """A global sort with no LIMIT funnels every row through one node."""
    order = tree.args.get("order")
    if order is None or tree.args.get("limit") is not None:
        return []
    return [
        SqlPattern(
            kind="order_by_without_limit",
            fragment=order.sql(dialect="trino"),
            explanation=(
                "a final ORDER BY with no LIMIT must gather every result row onto "
                "a single node to sort it, which does not scale with the cluster"
            ),
            confidence=Confidence.SUSPECTED,
        )
    ]


def cross_join(
    tree: exp.Expression, facts: Mapping[str, TableFacts]
) -> list[SqlPattern]:
    """A join with no condition multiplies row counts."""
    found: list[SqlPattern] = []
    for join in tree.find_all(exp.Join):
        if join.args.get("on") is not None or join.args.get("using") is not None:
            continue
        side = (join.side or join.kind or "CROSS").upper()
        found.append(
            SqlPattern(
                kind="cross_join",
                fragment=f"{side} JOIN {join.this.sql(dialect='trino')}",
                explanation=(
                    "a join with no condition pairs every row on the left with "
                    "every row on the right, so output size is the product of the "
                    "two inputs"
                ),
                confidence=Confidence.SUSPECTED,
            )
        )
    return found


ALL_PATTERNS = (
    function_on_filter_column,
    cast_on_join_key,
    leading_wildcard_like,
    select_star,
    order_by_without_limit,
    cross_join,
)
