"""Contract tests for the query-analysis MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.types import CallToolResult

from core.analysis.models import QueryInfo, QueryState
from core.analysis.service import QueryAnalysisService
from mcp_server.server import build_server
from tests.fakes import FakeConfigService, FakeQueryRepository

SQL = (
    "SELECT c.name, sum(o.amount) FROM orders o JOIN customers c ON o.cid = c.id "
    "WHERE year(o.event_date) = 2026 GROUP BY c.name"
)
SLOW = QueryInfo(
    query_id="20260909_00042",
    cluster="prod",
    source="audit:test",
    state=QueryState.FINISHED,
    sql=SQL,
    session_catalog="hive",
    session_schema="sales",
    elapsed_ms=725_000,
    queued_ms=41_000,
    total_bytes_scanned=4_400_000_000_000,
    output_rows=112,
)

EXPECTED_QUERY_TOOLS = {"analyze_query", "get_query_info"}


def _server() -> Any:
    service = QueryAnalysisService(
        FakeQueryRepository(
            queries={SLOW.query_id: SLOW},
            partitions={"orders": frozenset({"event_date"})},
        )
    )
    return build_server(FakeConfigService(), service)


def _tools() -> dict[str, Any]:
    return {t.name: t for t in asyncio.run(_server().list_tools())}


def _call(name: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(_server().call_tool(name, args))
    assert isinstance(result, CallToolResult)
    return result.structured_content


def test_query_tools_are_registered() -> None:
    assert set(_tools()) >= EXPECTED_QUERY_TOOLS


def test_config_tools_still_present() -> None:
    assert "run_rules" in _tools()


def test_query_tools_are_read_only() -> None:
    for name in EXPECTED_QUERY_TOOLS:
        annotations = _tools()[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True


def test_tools_have_schemas() -> None:
    for name in EXPECTED_QUERY_TOOLS:
        tool = _tools()[name]
        assert tool.description
        assert tool.input_schema
        assert tool.output_schema


def test_analyze_query_documents_the_owner_field() -> None:
    """The model must know that owner tells the user whose problem it is."""
    description = " ".join((_tools()["analyze_query"].description or "").split())
    assert "query_author" in description
    assert "platform_team" in description


def test_analyze_query_documents_the_equivalence_contract() -> None:
    description = " ".join((_tools()["analyze_query"].description or "").split())
    assert "equivalent" in description
    assert "caveat" in description


def test_analyze_query_states_what_empty_findings_mean() -> None:
    description = " ".join((_tools()["analyze_query"].description or "").split())
    assert "coverage.complete" in description


def test_findings_reach_the_caller_with_owner_and_next_step() -> None:
    payload = _call("analyze_query", {"cluster": "prod", "query_id": SLOW.query_id})
    assert payload is not None
    assert payload["found"] is True
    top = payload["findings"][0]
    assert top["owner"] == "query_author"
    assert top["next_step"]
    assert top["is_root_cause"] is True


def test_rewrite_reaches_the_caller() -> None:
    payload = _call("analyze_query", {"cluster": "prod", "query_id": SLOW.query_id})
    assert payload is not None
    assert "2026-01-01" in payload["suggested_sql"]
    assert payload["rewrites"][0]["equivalence"] == "equivalent"


def test_unknown_query_is_reported_not_raised() -> None:
    payload = _call("analyze_query", {"cluster": "prod", "query_id": "nope"})
    assert payload is not None
    assert payload["found"] is False
    assert payload["not_found_reason"]


def test_server_without_query_service_omits_the_tools() -> None:
    """A deployment with no cluster credentials still serves config tools."""
    names = {
        t.name for t in asyncio.run(build_server(FakeConfigService()).list_tools())
    }
    assert not (EXPECTED_QUERY_TOOLS & names)
    assert "run_rules" in names


class TestServerInstructions:
    """The instructions are the server's own prompt surface.

    A client shows them to its model at connection time, so they must
    describe what this deployment actually serves -- not what the server
    served when the string was first written.
    """

    def _instructions(self, *, query_analysis: bool) -> str:
        from core.analysis.service import QueryAnalysisService
        from mcp_server.server import build_server

        service = (
            QueryAnalysisService(FakeQueryRepository()) if query_analysis else None
        )
        return build_server(FakeConfigService(), service).instructions or ""

    def test_names_every_registered_tool(self) -> None:
        """The failure this guards: adding a use case and forgetting the text."""
        instructions = self._instructions(query_analysis=True)
        for tool in asyncio.run(_server().list_tools()):
            assert f"`{tool.name}`" in instructions, tool.name

    def test_describes_both_use_cases_when_both_are_served(self) -> None:
        instructions = self._instructions(query_analysis=True)
        assert "Config auditing" in instructions
        assert "Query analysis" in instructions
        assert "two separate use cases" in instructions

    def test_omits_query_analysis_when_it_is_not_served(self) -> None:
        """A deployment without cluster credentials must not advertise it."""
        instructions = self._instructions(query_analysis=False)
        assert "Config auditing" in instructions
        assert "analyze_query" not in instructions

    def test_states_the_no_model_contract(self) -> None:
        instructions = self._instructions(query_analysis=True)
        assert "no model" in instructions
        assert "identical input always yields identical output" in instructions

    def test_states_the_empty_findings_contract(self) -> None:
        assert "coverage.complete" in self._instructions(query_analysis=True)
