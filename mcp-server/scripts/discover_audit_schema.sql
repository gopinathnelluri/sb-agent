-- Discovery queries for the query-history backing store.
--
-- Run these against the master cluster (the one federating each cluster's
-- audit catalog). The goal is one decision: does the store carry per-stage /
-- per-operator statistics, or only query-level aggregates?
--
-- That answer decides which of the nine detectors in SPEC.md are buildable.
-- Nothing in core/analysis/ should be written before it is known.
--
-- Replace <catalog> and <schema> as you go. Steps run in order; each one
-- narrows the next.

-- ---------------------------------------------------------------------------
-- 1. What catalogs does the master actually see?
--    Confirms the audit catalogs are federated, and whether the *data*
--    catalogs are too (analyze_query needs SHOW STATS against real tables,
--    not just the audit store).
-- ---------------------------------------------------------------------------
SHOW CATALOGS;

-- ---------------------------------------------------------------------------
-- 2. Find the query-history table.
--    Common names: completed_queries, queries, query_history, query_log,
--    insights_queries, event_query_completed.
-- ---------------------------------------------------------------------------
SHOW SCHEMAS FROM <catalog>;
SHOW TABLES FROM <catalog>.<schema>;

-- ---------------------------------------------------------------------------
-- 3. THE DECIDING QUERY -- what columns exist, and what type are they?
--
--    Scan the result for two groups:
--
--    (a) Query-level aggregates. Expect these to be present:
--        query_id, query (SQL text), user, query_state, error_code,
--        create_time / end_time, wall_time_ms, cpu_time_ms, queued_time_ms,
--        peak_memory_bytes, total_bytes, total_rows, output_rows,
--        written_rows, completed_splits
--
--    (b) A large JSON / VARCHAR / CLOB column holding the full statistics
--        payload. THIS is what decides the build. Look for:
--        operator_summaries, operatorSummaries, stage_statistics,
--        plan_node_stats_and_costs, payload, plan, statistics, event
--
--    If (b) is absent, the operator-level detectors cannot be built from
--    this store regardless of how complete (a) is.
-- ---------------------------------------------------------------------------
SELECT column_name, data_type
FROM <catalog>.information_schema.columns
WHERE table_schema = '<schema>'
  AND table_name   = '<table>'
ORDER BY ordinal_position;

-- ---------------------------------------------------------------------------
-- 4. If a candidate payload column exists, inspect one real value.
--    A summary row is a few hundred bytes; a full-statistics payload is
--    typically tens to hundreds of KB. The length alone is a strong signal.
-- ---------------------------------------------------------------------------
SELECT
    query_id,
    length(CAST(<payload_column> AS VARCHAR)) AS payload_len,
    substr(CAST(<payload_column> AS VARCHAR), 1, 2000) AS payload_head
FROM <catalog>.<schema>.<table>
WHERE <payload_column> IS NOT NULL
ORDER BY end_time DESC
LIMIT 3;

-- ---------------------------------------------------------------------------
-- 5. Retention -- this is the real analysis window, and it belongs in the
--    tool response so the parent can tell the user when a query is too old.
-- ---------------------------------------------------------------------------
SELECT
    min(end_time)  AS oldest_retained,
    max(end_time)  AS newest,
    count(*)       AS total_rows,
    count(DISTINCT <cluster_column>) AS clusters_covered
FROM <catalog>.<schema>.<table>;

-- ---------------------------------------------------------------------------
-- 6. Is queued time recorded separately from wall time?
--    "Not the query's fault" is the single highest-value detector and needs
--    only these two columns -- it is buildable even if step 4 finds nothing.
-- ---------------------------------------------------------------------------
SELECT
    query_id,
    queued_time_ms,
    wall_time_ms,
    CAST(queued_time_ms AS DOUBLE) / nullif(wall_time_ms, 0) AS queued_fraction
FROM <catalog>.<schema>.<table>
WHERE wall_time_ms > 60000
ORDER BY queued_fraction DESC
LIMIT 20;
