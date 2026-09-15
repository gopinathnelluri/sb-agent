"""Reading Trino's per-operator statistics.

`operator_summaries` records what each step of a query actually did -- rows in
and out, time, memory, spill -- as opposed to the whole-query totals, which
say a query was slow without saying which part of it was.

Written against Trino's documented `OperatorStats` shape and DELIBERATELY
TOLERANT, because that shape has not yet been checked against a real payload
from this fleet. Three things vary between deployments and versions, and all
three are absorbed here rather than asserted:

* the payload may be a JSON array of objects, or an array of JSON-encoded
  strings, or a single object
* field names may be camelCase or snake_case
* sizes and durations may be numbers or human strings ("4.4GB", "1.32s")

Every field is optional. An operator whose row counts are missing is an
operator the detectors skip, not one they assume was fine. That is what makes
it safe to ship a parser written against documentation: a wrong guess loses a
detector and says so, rather than producing a confident wrong answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_SIZE = re.compile(r"^\s*([\d.]+)\s*([KMGTP]?B)\s*$", re.I)
_DURATION = re.compile(r"^\s*([\d.]+)\s*(ns|us|ms|s|m|h|d)\s*$", re.I)

_SIZE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}
_DURATION_UNITS = {
    "ns": 1e-6,
    "us": 1e-3,
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}

# Operator type fragments, matched case-insensitively against operatorType.
JOIN_OPERATORS = ("join", "nestedloop", "semijoin")
SCAN_OPERATORS = ("tablescan", "scanfilter", "scanfilterproject", "sourceoperator")
EXCHANGE_OPERATORS = ("exchange", "merge")
AGGREGATION_OPERATORS = ("aggregation", "hashaggregation")


def _num(value: Any) -> int | None:
    """Read a byte count, whether it arrives as a number or as '4.4GB'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, dict):
        # Some serialisations wrap sizes as {"value": 4.4, "unit": "GB"}.
        inner = value.get("value")
        unit = str(value.get("unit") or "B").upper()
        if isinstance(inner, int | float):
            return int(float(inner) * _SIZE_UNITS.get(unit, 1))
        return None
    match = _SIZE.match(str(value))
    if match:
        return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _millis(value: Any) -> float | None:
    """Read a duration in milliseconds, whether numeric or '1.32s'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _DURATION.match(str(value))
    if match:
        return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]
    try:
        return float(str(value))
    except ValueError:
        return None


def _get(raw: dict[str, Any], *names: str) -> Any:
    """Look a field up under any of its plausible spellings."""
    for name in names:
        if name in raw:
            return raw[name]
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        if snake in raw:
            return raw[snake]
        camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)
        if camel in raw:
            return raw[camel]
    return None


@dataclass(frozen=True)
class OperatorSummary:
    """One step of the query, and what it did.

    Every metric is optional; see the module docstring for why.
    """

    operator_type: str
    stage_id: str | None = None
    pipeline_id: str | None = None
    operator_id: str | None = None
    plan_node_id: str | None = None
    drivers: int | None = None

    input_rows: int | None = None
    input_bytes: int | None = None
    physical_input_rows: int | None = None
    physical_input_bytes: int | None = None
    output_rows: int | None = None
    output_bytes: int | None = None

    wall_ms: float | None = None
    cpu_ms: float | None = None
    blocked_ms: float | None = None

    peak_memory_bytes: int | None = None
    spilled_bytes: int | None = None

    @property
    def label(self) -> str:
        """How to refer to this operator when talking to a person."""
        where = f"stage {self.stage_id}" if self.stage_id else "the plan"
        return f"{self.operator_type} in {where}"

    @property
    def row_multiplier(self) -> float | None:
        """How many rows this step produced per row it consumed."""
        if self.input_rows is None or self.output_rows is None:
            return None
        if self.input_rows <= 0:
            return None
        return self.output_rows / self.input_rows

    def is_a(self, *fragments: str) -> bool:
        lowered = self.operator_type.lower().replace("_", "")
        return any(fragment in lowered for fragment in fragments)


def _one(raw: dict[str, Any]) -> OperatorSummary:
    return OperatorSummary(
        operator_type=str(_get(raw, "operatorType", "operator_type") or "unknown"),
        stage_id=_text(_get(raw, "stageId", "stage_id")),
        pipeline_id=_text(_get(raw, "pipelineId", "pipeline_id")),
        operator_id=_text(_get(raw, "operatorId", "operator_id")),
        plan_node_id=_text(_get(raw, "planNodeId", "plan_node_id")),
        drivers=_int(_get(raw, "totalDrivers", "total_drivers")),
        input_rows=_int(_get(raw, "inputPositions", "input_positions")),
        input_bytes=_num(_get(raw, "inputDataSize", "input_data_size")),
        physical_input_rows=_int(
            _get(raw, "physicalInputPositions", "physical_input_positions")
        ),
        physical_input_bytes=_num(
            _get(raw, "physicalInputDataSize", "physical_input_data_size")
        ),
        output_rows=_int(_get(raw, "outputPositions", "output_positions")),
        output_bytes=_num(_get(raw, "outputDataSize", "output_data_size")),
        wall_ms=_millis(_get(raw, "addInputWall", "getOutputWall", "wall")),
        cpu_ms=_millis(_get(raw, "addInputCpu", "getOutputCpu", "cpu")),
        blocked_ms=_millis(_get(raw, "blockedWall", "blocked_wall")),
        peak_memory_bytes=_num(
            _get(raw, "peakUserMemoryReservation", "peak_user_memory_reservation")
        ),
        spilled_bytes=_num(_get(raw, "spilledDataSize", "spilled_data_size")),
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_operator_summaries(payload: str | None) -> list[OperatorSummary]:
    """Read the payload into operator records.

    Returns an empty list for anything unreadable rather than raising. A
    caller that gets nothing back reports reduced coverage, which is the
    honest outcome when the payload is absent or in a shape we do not yet
    recognise.
    """
    if not payload or not payload.strip():
        return []
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return []

    if isinstance(loaded, dict):
        return [_one(loaded)]
    if not isinstance(loaded, list):
        return []

    out: list[OperatorSummary] = []
    for item in loaded:
        entry: Any = item
        # Trino serialises each operator as a JSON string inside the array on
        # some versions, and as an object on others.
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except json.JSONDecodeError:
                continue
        if isinstance(entry, dict):
            out.append(_one(entry))
    return out
