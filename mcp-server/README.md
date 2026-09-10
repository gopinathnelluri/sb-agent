# starburst-agent

MCP server exposing Starburst/Trino cluster config validation as deterministic
tools, for a parent LangGraph agent to call. Design lives in `SPEC.md`;
conventions in `CLAUDE.md`.

This service contains no LLM. The parent owns the model and writes the prose;
we return structured findings with evidence.

## Run it

```bash
uv sync
uv run pytest

# serve over stdio against local fixtures
uv run python -m mcp_server --backend local --root tests/fixtures/backups

# serve over HTTP against COS
COS_BUCKET=... COS_ENDPOINT=... uv run python -m mcp_server \
    --backend cos --transport streamable-http
```

To read the tool descriptions as the parent's model receives them:

```bash
uv run python scripts/dump_tools.py --schemas
```

Worked examples of every tool -- what a user asks, what the tool returns --
are in [`examples.md`](examples.md). They are generated from the live tools,
so regenerate after changing a message or a threshold:

```bash
uv run python scripts/generate_examples.py          # rewrite examples.md
uv run python scripts/generate_examples.py --check  # fail if stale (CI)
```

## Layout

```
core/          pure logic; no framework, no LLM, no network at import time
  parsers/     properties, jvm.config, site XML -> keys + line numbers
  config/      per-node snapshot, redaction checks, diff classification
  rules/       check kinds, version gating, engine, catalog/*.yaml
  analysis/    query detectors, SQL patterns, rewrites, correlation
    sql/       Trino parsing, anti-patterns, mechanical rewrites
    profiles/  audit-table column mappings (YAML, not code)
  enrich.py    post-hoc enrichment from injected context
  service.py   the one implementation of ConfigService
  ports.py     the Protocols everything else depends on
adapters/      local directory, IBM COS, Trino audit catalog
mcp_server/    thin MCP wrapper; no business logic
```

`mcp_server` imports only `core.ports`. `core/rules` cannot import
`InjectedContext` or `enrich` — asserted in `tests/test_context_independence.py`.

## Adding a rule

Edit a YAML file under `core/rules/catalog/`. No Python required.

```yaml
- id: SEP-MEM-002
  domain: memory
  property: query.max-memory-per-node
  check:
    kind: max_ratio        # required | equals | one_of | matches
    of: jvm_heap           # max | min | range | max_ratio | min_ratio
    ratio: 0.3
  on_missing: skip         # skip | fail | pass
  severity: high
  rationale: >-
    Leaves headroom for non-query JVM allocation.
  applies_to:
    roles: [coordinator, worker]
    sep_version: ">=413"
```

Rules that compare across nodes use `consistency:` instead of `check:`:

```yaml
- id: SEP-NODE-001
  domain: node_identity
  consistency:
    kind: equal_across_nodes    # or equal_across_roles
    property: node.environment
  severity: high
  rationale: >-
    Nodes only join a cluster when node.environment matches exactly.
```

Unknown fields are a load-time error, not an ignored line — a rule disabled by
a typo looks exactly like a healthy cluster. Every rule needs a passing and a
failing fixture under `tests/fixtures/backups/`.

## Backup layout

```
<root>/<cluster>/manifest.json          {"sep_version", "backed_up_at"}
<root>/<cluster>/coordinator/<node>/config.properties
<root>/<cluster>/worker/<node>/jvm.config
<root>/<cluster>/worker/<node>/catalog/hive.properties
```

Per-node directories are what make fleet-drift rules possible. A role
directory holding files directly (no node level) still loads, but drift
detection is unavailable for it.

## Two invariants worth knowing

**An empty findings list is only good news when `coverage.complete` is true.**
Zero rules evaluated always reports incomplete, with a plain-language reason in
`coverage.blind_spots` — an undetectable SEP version must never read as a clean
bill of health.

**Injected context cannot change a finding.** `evaluate()` takes a plain
version string and has no parameter through which context could arrive.
Enrichment is a separate pass over the finished list, and may only reword a
rationale, add an `annotation`, or report a `guidance_conflict`.
