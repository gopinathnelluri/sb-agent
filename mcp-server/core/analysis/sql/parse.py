"""Parsing Trino SQL, defensively.

Query text arrives from an audit log and may be truncated, use an unfamiliar
construct, or be a dialect we do not fully model. A parse failure is never
fatal: it yields ``None`` and the caller records a blind spot, because
"we could not read the SQL" and "the SQL is fine" must not look the same.
"""

from __future__ import annotations

from typing import cast

import sqlglot
from sqlglot import exp

DIALECT = "trino"
MAX_SQL_CHARS = 1_000_000


def parse(sql: str) -> tuple[exp.Expression | None, str | None]:
    """Parse Trino SQL. Returns (tree, error) -- exactly one is None."""
    if not sql or not sql.strip():
        return None, "empty statement"
    if len(sql) > MAX_SQL_CHARS:
        return None, f"statement exceeds {MAX_SQL_CHARS} characters"
    try:
        # parse_one's return is a TypeVar that only resolves when `into=` is
        # given; without it the concrete type is still Expression.
        tree = cast(exp.Expression, sqlglot.parse_one(sql, dialect=DIALECT))
    except Exception as exc:  # noqa: BLE001 - sqlglot raises several types
        return None, f"could not parse as Trino SQL: {exc}"
    return tree, None


def table_names(tree: exp.Expression) -> tuple[str, ...]:
    """Distinct table names referenced, in a stable order."""
    seen: list[str] = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def column_name(column: exp.Column) -> str:
    """Bare column name, without table qualifier."""
    return column.name
