"""The whole stack, called the way the parent agent calls it.

Every other test exercises one layer. This one goes through the real MCP tool
surface into the real service into the real catalog and fixtures, checking the
JSON a parent would actually receive.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from core.ports import ConfigService
from core.service import ConfigValidationService
from mcp_server.server import build_server


@pytest.fixture
def call(service: ConfigValidationService) -> Any:
    server = build_server(service)

    def invoke(name: str, arguments: dict[str, Any]) -> Any:
        result = asyncio.run(server.call_tool(name, arguments))
        assert isinstance(result, CallToolResult)
        return result.structured_content

    return invoke


def test_the_real_service_satisfies_the_protocol(
    service: ConfigValidationService,
) -> None:
    assert isinstance(service, ConfigService)


def test_list_clusters_round_trips_to_json(call: Any) -> None:
    payload = call("list_clusters", {})
    names = {row["name"] for row in payload["result"]}
    assert names == {"clean-cluster", "drifted-cluster", "bare-cluster"}


def test_a_healthy_cluster_reports_no_issues(call: Any) -> None:
    payload = call(
        "run_rules", {"cluster": "clean-cluster", "scopes": ["memory", "jvm"]}
    )
    assert payload["findings"] == []
    assert payload["coverage"]["complete"] is True


def test_a_misconfigured_cluster_reports_findings_with_evidence(call: Any) -> None:
    payload = call("run_rules", {"cluster": "drifted-cluster", "scopes": ["memory"]})
    finding = next(f for f in payload["findings"] if f["rule_id"] == "SEP-MEM-002")
    assert finding["severity"] == "high"
    assert finding["actual"] == "40GB"
    assert finding["evidence"][0]["file"] == "etc/starburst/config.properties"
    assert finding["evidence"][0]["line"] > 0
    assert finding["rationale_source"] == "rule_catalog"
    assert finding["doc_ref"]["source_doc"] == "Starburst Tuning Guide"


def test_findings_arrive_sorted_most_severe_first(call: Any) -> None:
    payload = call(
        "run_rules",
        {"cluster": "drifted-cluster", "scopes": ["memory", "jvm", "node_identity"]},
    )
    order = ["critical", "high", "medium", "low", "info"]
    ranks = [order.index(f["severity"]) for f in payload["findings"]]
    assert ranks == sorted(ranks)


def test_context_arrives_and_only_annotates(call: Any) -> None:
    payload = call(
        "run_rules",
        {
            "cluster": "drifted-cluster",
            "scopes": ["memory"],
            "context": {
                "source": "parent_rag",
                "properties": {
                    "query.low-memory-killer.policy": "prefer blocked nodes"
                },
            },
        },
    )
    assert payload["annotations"][0]["subject"] == "query.low-memory-killer.policy"
    assert payload["annotations"][0]["source"] == "injected_context"
    assert all(
        f["subject"] != "query.low-memory-killer.policy" for f in payload["findings"]
    )


def test_config_detail_follows_a_finding_to_its_file(call: Any) -> None:
    findings = call("run_rules", {"cluster": "drifted-cluster", "scopes": ["memory"]})[
        "findings"
    ]
    evidence = findings[0]["evidence"][0]
    detail = call(
        "get_config_detail",
        {
            "cluster": "drifted-cluster",
            "role": evidence["role"],
            "file": evidence["file"],
            "node": evidence["node"],
        },
    )
    assert detail["redaction_verified"] is True
    assert "query.max-memory-per-node" in detail["content"]


def test_summary_surfaces_node_drift(call: Any) -> None:
    payload = call(
        "get_config_summary",
        {"cluster": "drifted-cluster", "scopes": ["node_identity"]},
    )
    drifted = [p for p in payload["properties"] if not p["consistent_across_nodes"]]
    assert drifted
    assert "worker-02.corp.com" in drifted[0]["differing_nodes"]


def test_diff_leads_with_unexpected_differences(call: Any) -> None:
    payload = call(
        "diff_clusters",
        {
            "cluster_a": "clean-cluster",
            "cluster_b": "drifted-cluster",
            "scopes": ["memory"],
        },
    )
    assert payload["differences"][0]["classification"] == "unexpected"


def test_an_unknown_cluster_is_an_actionable_tool_error(call: Any) -> None:
    with pytest.raises(ToolError, match="list_clusters"):
        call("run_rules", {"cluster": "typo", "scopes": ["memory"]})


def test_an_invalid_scope_is_rejected_by_the_schema(call: Any) -> None:
    with pytest.raises(Exception, match="scopes"):
        call("run_rules", {"cluster": "clean-cluster", "scopes": ["memry"]})
