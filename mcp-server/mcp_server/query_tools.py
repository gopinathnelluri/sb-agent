"""MCP tools for use case 2: analysing a completed query.

Thin, like ``server.py``: translate arguments, call one service method,
translate errors. The docstrings are the product -- they are the entire prompt
the calling model sees, and they are written for a model, not a maintainer.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import cleandoc
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from core.analysis.service import QueryAnalysis, QueryAnalysisService
from core.errors import StarburstAgentError

P = ParamSpec("P")
R = TypeVar("R")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

_OWNER_CONTRACT = """
Every finding carries an `owner` saying who can act on it:

* `query_author` -- the person who wrote the SQL can fix this. Lead with these.
* `platform_team` -- nothing in the query will help; it needs whoever runs the
  cluster. Say so plainly rather than suggesting query changes.
* `data_owner` -- the table needs attention, such as statistics or file layout.

Each finding also carries `next_step`, written in plain language for an
analyst who knows SQL but may be new to Starburst. Prefer it over inventing
your own advice.
"""


def _translate_errors(fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except StarburstAgentError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _formatted(fn: Callable[P, R]) -> Callable[P, R]:
    if fn.__doc__:
        fn.__doc__ = cleandoc(fn.__doc__).format(owner_contract=_OWNER_CONTRACT.strip())
    return fn


def register_query_tools(server: MCPServer, service: QueryAnalysisService) -> None:
    """Add the query-analysis tools to an existing server."""

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def analyze_query(cluster: str, query_id: str) -> QueryAnalysis:
        """Explain why a query that already ran was slow, and how to fix it.

        Use this whenever a user asks about a specific query they ran -- "why
        was this slow", "is this query bad", "can this be faster". It reads
        recorded statistics from the query history; it does not re-run
        anything, so it is fast and safe to call.

        You need a `query_id`. If the user pasted SQL instead, ask them for the
        id -- with it you get measured facts, without it only estimates.

        The analysis combines two independent signals: what the query actually
        did at runtime, and what its SQL text looks like. A finding backed by
        both is a root cause and is worth stating firmly. A finding from one
        signal alone is weaker -- `sql_patterns` entries marked `suspected`
        rather than `confirmed` should be raised as a question, not a verdict.

        {owner_contract}

        When `suggested_sql` is present, the query was rewritten mechanically
        and returns the same rows. Show it. Check each entry in `rewrites`:
        `equivalence: "equivalent"` is safe to recommend directly, while
        `"suggested"` needs the user to verify. Always pass on a rewrite's
        `caveat` if it has one.

        An empty `findings` list means nothing was detected -- a real answer,
        not a failure. Check `coverage.complete` before reporting an all clear;
        `coverage.blind_spots` explains in plain language what could not be
        checked, and the user should hear that caveat.

        If `found` is false, the query is not in the history. Read
        `not_found_reason` -- it usually means the record aged out, not that
        the id was wrong.
        """
        return service.analyze_query(cluster, query_id)

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def get_query_info(cluster: str, query_id: str) -> QueryAnalysis:
        """Fetch the recorded facts about one query, without interpreting them.

        Use this when the user asks a specific factual question -- how long did
        it run, how much did it scan, did it fail, who ran it. For "why was it
        slow", call `analyze_query` instead, which returns the same facts plus
        findings.

        Fields that are null were not recorded by the query history for this
        query. That is not the same as zero: a null `spilled_bytes` means the
        source does not track spilling, not that the query did not spill.
        """
        return service.analyze_query(cluster, query_id)
