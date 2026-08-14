"""Contract tests for the MCP tool surface.

These assert the properties the parent agent depends on: that the tools
exist, that they are advertised as read-only, that their descriptions carry
the guidance a model needs, and that domain errors arrive as actionable
messages rather than tracebacks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.models import Role
from core.ports import ConfigService
from core.scopes import Scope
from mcp_server.server import build_server
from tests.fakes import FakeConfigService

EXPECTED_TOOLS = {
    "list_clusters",
    "get_config_summary",
    "run_rules",
    "get_config_detail",
    "diff_clusters",
}


def _tools(service: ConfigService | None = None) -> dict[str, Any]:
    server = build_server(service or FakeConfigService())
    return {t.name: t for t in asyncio.run(server.list_tools())}


def _call(service: ConfigService, name: str, args: dict[str, Any]) -> Any:
    server = build_server(service)
    return asyncio.run(server.call_tool(name, args))


def test_fake_satisfies_the_service_protocol() -> None:
    assert isinstance(FakeConfigService(), ConfigService)


def test_exactly_the_use_case_one_tools_are_exposed() -> None:
    assert set(_tools()) == EXPECTED_TOOLS


def test_every_tool_is_marked_read_only() -> None:
    for name, tool in _tools().items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is True, name


def test_every_tool_has_input_and_output_schemas() -> None:
    for name, tool in _tools().items():
        assert tool.description, name
        assert tool.input_schema, name
        assert tool.output_schema, name


def test_scope_argument_offers_every_scope() -> None:
    """The advertised enum must not drift from the Scope type."""
    schema = _tools()["run_rules"].input_schema
    scope_def = schema["$defs"]["Scope"]
    assert set(scope_def["enum"]) == {s.value for s in Scope}


def test_scoped_tools_document_the_scope_table() -> None:
    """Tools taking scopes must show the model how to choose one."""
    for name in ("get_config_summary", "run_rules"):
        description = _tools()[name].description or ""
        assert "| scope |" in description, name
        for scope in Scope:
            assert f"`{scope.value}`" in description, (name, scope)


def test_run_rules_states_what_an_empty_findings_list_means() -> None:
    description = _tools()["run_rules"].description or ""
    assert "empty `findings` list means no rule fired" in description
    assert "coverage.complete" in description


def test_run_rules_states_that_context_cannot_change_findings() -> None:
    description = " ".join((_tools()["run_rules"].description or "").split())
    assert "non-decisional" in description
    assert "cannot create, remove or re-grade a finding" in description
    assert "unvalidated guidance" in description


def test_scope_is_required_and_has_no_default() -> None:
    """Scope is an argument, never implicit -- there is no validate_everything."""
    schema = _tools()["run_rules"].input_schema
    assert "scopes" in schema["required"]


def test_findings_reach_the_caller_with_evidence() -> None:
    result = _call(
        FakeConfigService(),
        "run_rules",
        {"cluster": "prod-analytics", "scopes": ["memory"]},
    )
    payload = result.structured_content
    assert payload is not None
    finding = payload["findings"][0]
    assert finding["rule_id"] == "SEP-MEM-002"
    assert finding["evidence"][0]["file"] == "worker/config.properties"
    assert finding["evidence"][0]["line"] == 27
    assert finding["rationale_source"] == "rule_catalog"
    assert payload["coverage"]["complete"] is True


def test_clean_cluster_returns_no_findings_and_complete_coverage() -> None:
    service = FakeConfigService(findings=False)
    payload = _call(
        service, "run_rules", {"cluster": "prod-analytics", "scopes": ["memory"]}
    ).structured_content
    assert payload is not None
    assert payload["findings"] == []
    assert payload["coverage"]["complete"] is True
    assert payload["coverage"]["blind_spots"] == []


def test_incomplete_run_is_distinguishable_from_a_clean_one() -> None:
    """The failure mode that matters: silence must not look like an all clear."""
    service = FakeConfigService(complete=False, findings=False)
    payload = _call(
        service, "run_rules", {"cluster": "prod-analytics", "scopes": ["memory"]}
    ).structured_content
    assert payload is not None
    assert payload["findings"] == []
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["blind_spots"]


def test_unknown_cluster_error_tells_the_model_what_to_do() -> None:
    with pytest.raises(ToolError) as excinfo:
        _call(
            FakeConfigService(), "run_rules", {"cluster": "nope", "scopes": ["memory"]}
        )
    message = str(excinfo.value)
    assert "list_clusters" in message
    assert "prod-analytics" in message


def test_unredacted_secret_is_refused_without_leaking_the_value() -> None:
    with pytest.raises(ToolError) as excinfo:
        _call(
            FakeConfigService(),
            "get_config_detail",
            {
                "cluster": "prod-analytics",
                "role": "worker",
                "file": "leaky.properties",
            },
        )
    message = str(excinfo.value)
    assert "Refusing to return" in message
    assert "hive.s3.aws-secret-key" in message
    assert "do not retry" in message


def test_context_is_forwarded_as_a_typed_object() -> None:
    service = FakeConfigService()
    _call(
        service,
        "run_rules",
        {
            "cluster": "prod-analytics",
            "scopes": ["memory"],
            "context": {"source": "parent_rag", "sep_version_hint": "429"},
        },
    )
    _, args = service.calls[-1]
    assert isinstance(args, tuple)
    context = args[2]
    assert context is not None
    assert context.sep_version_hint == "429"


def test_node_argument_is_optional_on_config_detail() -> None:
    payload = _call(
        FakeConfigService(),
        "get_config_detail",
        {
            "cluster": "prod-analytics",
            "role": Role.WORKER.value,
            "file": "config.properties",
        },
    ).structured_content
    assert payload is not None
    assert payload["node"] is None
    assert payload["redaction_verified"] is True
