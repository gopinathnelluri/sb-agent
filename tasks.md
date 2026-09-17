# Backlog

What is not built, and what it is waiting on. Ordered by value, not by effort.

Items marked **blocked** need something from outside this repo. Everything
else can be picked up now.

---

## Blocked on a real cluster

### 1. Confirm the `operator_summaries` payload shape — blocked

The four operator-level detectors are built and tested, but against Trino's
*documented* `OperatorStats` shape rather than a payload from this fleet. The
parser accepts three serialisations, both naming conventions, and values as
numbers or strings, so a mismatch loses a detector rather than producing a
wrong answer — but a mismatch would still lose it silently apart from the
`detectors_skipped` entry.

One query settles it permanently. It is the first block in
`mcp-server/scripts/discover_audit_schema.sql`.

*Impact if wrong:* four detectors go quiet. *Effort once confirmed:* minutes.

### 2. Confirm the units of the unlabelled duration columns — blocked

`planning_time`, `execution_time`, `analysis_time`, `scheduled_time` and
`resource_waiting_time` carry no `_ms` suffix, unlike `wall_time_ms` and
`cpu_time_ms`. They are deliberately left unmapped: a Trino `Duration` string
read as an integer count of milliseconds is wrong by three orders of
magnitude, and absent beats wrong.

Second block in the same file. One `typeof()` answers it.

### 3. First live connection — blocked

Nothing has ever connected to a real cluster. The Trino adapter is tested
against an injected connection, so authentication, TLS, the AD functional ID
and the audit catalog's real name have never been exercised together.

Needs: host, port, the audit table's `catalog.schema`, and the env var names
for the credential. The account needs `SELECT` on the audit catalog and
metadata read on the data catalogs — nothing more.

### 4. Validate the backup layout against one real cluster — blocked

`describe_backup_layout` was written from a described layout. Running it
against one real cluster would confirm the role directories, the per-host
paths, and the `.metadata.json` key names in a single call, without reading a
single config file.

---

## Buildable now

### 5. Skew detection

The highest-value detector still missing. One worker doing ten times the work
of its peers is invisible in every total and obvious in a distribution.

Needs the `cpu_time_distribution` and `stages` columns, both mapped and both
currently unparsed. `operator_summaries` aggregates across tasks, so it cannot
answer this — that is why it was left out of the operator work.

### 6. Dynamic filter effectiveness

A dynamic filter that removes almost nothing means the join is not helping the
scan, which is worth knowing and cheap to check. Left out because the field
names vary enough across Trino versions that building it before item 1 would
be guessing twice over.

### 7. Estimated versus actual rows, from `plan_node_stats_and_costs`

The single best signal Trino produces and we do not read. The optimizer
records what it *expected* each plan node to produce alongside what it
*actually* did. A large divergence is the definitive evidence for stale
statistics — far stronger than inferring it from scan volume, and it turns
"maybe run ANALYZE" into "run ANALYZE on this table, here is why".

### 8. Aggregate and fleet views

Every question today is about one query or one cluster. Three that are not:

- *Which of my queries is worst?* — removes the need for a query id
- *What changed on this cluster this week?*
- *Am I the only one hitting this?* — the difference between "you are doing
  something wrong" and "this cluster is misconfigured for everyone"

The third is the most valuable and the most awkward: it turns one user's
problem into evidence their cluster owner can act on.

### 9. Join config auditing to query behaviour

The two use cases keep their taxonomies apart, correctly. But a user asking
"why do my queries keep running out of memory" needs a query's peak memory
*and* the cluster's configured limit, and we have both and never join them.

A finding that says "your query wanted 48GB, this cluster allows 24GB, and 340
other queries hit the same limit this month" is worth more than either half.

### 10. Use case 2b: analysing a query that has not run yet

Deferred deliberately. Needs `EXPLAIN` (fast, plan only — never `EXPLAIN
ANALYZE`, which executes) and a way to resolve an unqualified table name.
Resolution matters more than it looks: analysing the wrong table produces a
confident wrong answer, so unqualified names must ask rather than guess.

### 11. `plan_` / `run_explain_analyze`

The only tools that would not be read-only. `EXPLAIN ANALYZE` genuinely
executes the statement, including the write in `EXPLAIN ANALYZE INSERT`. Needs
the `plan_`/`run_` split from `SPEC.md`, `destructive_hint: true`, and SQL
parsing to refuse anything that is not a read.

---

## Smaller

### 12. Threshold tuning against real data

Every threshold is a starting point chosen without evidence: 50% queued, 10x
join growth, 1GB broadcast, 1% scan selectivity. They want calibrating against
a few hundred real queries, and they are already config rather than code.

### 13. Caching

A conversation that calls four tools re-reads and re-parses the same backup
each time. The snapshot cache keys on the backup timestamp, so it works
within one process, but nothing survives a restart. Interacts with the
random-UID constraint: no writable local disk.

### 14. An audit log

Which cluster, which tool, which findings. Cheap, and valuable the first time
someone asks why the agent said something last Tuesday.

---

## Landed

### Per-cluster audit connections — done 2026-09-16

The adapter assumed one master cluster federating the fleet's audit catalogs,
with the cluster name as a `WHERE` filter. It is one catalog per cluster, so
the name selects a connection instead. `table_facts` and `retention_days` now
take a cluster for the same reason — both were fleet-wide questions that are
really per-cluster ones.

`cluster_column` is now unset in the shipped profile, with a comment saying
why. The candidate it used to name, `environment`, holds `node.environment`
("production"), not a cluster name: left in place it would have matched no
rows and reported every query as aged out of retention. That failure would
have looked exactly like a normal empty history, which is the kind of bug that
survives a long time.

Endpoints come from `TRINO_CLUSTER_ENDPOINTS` (JSON) or a mounted file, so the
mapping can be a ConfigMap. Still open: sourcing it from the cluster details
API instead, which already holds each cluster's url beside its environment,
sector and authorised AD group. That removes a mapping someone has to keep in
step with the fleet, and is worth doing before the list grows.

### Automatic historical context in `analyze_query` — done 2026-09-15

`analyze_query` now returns `history` alongside the findings: the same SQL's
most recent earlier run, compared metric by metric, with no second call. It
is the difference between "your query read 4TB" — which invites "is that a
lot?" and is answered by a threshold — and "it read 4TB today and 196GB a
month ago", which is a measurement.

`history.same_sql` carries the weight. When it is true, the user changed
nothing, so whatever moved came from outside their query, and saying so stops
them concluding they broke something they did not.

Best-effort throughout: a query with no earlier runs returns `history: null`,
which is the normal case for ad-hoc work, and a lookup that fails costs the
context rather than the analysis.

Two limits worth knowing. The match is on exact SQL text, so a query that
differs by a literal finds no baseline — that favours scheduled jobs and
dashboards, which send byte-identical SQL and are exactly where "it used to
be fast" gets asked. And the baseline is a single previous run, not a trend;
item 8 is where a distribution would come from.
