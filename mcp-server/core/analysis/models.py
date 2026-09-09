"""Normalised view of one completed query.

Every source -- the audit catalog today, the coordinator REST API or an
event-listener payload later -- is mapped into this type by an adapter. The
detectors never see a vendor shape, so a Starburst upgrade that changes a
column name or a JSON field breaks one mapping function rather than nine
detectors.

Every metric is optional. A field the source does not carry is ``None``, and
a detector that needs it is skipped and reported in coverage. That is the
difference between "we checked and it is fine" and "we could not check",
which the parent has to be able to tell apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class QueryState(StrEnum):
    """Terminal state of a query, normalised across sources."""

    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RUNNING = "running"
    QUEUED = "queued"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StageStats:
    """Per-stage statistics. Only present when the source carries them."""

    stage_id: str
    tasks: int | None = None
    wall_time_ms_max: int | None = None
    wall_time_ms_p50: int | None = None
    input_rows: int | None = None
    output_rows: int | None = None
    input_bytes: int | None = None
    spilled_bytes: int | None = None


@dataclass(frozen=True)
class OperatorStats:
    """Per-operator statistics. Only present when the source carries them."""

    stage_id: str
    operator_id: str
    operator_type: str
    input_rows: int | None = None
    output_rows: int | None = None
    input_bytes: int | None = None
    spilled_bytes: int | None = None


@dataclass(frozen=True)
class TableRef:
    """A table the query touched, fully qualified where known."""

    catalog: str | None
    schema: str | None
    table: str

    @property
    def qualified(self) -> str:
        parts = [p for p in (self.catalog, self.schema, self.table) if p]
        return ".".join(parts)


@dataclass(frozen=True)
class QueryInfo:
    """One completed query, as much as the source could tell us.

    ``source`` records where this came from, so a finding can say whether it
    rests on measured runtime statistics or on a thinner audit row.
    """

    query_id: str
    cluster: str
    source: str
    state: QueryState = QueryState.UNKNOWN
    sql: str | None = None
    user: str | None = None
    session_catalog: str | None = None
    session_schema: str | None = None

    # Timing -- the basis of the "not the query's fault" detector.
    elapsed_ms: int | None = None
    queued_ms: int | None = None
    planning_ms: int | None = None
    execution_ms: int | None = None
    cpu_ms: int | None = None

    # Volume.
    peak_memory_bytes: int | None = None
    total_bytes_scanned: int | None = None
    total_rows: int | None = None
    output_rows: int | None = None
    written_rows: int | None = None
    spilled_bytes: int | None = None
    completed_splits: int | None = None

    # Failure detail.
    error_code: str | None = None
    error_message: str | None = None

    # Present only when the source carries a full statistics payload.
    stages: list[StageStats] = field(default_factory=list)
    operators: list[OperatorStats] = field(default_factory=list)
    tables: list[TableRef] = field(default_factory=list)

    started_at: str | None = None
    ended_at: str | None = None

    @property
    def has_operator_detail(self) -> bool:
        """Whether the richer detectors can run at all against this query."""
        return bool(self.operators)

    def available_fields(self) -> set[str]:
        """Names of the metrics this query actually carries.

        Detectors declare what they need; the engine compares against this to
        decide run-or-skip, so a thin audit row degrades to fewer detectors
        rather than to wrong answers.
        """
        present = {
            name
            for name in (
                "elapsed_ms",
                "queued_ms",
                "planning_ms",
                "execution_ms",
                "cpu_ms",
                "peak_memory_bytes",
                "total_bytes_scanned",
                "total_rows",
                "output_rows",
                "written_rows",
                "spilled_bytes",
                "completed_splits",
            )
            if getattr(self, name) is not None
        }
        if self.stages:
            present.add("stages")
        if self.operators:
            present.add("operators")
        return present
