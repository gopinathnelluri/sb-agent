# starburst-agent — design spec

## Context

An existing parent agent (LangGraph today, likely Google ADK later)
orchestrates several domain agents. This project is the Starburst/Trino
domain capability, exposed as an MCP server so the parent can call it
regardless of which framework the parent runs on.

The parent owns the LLM and the conversation. This service is pure code:
deterministic tools returning structured findings with evidence.

Two use cases. They are genuinely separate — different inputs, different
questions, different answer shapes — and share only the *shape* of a finding
(severity, owner, evidence), never its taxonomy:

1. **Cluster config auditor** — "is this cluster configured correctly?"
   Reads config backups from COS, evaluates a versioned rule catalog scoped
   to the domains the caller names. Findings are tagged with a `Scope`, a
   domain of cluster configuration.
2. **Query-plan analyzer** — "why was this query slow?" Reads recorded
   statistics for one completed query from the audit catalog, correlates them
   with the query text, and offers a rewrite where one is provably safe.
   Findings are tagged with a `QueryDomain`, an aspect of one execution.

Reusing the config `Scope` enum for query findings would claim a slow query
is a fact about `catalog/*.properties`. It is not. The two taxonomies stay
apart, and a test asserts it.

## Layering

    core/                 pure Python, no frameworks, no LLM
      parsers/            config.properties, jvm.config, node.properties,
                          catalog *.properties, hive-site.xml, ranger policies
      rules/
        engine.py         evaluates catalog against normalized config
        catalog/*.yaml    the rules themselves
      analysis/
        query_info.py     parse coordinator query JSON
        detectors/        one module per finding type
      enrich.py           post-hoc rationale resolution (see below)
      models.py           Finding, Evidence, ConfigSnapshot, QueryReport
    adapters/
      cos.py              IBM COS (S3-compatible) reads
      trino.py            Trino/Starburst REST + SQL
      doc_index.py        optional local precomputed index
    mcp_server/
      server.py           tool definitions, thin
    tests/

## MCP tool surface

### Use case 1 — config auditing

    list_clusters()
      -> [{name, sep_version, backed_up_at, roles: [...]}]

    # NOTE: resolve_scope was dropped. Interpreting free text is the caller's
    # job and it has a model for it; Scope is published as an enum on the
    # tools that take it, with the symptom -> scope table in the description.

    get_config_summary(cluster: str, scope: [str])
      -> {properties: {k: v}, anomalies: [...], sep_version, source_files: [...]}
      # ~20-50 properties, not whole files

    run_rules(cluster: str, scope: [str], context: dict | None = None)
      -> {findings: [Finding], rules_evaluated: int, rules_skipped_version: int}

    get_config_detail(cluster: str, role: str, file: str)
      -> {content: str, redaction_verified: bool}

    diff_clusters(cluster_a: str, cluster_b: str, scope: [str])
      -> {differences: [{property, a, b, role}]}

    search_recommendations(query: str, sep_version: str)
      -> [{text, source_doc, page, score}]
      # optional, flag-gated; only if local index is enabled

### Use case 2 — query analysis

    get_query_info(cluster: str, query_id: str)
      -> {state, elapsed, queued_ms, peak_memory, stages: [...], operators: [...]}

    analyze_query(cluster: str, query_id: str, context: dict | None = None)
      -> {findings: [Finding], query_state, detectors_run: [...],
          detectors_skipped: [...]}

    get_table_stats(cluster: str, catalog: str, schema: str, table: str)
      -> {row_count, columns: [{name, ndv, nulls, size}], stats_stale: bool}

    get_partition_info(cluster: str, table: str)
      -> {partition_count, files, avg_file_bytes, format}

    plan_explain_analyze(cluster: str, sql: str)
      -> {estimated_scan_bytes, estimated_cost, safe_to_run: bool, warnings: [...]}

    run_explain_analyze(cluster: str, sql: str)
      -> {plan_with_stats: str}
      # only after plan_explain_analyze; parent gates approval

### Finding shape

```json
{
  "rule_id": "SEP-MEM-002",
  "severity": "high",
  "property": "query.max-memory-per-node",
  "actual": "40GB",
  "expected": "<= 24GB (30% of 80GB heap)",
  "rationale": "Leaves headroom for non-query JVM allocation.",
  "rationale_source": "rule_catalog",
  "evidence": {"file": "coordinator/config.properties", "line": 27},
  "doc_ref": {"source": "Starburst Tuning Guide", "version": "413+",
              "page": 14, "anchor": "query.max-memory-per-node"}
}
```

`doc_ref` is a pointer, not text. The parent may expand it via its own RAG.
`rationale` always stands alone without expansion.

## Context injection contract

Optional `context` argument, supplied by the parent from its RAG:

```json
{
  "source": "parent_rag",
  "properties": {"query.max-memory-per-node": "recommended <= 30% heap"},
  "passages": [{"text": "...", "doc": "Tuning Guide", "page": 14}],
  "sep_version_hint": "429"
}
```

Permitted effects:

- **Enrich** — better rationale on a finding that already fired
- **Annotate** — flag an uncovered property as `unvalidated_guidance`
- **Hint** — supply SEP version only if not detectable from the config;
  a detected version always wins over a hint

Forbidden: setting a threshold, suppressing a finding, adding one.

Implementation: `run_rules` computes findings with no access to `context`,
then passes the finished list to `core/enrich.py`. Context must not be
importable from the rules engine. Add a test asserting that identical
configs produce identical `rule_id` sets with and without context.

If an injected passage contradicts a rule, emit a low-severity
`guidance_conflict` note rather than preferring either. Usually it means
the catalog is stale against a newer doc.

Rationale resolution order: injected context matching `doc_ref` -> local
index (if enabled) -> inline YAML rationale (always present).

## Config backup layout (read-only, in IBM COS)

    configs/<cluster_name>/coordinator/
    configs/<cluster_name>/worker/
    configs/<cluster_name>/hms/
    configs/<cluster_name>/ranger/

Written by a separate Ansible pipeline that redacts secrets. This service
must **verify** redaction on read — assert no key matching
`*password*|*secret*|*keytab*|*credential*` has a value that isn't a
placeholder. If one does, refuse to return the file and emit a
high-severity finding. Do not trust upstream redaction.

## Scoping

`resolve_scope` maps user intent to a config domain set. A lookup we own,
not something a model invents.

| Intent signal            | Scope                        |
|--------------------------|------------------------------|
| spilling, OOM, memory    | memory, jvm, spill           |
| hive catalog slow        | hive_catalog, hms, s3        |
| cannot access table      | ranger, hms, auth            |
| general / "validate"     | all                          |

Unknown intent -> return the scope list and let the caller pick, rather
than defaulting to `all`.

## Rule catalog format

Checks are typed kinds with named fields, not expressions. Nothing in the
catalog is ever parsed as code, so a YAML edit cannot execute anything and
rule authors need not be trusted like code contributors.

```yaml
- id: SEP-MEM-002
  domain: memory
  applies_to:
    roles: [coordinator, worker]
    sep_version: ">=413"
  property: query.max-memory-per-node
  check:
    kind: max_ratio        # required | equals | one_of | matches
    of: jvm_heap           # max | min | range | max_ratio | min_ratio
    ratio: 0.3
  on_missing: skip         # skip | fail | pass -- absence is its own outcome
  severity: high
  rationale: "Leaves headroom for non-query JVM allocation."
  next_step: "Lower query.max-memory-per-node to at or below 30% of heap."
  doc_ref:
    source_doc: Starburst Tuning Guide
    page: 14
```

Unknown fields are a load-time error. A rule silently disabled by a typo
looks exactly like a healthy cluster, which is the worst failure available.

Version-gated because config properties change across SEP releases. A rule
that does not apply to the cluster's version is skipped, not failed.

Also needed: **cross-file consistency checks** no single-property rule can
express — coordinator vs worker heap mismatch, catalog pointing at an HMS
the config does not match, `node.environment` drift across the fleet.

## Query analysis data sources

Richest source is the coordinator REST endpoint `/v1/query/{queryId}`,
returning full per-stage and per-operator statistics. Supplement with:

- `system.runtime.queries`, `system.runtime.tasks`, `system.runtime.nodes`
- `SHOW STATS FOR <table>` — detects missing stats (most common cause of
  bad join order under the CBO)
- `"<table>$partitions"` (Hive) or `"<table>$files"` / `$manifests` (Iceberg)
- `EXPLAIN`; `EXPLAIN ANALYZE` only behind the `plan_`/`run_` gate

**Retention gotcha:** completed query info is purged from the coordinator per
`query.max-history` / `query.min-expire-age`, so the live REST endpoint is not
a usable primary source — users report slow queries hours later.

**Resolved:** the primary source is a master cluster federating each cluster's
audit catalog, read over SQL. That is also the most upgrade-resilient option
available, because the schema is ours: `/v1/query/{queryId}` is documented as
internal and changes between releases with no deprecation cycle. Column names
live in `core/analysis/profiles/*.yaml` so a schema change is a YAML edit.

**Still open:** whether the audit table carries a full per-operator statistics
payload. If it does, all nine detectors are buildable. If it carries only
query-level aggregates, four are — see the detector table below.

## Detectors

Each emits zero or more `Finding` objects with concrete evidence.

| Finding                    | Signal                                                    |
|----------------------------|-----------------------------------------------------------|
| No partition pruning       | scanned partitions ~= total; predicate wraps column in a function or casts |
| Exploding join             | join operator output rows >> input rows                    |
| Bad broadcast              | broadcast-side input bytes over threshold                  |
| Missing stats              | `SHOW STATS` returns null row_count / NDV                  |
| Skew                       | stage max task wall time / p50 > ~5x                       |
| Small files                | bytes scanned / files scanned yields tiny average          |
| Spilling                   | non-zero spilled bytes                                     |
| Not the query's fault      | queued time dominates elapsed -> cluster contention        |
| Ineffective dynamic filter | dynamic filter stats show low row reduction                |

Thresholds live in config, not hardcoded, so they can be tuned per fleet.

The "not the query's fault" detector matters disproportionately. A large
share of "slow query" complaints are queueing or a busy cluster; an
analyzer that always blames the SQL loses user trust fast.

## Loop ownership

`analyze_query` runs a **deterministic internal loop**: fetch query info,
then conditionally fetch table stats / partition info / task stats based
on which detectors fired. Multi-step investigation with no LLM inside,
because the branching is rule-driven ("skew detected -> fetch task stats").

Use case 1 stays parent-driven: the parent calls `resolve_scope`, then
`run_rules`, then `get_config_detail` only if a finding needs it.

Revisit if a typical config question needs more than ~4 round trips, or
if large intermediates are being pulled into the parent's context and
discarded.

## Open questions to resolve before building

- Starburst Enterprise (Insights available) or open-source Trino?
- SEP version(s) in the fleet — determines which rules apply
- Auth to the coordinator: OAuth2, LDAP, mTLS, JWT?
- Does the OpenShift namespace have egress to the coordinators and the
  COS endpoint? NetworkPolicy is a common day-one blocker.
- Where do the recommendation PDFs live, and can they be committed?
- Is the local doc index worth building, or will parent RAG cover it?
  (Default: skip it initially; inline rationale is sufficient.)

## Build order

1. `core/models.py` + `core/parsers/` with fixtures. No network.
2. `core/rules/` engine + first catalog domain (memory), tests green.
3. `core/enrich.py` + the context-independence test.
4. `adapters/cos.py` with redaction verification.
5. `mcp_server/` exposing `resolve_scope`, `run_rules`, `get_config_detail`.
6. Container + OpenShift manifests; verify random-UID startup.
7. Use case 2: `core/analysis/` detectors against saved query JSON fixtures
   before wiring the live Trino adapter.

Do not start use case 2 until use case 1 runs end to end against a real
cluster backup.
