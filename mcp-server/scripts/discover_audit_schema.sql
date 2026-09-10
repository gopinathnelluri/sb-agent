-- Open questions against the completed_queries table.
--
-- The column list is known. These three queries answer what is left, and the
-- first one is worth far more than the other two.
--
-- Replace <catalog>.<schema> throughout. Any client works -- the Python
-- client, the Starburst API, or the CLI.

-- ===========================================================================
-- 1. THE IMPORTANT ONE: what shape is operator_summaries?
--
--    This decides whether five detectors can be built: skew, exploding join,
--    bad broadcast, ineffective dynamic filter, and plan-based partition
--    pruning. Together they are roughly half the analyser.
--
--    Pick a query that did real work -- a trivial one may have almost no
--    operator detail. Redact anything sensitive in `query` before sharing;
--    the operator payload itself holds statistics, not data.
-- ===========================================================================
-- operator_summaries is a VARCHAR on this fleet, so it holds JSON as text
-- rather than a native array. Read it as text -- this works whatever is
-- inside, which is the point when the shape is what we are trying to learn.

SELECT
    query_id,
    length(operator_summaries)              AS payload_chars,
    substr(operator_summaries, 1, 4000)     AS payload_head
FROM <catalog>.<schema>.completed_queries
WHERE operator_summaries IS NOT NULL
  AND length(operator_summaries) > 100
  AND wall_time_ms > 30000
ORDER BY end_time DESC
LIMIT 1;

-- Once the text confirms it is JSON, this says whether it is an array of
-- objects or an array of JSON-encoded strings -- which changes how it is
-- parsed. Skip it if the query above already makes that obvious.
--
--   SELECT query_id,
--          json_array_length(json_parse(operator_summaries)) AS operators,
--          substr(
--              json_format(
--                  json_array_get(json_parse(operator_summaries), 0)
--              ), 1, 3000
--          ) AS first_operator
--   FROM <catalog>.<schema>.completed_queries
--   WHERE operator_summaries IS NOT NULL AND wall_time_ms > 30000
--   ORDER BY end_time DESC LIMIT 1;

-- ===========================================================================
-- 2. Units of the columns with no _ms suffix.
--
--    wall_time_ms and cpu_time_ms are clearly milliseconds. planning_time,
--    execution_time, analysis_time, scheduled_time and resource_waiting_time
--    are not labelled. If they are Trino Duration strings ("1.32s") and we
--    read them as integer milliseconds, every number derived from them is
--    wrong by three orders of magnitude -- so they are currently left
--    unmapped rather than guessed.
-- ===========================================================================
SELECT
    typeof(planning_time)           AS planning_type,
    typeof(execution_time)          AS execution_type,
    typeof(analysis_time)           AS analysis_type,
    typeof(resource_waiting_time)   AS waiting_type,
    typeof(wall_time_ms)            AS wall_type_for_comparison,
    CAST(planning_time AS VARCHAR)  AS planning_sample,
    CAST(execution_time AS VARCHAR) AS execution_sample,
    wall_time_ms                    AS wall_sample
FROM <catalog>.<schema>.completed_queries
WHERE planning_time IS NOT NULL
ORDER BY end_time DESC
LIMIT 3;

-- ===========================================================================
-- 3. Retention, and whether one table really covers the whole fleet.
--
--    Retention is the real analysis window and belongs in the tool response,
--    so a user hears "that query is outside our window" rather than "not
--    found". The environment count confirms the cluster_column mapping.
-- ===========================================================================
SELECT
    min(end_time)                   AS oldest_retained,
    max(end_time)                   AS newest,
    count(*)                        AS total_rows,
    count(DISTINCT environment)     AS clusters_covered
FROM <catalog>.<schema>.completed_queries;
