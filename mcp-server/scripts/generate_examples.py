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
import sys
import textwrap
from pathlib import Path
from typing import Any

from adapters.local_backup import LocalBackupRepository
from core.analysis.models import QueryInfo, QueryState
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
    if finding.get("next_step"):
        lines.append(_wrap("-> " + finding["next_step"]))
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
    if finding.get("next_step"):
        lines.append(_wrap("-> " + finding["next_step"]))
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
        "> SQL will change that. If it keeps happening, ask your platform team",
        "> whether the cluster is under-provisioned.",
        "",
        'This is why findings carry `owner`. A large share of "slow query"',
        "reports are queueing, and an analyst who cannot tell their problem from",
        "the platform's will optimise a query that was already fine.",
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
