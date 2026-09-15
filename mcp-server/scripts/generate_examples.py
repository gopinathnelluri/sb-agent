"""Regenerate examples.md from the live tools.

Documentation that is typed by hand drifts from the code the first time a
message is reworded. This runs the real MCP tools against fixture data and
writes the result, so the examples in the doc are always output the server
actually produced.

    uv run python scripts/generate_examples.py          # write examples.md
    uv run python scripts/generate_examples.py --check  # fail if stale (CI)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from adapters.local_backup import LocalBackupRepository
from core.analysis.models import QueryInfo, QueryState
from core.analysis.operators import parse_operator_summaries
from core.analysis.service import QueryAnalysisService
from core.service import ConfigValidationService
from mcp_server.server import build_server
from tests.conftest import FROZEN_NOW
from tests.fakes import FakeQueryRepository

OUTPUT = Path(__file__).resolve().parent.parent / "examples.md"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "backups"

SQL_BAD = """SELECT c.region, count(*) AS orders, sum(o.amount) AS revenue
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE year(o.order_date) = 2026
  AND c.segment = 'enterprise'
GROUP BY c.region"""

SQL_FIXED = """SELECT c.region, count(*) AS orders, sum(o.amount) AS revenue
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.order_date >= DATE '2026-01-01'
  AND o.order_date < DATE '2027-01-01'
  AND c.segment = 'enterprise'
GROUP BY c.region"""

# A payload in the shape Trino documents. Written from the spec rather than
# captured from this fleet, which is why the parser is deliberately tolerant.
OPERATOR_PAYLOAD = json.dumps(
    [
        {
            "operatorType": "ScanFilterAndProjectOperator",
            "stageId": 4,
            "operatorId": 0,
            "physicalInputPositions": 1_800_000_000,
            "physicalInputDataSize": "3.9TB",
            "outputPositions": 2_400_000,
        },
        {
            "operatorType": "BroadcastExchangeOperator",
            "stageId": 3,
            "operatorId": 1,
            "outputDataSize": "6.2GB",
        },
        {
            "operatorType": "HashJoinOperator",
            "stageId": 3,
            "operatorId": 2,
            "inputPositions": 2_400_000,
            "outputPositions": 410_000_000,
            "addInputWall": "17.20m",
            "spilledDataSize": "48GB",
        },
    ]
)

QUERIES: dict[str, QueryInfo] = {
    "20260910_093412_00042_x7k2m": QueryInfo(
        query_id="20260910_093412_00042_x7k2m",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=SQL_BAD,
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=1_284_000,
        queued_ms=8_000,
        cpu_ms=3_900_000,
        total_bytes_scanned=4_400_000_000_000,
        total_rows=18_400_000_000,
        output_rows=6,
        peak_memory_bytes=42_000_000_000,
    ),
    "20260910_101500_00311_p4nq8": QueryInfo(
        query_id="20260910_101500_00311_p4nq8",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=(
            "SELECT product_id, sum(qty) FROM sales "
            "WHERE sale_date >= DATE '2026-09-01' GROUP BY product_id"
        ),
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=902_000,
        queued_ms=847_000,
        total_bytes_scanned=2_100_000_000,
        output_rows=1_240,
    ),
    # The same SQL a month earlier, when the table was smaller. The baseline
    # for "it used to be fast".
    "20260812_090015_00007_bb3ll": QueryInfo(
        query_id="20260812_090015_00007_bb3ll",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=SQL_BAD,
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=94_000,
        queued_ms=6_000,
        total_bytes_scanned=210_000_000_000,
        output_rows=6,
    ),
    # The same query after applying the suggested rewrite.
    "20260910_101820_00061_ww8dd": QueryInfo(
        query_id="20260910_101820_00061_ww8dd",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=SQL_FIXED,
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=38_000,
        queued_ms=7_000,
        total_bytes_scanned=9_200_000_000,
        output_rows=6,
    ),
    # The same shape of problem, seen with per-operator detail.
    "20260912_112233_00901_vv4rr": QueryInfo(
        query_id="20260912_112233_00901_vv4rr",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=(
            "SELECT c.region, count(*) FROM orders o "
            "JOIN customers c ON o.region_code = c.region_code "
            "WHERE o.order_date >= DATE '2026-09-01' GROUP BY c.region"
        ),
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=1_310_000,
        queued_ms=9_000,
        total_bytes_scanned=4_200_000_000_000,
        output_rows=12,
        operators=parse_operator_summaries(OPERATOR_PAYLOAD),
    ),
    # Failed before finishing -- its numbers describe a partial run.
    "20260911_141203_00518_kk9ww": QueryInfo(
        query_id="20260911_141203_00518_kk9ww",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FAILED,
        sql=(
            "SELECT o.id, c.name, p.description FROM orders o "
            "JOIN customers c ON o.customer_id = c.id "
            "JOIN products p ON o.product_id = p.id "
            "WHERE o.order_date >= DATE '2026-01-01'"
        ),
        user="j.okafor",
        session_catalog="hive",
        session_schema="sales",
        error_code="EXCEEDED_MEMORY_LIMIT",
        error_message=("Query exceeded per-node memory limit of 24GB"),
        elapsed_ms=412_000,
        queued_ms=3_000,
        total_bytes_scanned=880_000_000_000,
        output_rows=0,
        peak_memory_bytes=48_000_000_000,
    ),
    # Ran out of memory because of a join with no condition.
    "20260911_160440_00733_mm2bb": QueryInfo(
        query_id="20260911_160440_00733_mm2bb",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=(
            "SELECT r.region_name, s.store_name FROM regions r, stores s "
            "WHERE r.active = true"
        ),
        user="j.okafor",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=1_840_000,
        queued_ms=12_000,
        total_bytes_scanned=140_000_000_000,
        output_rows=94_000_000,
        spilled_bytes=61_000_000_000,
    ),
    # A thin audit row: the history recorded no SQL for this one.
    "20260908_071510_00044_tt5hh": QueryInfo(
        query_id="20260908_071510_00044_tt5hh",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=None,
        user="s.mehta",
        elapsed_ms=688_000,
        queued_ms=14_000,
        total_bytes_scanned=2_900_000_000_000,
        output_rows=41,
    ),
    "20260910_110022_00877_zz1aa": QueryInfo(
        query_id="20260910_110022_00877_zz1aa",
        cluster="prod-analytics",
        source="audit:sep_event_logger",
        state=QueryState.FINISHED,
        sql=(
            "SELECT id, total FROM orders "
            "WHERE order_date >= DATE '2026-09-01' LIMIT 500"
        ),
        user="a.patel",
        session_catalog="hive",
        session_schema="sales",
        elapsed_ms=4_100,
        queued_ms=180,
        total_bytes_scanned=61_000_000,
        output_rows=500,
    ),
}


def _server() -> Any:
    # Pin the clock. The config fixtures carry fixed backup timestamps, so with
    # a live clock `backup_age_days` grows every day and the generated file
    # would go stale overnight -- `--check` failing in CI with no code change.
    # Frozen just after the fixture dates, which is also when a backup is
    # genuinely fresh enough for a healthy cluster to report complete coverage.
    config = ConfigValidationService(
        LocalBackupRepository(FIXTURES), clock=lambda: FROZEN_NOW
    )
    queries = QueryAnalysisService(
        FakeQueryRepository(
            queries=QUERIES,
            partitions={
                "orders": frozenset({"order_date"}),
                "sales": frozenset({"sale_date"}),
                # regions and stores are small and unpartitioned, which is why
                # the cross-join finding stays `suspected` rather than confirmed.
            },
            retention=30,
        )
    )
    return build_server(config, queries)


def _call(name: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(_server().call_tool(name, args))
    return result.structured_content


def _wrap(text: str, indent: str = "", hanging: str | None = None) -> str:
    """Wrap to 78 columns, optionally with a hanging indent for list items."""
    return "\n".join(
        textwrap.fill(
            line,
            78,
            initial_indent=indent,
            subsequent_indent=hanging if hanging is not None else indent,
        )
        for line in text.splitlines()
    )


def _finding_block(finding: dict[str, Any]) -> list[str]:
    lines = [
        f"[{finding['severity'].upper()}]  domain: {finding['domain']}  "
        f"owner: {finding['owner']}"
        f"{'  (root cause)' if finding.get('is_root_cause') else ''}",
        _wrap(finding["summary"]),
    ]
    if finding.get("rationale"):
        lines += ["", _wrap("why: " + finding["rationale"], "", "     ")]
    if finding.get("next_step"):
        lines += ["", _wrap("what to do: " + finding["next_step"], "", "     ")]
    return lines


def _scenario(
    title: str, question: str, tool_call: str, payload: dict[str, Any]
) -> str:
    out = [f"### {title}", "", "**The analyst asks:**", "", f"> {question}", ""]
    out += ["**The agent calls:**", "", "```python", tool_call, "```", ""]
    out += ["**The tool returns:**", "", "```"]

    if not payload["found"]:
        out += ["found: false", "", _wrap(payload["not_found_reason"])]
    else:
        coverage = payload["coverage"]
        out.append(
            f"found: true   findings: {len(payload['findings'])}   "
            f"coverage.complete: {coverage['complete']}"
        )
        for finding in payload["findings"]:
            out.append("")
            out += _finding_block(finding)
        if payload.get("suggested_sql"):
            out += ["", "suggested_sql:", ""]
            out.append(textwrap.indent(payload["suggested_sql"], "  "))
            for rewrite in payload["rewrites"]:
                out.append(f"  equivalence: {rewrite['equivalence']}")
                if rewrite.get("caveat"):
                    out.append(_wrap("  caveat: " + rewrite["caveat"]))
        if coverage["blind_spots"]:
            out += ["", "coverage.blind_spots:"]
            out += [
                _wrap(f"- {spot}", "  ", "    ") for spot in coverage["blind_spots"]
            ]
    out += ["```", ""]
    return "\n".join(out)


def _comparison_scenario(
    title: str, question: str, tool_call: str, payload: dict[str, Any]
) -> str:
    out = [f"### {title}", "", "**The analyst asks:**", "", f"> {question}", ""]
    out += ["**The agent calls:**", "", "```python", tool_call, "```", ""]
    out += ["**The tool returns:**", "", "```"]
    out.append(f"same_sql: {payload['same_sql']}")
    out += ["", _wrap(payload["verdict"])]

    moved = [c for c in payload["changes"] if c["summary"]]
    if moved:
        out += ["", "what changed:"]
        for change in moved:
            out.append(
                _wrap(f"- [{change['direction']}] {change['summary']}", "  ", "    ")
            )

    for label, key in (
        ("problems that went away:", "findings_only_in_a"),
        ("problems that appeared:", "findings_only_in_b"),
        ("problems present in both:", "findings_in_both"),
    ):
        entries = payload[key]
        if entries:
            out += ["", label]
            for finding in entries:
                out.append(_wrap(f"- {finding['summary']}", "  ", "    "))

    if payload["notes"]:
        out += ["", "notes:"]
        out += [_wrap(f"- {note}", "  ", "    ") for note in payload["notes"]]
    out += ["```", ""]
    return "\n".join(out)


def _config_finding_lines(finding: dict[str, Any]) -> list[str]:
    evidence = finding["evidence"][0]
    where = "/".join(
        part for part in (evidence["role"], evidence["node"], evidence["file"]) if part
    )
    lines = [
        "",
        f"[{finding['severity'].upper()}]  {finding['rule_id']}  "
        f"domain: {finding['domain']}  owner: {finding['owner']}",
        _wrap(finding["summary"]),
        f"  actual:   {finding['actual']}",
        f"  expected: {finding['expected']}",
        f"  evidence: {where}"
        + (f" line {evidence['line']}" if evidence["line"] else ""),
    ]
    if finding.get("rationale"):
        lines += ["", _wrap("  why: " + finding["rationale"], "", "        ")]
    if finding.get("next_step"):
        lines += ["", _wrap("  what to do: " + finding["next_step"], "", "        ")]
    return lines


def _config_scenario(
    title: str,
    question: str,
    tool_call: str,
    payload: dict[str, Any],
    *,
    limit: int | None = None,
    trailer: list[str] | None = None,
) -> str:
    out = [f"### {title}", "", "**The user asks:**", "", f"> {question}", ""]
    out += ["**The agent calls:**", "", "```python", tool_call, "```", ""]
    out += ["**The tool returns:**", "", "```"]
    coverage = payload["coverage"]
    out.append(
        f"findings: {len(payload['findings'])}   "
        f"coverage.complete: {coverage['complete']}   "
        f"rules_evaluated: {coverage['rules_evaluated']}"
    )
    shown = payload["findings"][:limit] if limit else payload["findings"]
    for finding in shown:
        out += _config_finding_lines(finding)
    remaining = len(payload["findings"]) - len(shown)
    if remaining > 0:
        out += ["", f"... and {remaining} more"]
    if coverage["blind_spots"]:
        out += ["", "coverage.blind_spots:"]
        out += [_wrap(f"- {spot}", "  ", "    ") for spot in coverage["blind_spots"]]
    out += ["```", ""]
    if trailer:
        out += trailer + [""]
    return "\n".join(out)


def _config_inventory() -> str:
    payload = _call("list_clusters", {})
    out = [
        "### Which clusters can I ask about?",
        "",
        "**The user asks:**",
        "",
        "> What clusters do you know about?",
        "",
        "**The agent calls:**",
        "",
        "```python",
        "list_clusters()",
        "```",
        "",
        "**The tool returns:**",
        "",
        "```",
    ]
    for row in payload["result"]:
        out.append(
            f"{row['name']:16} sep_version: {row['sep_version']} "
            f"({row['sep_version_source']})"
        )
        out.append(
            f"{'':16} backed up {row['backup_age_days']} day(s) ago   "
            f"nodes: {row['node_counts']}"
        )
    out += ["```", ""]
    out += [
        "Start here when a cluster is named that you have not seen -- every other",
        "tool takes a name from this list.",
        "",
        "Two fields earn their place. `sep_version_source` distinguishes a version",
        "read from the backup manifest from one that was merely hinted: the rule",
        "catalog is version-gated, so a cluster whose version is unknown has rules",
        "skipped. And `backup_age_days` matters because findings describe the",
        "config as of the backup, not as of now.",
        "",
        "---",
        "",
    ]
    return "\n".join(out)


def _config_misconfigured() -> str:
    payload = _call(
        "run_rules",
        {"cluster": "drifted-cluster", "scopes": ["memory", "jvm", "node_identity"]},
    )
    return _config_scenario(
        "A cluster with problems",
        "Is `drifted-cluster` set up correctly?",
        'run_rules(\n    cluster="drifted-cluster",\n'
        '    scopes=["memory", "jvm", "node_identity"],\n)',
        payload,
        limit=4,
        trailer=[
            "Every finding cites a file, a node, and a line. That is what makes the",
            "answer checkable rather than merely plausible -- and it is why",
            "`SEP-NODE-001` can name the one worker out of three that drifted",
            "rather than reporting that something, somewhere, is inconsistent.",
            "",
            "Note `actual` and `expected` are both present on every finding. The",
            "reader does not have to take the summary on trust; they can see the",
            "value that was found and the value the rule wanted.",
            "",
            "---",
        ],
    )


def _config_healthy() -> str:
    payload = _call(
        "run_rules",
        {"cluster": "clean-cluster", "scopes": ["memory", "jvm", "node_identity"]},
    )
    return _config_scenario(
        "A healthy cluster",
        "Anything wrong with `clean-cluster`?",
        'run_rules(\n    cluster="clean-cluster",\n'
        '    scopes=["memory", "jvm", "node_identity"],\n)',
        payload,
        trailer=[
            "Nothing found, and `coverage.complete` is true with 24 rules actually",
            "evaluated -- so the silence means the cluster is fine, not that the",
            "checks could not run. Report it as a clean result.",
            "",
            "Contrast this with the next scenario, where an empty-looking result",
            "would mean something very different.",
            "",
            "---",
        ],
    )


def _config_missing_settings() -> str:
    payload = _call(
        "run_rules",
        {"cluster": "bare-cluster", "scopes": ["memory", "jvm", "node_identity"]},
    )
    return _config_scenario(
        "Settings that were never configured",
        "Check `bare-cluster` for me.",
        'run_rules(\n    cluster="bare-cluster",\n'
        '    scopes=["memory", "jvm", "node_identity"],\n)',
        payload,
        limit=3,
        trailer=[
            "Two things to notice.",
            "",
            "First, `actual: not set`. A property nobody configured is its own",
            "finding, not a silent pass -- many real misconfigurations are a",
            "setting left at a default that is wrong for the fleet.",
            "",
            "Second, only 8 rules ran here against 24 on the other clusters, and",
            "`coverage.complete` is false. Rules that needed a value this cluster",
            "does not have could not run at all. Tell the user that: an incomplete",
            "audit reported as a clean bill of health is the worst outcome",
            "available.",
            "",
            "---",
        ],
    )


def _config_file_security() -> str:
    payload = _call(
        "run_rules",
        {"cluster": "drifted-cluster", "scopes": ["file_security"]},
    )
    return _config_scenario(
        "File permissions, including on files never backed up",
        "Are the config files on `drifted-cluster` locked down properly?",
        'run_rules(\n    cluster="drifted-cluster",\n    scopes=["file_security"],\n)',
        payload,
        trailer=[
            "These findings come from the `*.metadata.json` files the pipeline",
            "writes beside each config, not from the config contents. A file can",
            "hold entirely correct settings and still be a finding if every",
            "account on the host can read it.",
            "",
            "The keytab is the case worth understanding. Its *contents* were never",
            "backed up -- a Kerberos keytab is a credential and has no business in",
            "an object store. But its metadata was, so we can see it exists and is",
            "world-readable, and say so, without the secret ever leaving the host.",
            "",
            "That is what the note on the finding means: the permissions were",
            "recorded from the host even though the file itself is absent from the",
            "backup.",
            "",
            "---",
        ],
    )


def _config_drift() -> str:
    payload = _call(
        "get_config_summary",
        {"cluster": "drifted-cluster", "scopes": ["node_identity", "jvm"]},
    )
    drifted = [p for p in payload["properties"] if not p["consistent_across_nodes"]]
    out = [
        "### How is this cluster actually configured?",
        "",
        "**The user asks:**",
        "",
        "> What is `node.environment` set to across `drifted-cluster`?",
        "",
        "**The agent calls:**",
        "",
        "```python",
        'get_config_summary(\n    cluster="drifted-cluster",\n'
        '    scopes=["node_identity", "jvm"],\n)',
        "```",
        "",
        "**The tool returns:**",
        "",
        "```",
        f"properties: {len(payload['properties'])}   "
        f"anomalies: {len(payload['anomalies'])}",
    ]
    for prop in drifted:
        out += [
            "",
            f"{prop['key']}  ({prop['role']})",
            f"  value:                    {prop['value']}",
            "  consistent_across_nodes:  False",
            f"  differing_nodes:          {prop['differing_nodes']}",
        ]
    if payload["anomalies"]:
        out += ["", "anomalies:"]
        out += [_wrap(f"- {a}", "  ", "    ") for a in payload["anomalies"]]
    out += ["```", ""]
    out += [
        "This tool describes configuration; it does not judge it. Use it to answer",
        '"what is this set to" and to ground yourself before or after running',
        "rules.",
        "",
        "`differing_nodes` is the field that matters. Reporting only the majority",
        "value would hide the single node that drifted, which is usually the",
        "actual problem.",
        "",
        "---",
        "",
    ]
    return "\n".join(out)


def _config_diff() -> str:
    payload = _call(
        "diff_clusters",
        {
            "cluster_a": "clean-cluster",
            "cluster_b": "drifted-cluster",
            "scopes": ["memory", "jvm"],
        },
    )
    out = [
        "### It works on one cluster but not another",
        "",
        "**The user asks:**",
        "",
        "> Queries run fine on `clean-cluster` but keep failing on",
        "> `drifted-cluster`. What is different?",
        "",
        "**The agent calls:**",
        "",
        "```python",
        'diff_clusters(\n    cluster_a="clean-cluster",\n'
        '    cluster_b="drifted-cluster",\n    scopes=["memory", "jvm"],\n)',
        "```",
        "",
        "**The tool returns:**",
        "",
        "```",
        f"differences: {len(payload['differences'])}",
        "",
        f"{'classification':13} {'property':34} clean-cluster -> drifted-cluster",
    ]
    for diff in payload["differences"]:
        out.append(
            f"{diff['classification']:13} {diff['key']:34} "
            f"{diff['value_a']} -> {diff['value_b']}"
        )
    out += ["```", ""]
    out += [
        "Differences are classified so the real divergence leads. `unexpected`",
        "means the two clusters disagree on something that normally matches --",
        "start there. `expected` covers values that differ by design, such as",
        "hostnames and node ids; mention them only if asked.",
        "",
        "Without that classification the result would be dominated by differences",
        "nobody cares about.",
        "",
        "One limit worth stating to the user: this reports differences, not",
        "correctness. A property can match on both clusters and be wrong on both.",
        "Run `run_rules` against each to find that out.",
        "",
    ]
    return "\n".join(out)


def render() -> str:
    sections: list[str] = [
        "# Examples",
        "",
        "Every block below is real output from the MCP tools, generated by",
        "`scripts/generate_examples.py` against fixture data. Regenerate after",
        "changing a message, a threshold, or a rule:",
        "",
        "```bash",
        "uv run python scripts/generate_examples.py",
        "```",
        "",
        "The agent's prose is illustrative -- the tools return structure, and",
        "whatever agent calls them writes the sentences. The point of these",
        "examples is what the tools supply, and what a reader can do with it.",
        "",
        "---",
        "",
        "## Use case 2 -- Query-plan Analyzer",
        "",
        "**A separate use case from the config auditor below.** Different input",
        "(a query id, not a cluster scope), different question, different answer",
        "shape. The two share the shape of a finding -- severity, owner, evidence",
        "-- but not its taxonomy: query findings carry a `QueryDomain` naming an",
        "aspect of one execution, config findings carry a `Scope` naming a domain",
        "of cluster configuration.",
        "",
        "Given a cluster and a query id, explain why a query was slow and, where"
        "it can be proven safe, offer a rewrite. Reads recorded statistics; it",
        "re-runs nothing, so it is fast.",
        "",
        "Two independent signals are combined. Runtime statistics say what the",
        "query *did*; the SQL text says what looks *suspicious*. Neither is",
        "conclusive alone -- a full scan is correct when you want the whole table,",
        "and a function in a WHERE clause is harmless on an unpartitioned one. A",
        "finding marked `root cause` is one where both agreed.",
        "",
    ]

    q1 = "20260910_093412_00042_x7k2m"
    sections.append(
        _scenario(
            "The query is the problem",
            f"My query took 21 minutes this morning. Query id `{q1}`. What happened?",
            f'analyze_query(cluster="prod-analytics", query_id="{q1}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q1}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> Your query scanned 4TB to return 6 rows, and the cause is in the WHERE",
        "> clause: `year(o.order_date) = 2026`. `order_date` is the partition",
        "> column on `orders`, so wrapping it in `year()` stops Starburst skipping",
        "> partitions -- it reads all of them and filters afterwards. The rewrite",
        "> below returns the same rows. One caveat: if `order_date` is TIMESTAMP",
        "> WITH TIME ZONE, the boundaries use your session timezone.",
        "",
        "Note why this is stated firmly rather than hedged: the runtime signal",
        "(huge scan, tiny result) and the text signal (partition column wrapped in",
        "a function) agree. Without partition metadata to confirm `order_date` is",
        "a partition column, the same pattern would be reported as `suspected` and",
        "no root cause would be claimed.",
        "",
        "---",
        "",
    ]

    q2 = "20260910_101500_00311_p4nq8"
    sections.append(
        _scenario(
            "The query is not the problem",
            f"This one took 15 minutes. `{q2}`. Same issue?",
            f'analyze_query(cluster="prod-analytics", query_id="{q2}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q2}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> No -- and this one isn't yours. The query spent 14.1 of 15 minutes",
        "> waiting in the queue; actual execution was 55 seconds. Nothing in your",
        "> SQL will change that. If it keeps happening, worth raising with",
        "> your cluster owner -- it may be under-provisioned for the",
        "> workload.",
        "",
        'This is why findings carry `owner`. A large share of "slow query"',
        "reports are queueing, and someone who cannot tell their problem from",
        "the cluster's will optimise a query that was already fine.",
        "",
        "Note the phrasing: `cluster_owner`, not `platform_team`. Where a team",
        "runs its own cluster the person who can fix this is a colleague --",
        "possibly the reader. Telling them to escalate to a platform team sends",
        "them looking outside their own team for someone already in it.",
        "",
        "---",
        "",
    ]

    old, new = "20260812_090015_00007_bb3ll", "20260910_093412_00042_x7k2m"
    sections.append(
        _comparison_scenario(
            "It was fast last month -- what changed?",
            f"`{new}` took 21 minutes today. The same query ran in about 90 "
            f"seconds on the 12th (`{old}`). Nothing in it changed.",
            f'compare_queries(\n    cluster="prod-analytics",\n'
            f'    query_id_a="{old}",   # the baseline\n'
            f'    query_id_b="{new}",   # the run being judged\n)',
            _call(
                "compare_queries",
                {
                    "cluster": "prod-analytics",
                    "query_id_a": old,
                    "query_id_b": new,
                },
            ),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> You are right that the query did not change -- both runs used exactly",
        "> the same SQL. What changed is how much data it reads: 210GB on the",
        "> 12th, 4TB today. The `orders` table has grown, and because the WHERE",
        "> clause wraps `order_date` in `year()`, Starburst cannot skip",
        "> partitions and has to read all of it. That was survivable when the",
        "> table was smaller; it is not now.",
        "",
        "This is the comparison a single analysis cannot produce. On its own,",
        '"4TB scanned" invites the question "is that a lot?" -- the answer is a',
        "judgement against a fixed threshold. Next to the same query's own",
        "history it is not a judgement at all, it is a measurement.",
        "",
        "`same_sql: True` is what makes the answer safe to state. The user",
        "changed nothing, so nothing they did caused this, and telling them",
        "otherwise would send them looking in the wrong place.",
        "",
        "---",
        "",
    ]

    fixed = "20260910_101820_00061_ww8dd"
    sections.append(
        _comparison_scenario(
            "Did the rewrite actually help?",
            f"I applied the rewrite you suggested and re-ran it -- "
            f"`{fixed}`. Did it work?",
            f'compare_queries(\n    cluster="prod-analytics",\n'
            f'    query_id_a="{new}",   # before the rewrite\n'
            f'    query_id_b="{fixed}",   # after\n)',
            _call(
                "compare_queries",
                {
                    "cluster": "prod-analytics",
                    "query_id_a": new,
                    "query_id_b": fixed,
                },
            ),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> Yes -- 33 times faster, 21 minutes down to 38 seconds, and it reads",
        "> 9.2GB instead of 4TB. The partition-pruning problem is gone, and the",
        "> query returns the same six rows it did before.",
        "",
        "This closes the loop the analyser exists for: it suggested a rewrite,",
        "the user applied it, and this confirms the suggestion was right rather",
        "than leaving them to judge. Offer it whenever someone has acted on a",
        "rewrite -- a suggestion nobody verifies is a suggestion nobody trusts",
        "the second time.",
        "",
        "Note what is checked beyond the timing: `output_rows` is unchanged, so",
        "the rewrite really did preserve the result. A faster query that returns",
        "different rows is not an improvement.",
        "",
        "---",
        "",
    ]

    q_ops = "20260912_112233_00901_vv4rr"
    sections.append(
        _scenario(
            "Which step was actually slow",
            f"`{q_ops}` took 22 minutes. The WHERE clause looks fine to me.",
            f'analyze_query(cluster="prod-analytics", query_id="{q_ops}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q_ops}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> The filter is fine -- the problem is the join. It takes in 2.4 million",
        "> rows and produces 410 million, which means `region_code` is not unique",
        "> on one side, so each row is matching many others. That is where the 17",
        "> minutes went, and it is also why 48GB spilled to disk.",
        ">",
        "> Two things worth checking: whether `region_code` alone is really the",
        "> key you meant to join on, and whether `customers` has duplicate rows",
        "> per region. Counting them is a quick way to confirm.",
        "",
        "Compare this with the earlier scenarios. Those said a query read too",
        "much and took too long -- true, and about the whole query. This one",
        "names the step, the row counts going in and out, and the stage it sits",
        "in. That is the difference per-operator statistics make: a symptom",
        "becomes a location.",
        "",
        "It is also why the findings say `stage 3` rather than just `query`.",
        "Evidence that points somewhere specific is evidence someone can go and",
        "check.",
        "",
        "---",
        "",
    ]

    q_failed = "20260911_141203_00518_kk9ww"
    sections.append(
        _scenario(
            "A query that failed",
            f"`{q_failed}` errored out. Was it my query or the cluster?",
            f'analyze_query(cluster="prod-analytics", query_id="{q_failed}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q_failed}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> It ran out of memory -- it asked for more than the 24GB a single query",
        "> is allowed on one node. Worth noting the timing and data-volume figures",
        "> below describe only the part that ran before it stopped, so they are",
        "> not a fair picture of the query's performance. Fix the failure first,",
        "> then re-run and we can look at speed.",
        "",
        "A failed query is reported before anything else, because its statistics",
        "describe a partial execution. Reading them as performance figures leads",
        "somewhere wrong.",
        "",
        "---",
        "",
    ]

    q_spill = "20260911_160440_00733_mm2bb"
    sections.append(
        _scenario(
            "A query that ran out of memory",
            f"`{q_spill}` took half an hour. It is a simple two-table query.",
            f'analyze_query(cluster="prod-analytics", query_id="{q_spill}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q_spill}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> The two tables are joined with a comma and no ON clause, so every",
        "> region is paired with every store -- 94 million rows out of two small",
        "> tables. That did not fit in memory, so 61GB was written to disk, which",
        "> is why it took so long. Adding the condition that links them should",
        "> bring it back to seconds.",
        "",
        "This is the same two-signal pattern as the partition case, on a",
        "different pair: the runtime evidence is spilling, the text evidence is a",
        "join with no condition. Neither alone would justify naming a cause.",
        "",
        "---",
        "",
    ]

    q_thin = "20260908_071510_00044_tt5hh"
    sections.append(
        _scenario(
            "When the history did not record the SQL",
            f"Can you look at `{q_thin}`?",
            f'analyze_query(cluster="prod-analytics", query_id="{q_thin}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q_thin}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> It read 2.9TB to return 41 rows, which is far more than a result that",
        "> size should need -- usually a filter that is not narrowing the scan.",
        "> The history did not keep this query's SQL, so I cannot point at the",
        "> line responsible. If you still have the query text, send it and I can",
        "> be specific.",
        "",
        "The runtime detectors work on whatever the source recorded, so a thin",
        "row still produces a real finding. What is missing is named rather than",
        "glossed over: without the SQL there is no root cause, only a symptom.",
        "",
        "---",
        "",
    ]

    q3 = "20260910_110022_00877_zz1aa"
    sections.append(
        _scenario(
            "A healthy query",
            f"Anything wrong with `{q3}`?",
            f'analyze_query(cluster="prod-analytics", query_id="{q3}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": q3}),
        )
    )
    sections += [
        "**What the agent might say:**",
        "",
        "> Nothing flagged. Worth noting the history doesn't record spill data and",
        "> carries no per-operator statistics, so skew and join problems could not",
        "> be checked.",
        "",
        "An empty findings list is a real answer, but only when `coverage.complete`",
        "is true. Here it is false, and the caveat belongs in the reply -- silence",
        "because nothing was wrong and silence because little was checked must not",
        "look the same.",
        "",
        "---",
        "",
    ]

    missing = "20260901_000000_00001_aaaaa"
    sections.append(
        _scenario(
            "The query is not in history",
            f"Can you look at `{missing}`?",
            f'analyze_query(cluster="prod-analytics", query_id="{missing}")',
            _call("analyze_query", {"cluster": "prod-analytics", "query_id": missing}),
        )
    )
    sections += [
        "Not an error -- the likely explanation, stated. Retention is the real",
        "analysis window, so it is reported rather than left for the user to",
        "discover.",
        "",
        "---",
        "",
        "## Use case 1 -- Cluster Config Auditor",
        "",
        "**A separate use case from the query analyzer above.** This one asks",
        '"is this cluster configured correctly" and reads config backups; it',
        "knows nothing about any individual query.",
        "",
        "Scope is always explicit; there is no `validate_everything()`.",
        "",
    ]

    sections.append(_config_inventory())
    sections.append(_config_misconfigured())
    sections.append(_config_healthy())
    sections.append(_config_missing_settings())
    sections.append(_config_file_security())
    sections.append(_config_drift())
    sections.append(_config_diff())

    return "\n".join(sections).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if examples.md is out of date",
    )
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(
                "examples.md is out of date. Run: "
                "uv run python scripts/generate_examples.py",
                file=sys.stderr,
            )
            return 1
        print("examples.md is current")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parent.parent)} ({len(rendered)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
