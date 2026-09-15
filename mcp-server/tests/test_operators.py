"""Tests for per-operator statistics.

The parser is written against Trino's documented shape, not against a payload
from this fleet, so these pin the tolerance that makes that safe: several
serialisations, both field-name conventions, sizes and durations as numbers or
as strings, and -- most importantly -- unreadable input yielding nothing
rather than something wrong.
"""

from __future__ import annotations

import json

import pytest

from core.analysis.detectors.base import Thresholds
from core.analysis.detectors.operators import (
    BroadcastSizeDetector,
    ExplodingJoinDetector,
    OperatorSpillDetector,
    ScanSelectivityDetector,
)
from core.analysis.engine import analyze
from core.analysis.models import QueryInfo
from core.analysis.operators import OperatorSummary, parse_operator_summaries
from core.models import QueryEvidence

JOIN = {
    "operatorType": "HashJoinOperator",
    "stageId": 3,
    "operatorId": 2,
    "inputPositions": 2_400_000,
    "outputPositions": 410_000_000,
    "addInputWall": "17.20m",
    "spilledDataSize": "48GB",
}


def _query(*operators: OperatorSummary, **kwargs: object) -> QueryInfo:
    base: dict[str, object] = {
        "query_id": "q",
        "cluster": "prod",
        "source": "audit:test",
        "operators": list(operators),
    }
    base.update(kwargs)
    return QueryInfo(**base)  # type: ignore[arg-type]


class TestParsingShapes:
    """Which serialisation a deployment uses is not knowable in advance."""

    def test_array_of_objects(self) -> None:
        assert len(parse_operator_summaries(json.dumps([JOIN]))) == 1

    def test_array_of_json_strings(self) -> None:
        assert len(parse_operator_summaries(json.dumps([json.dumps(JOIN)]))) == 1

    def test_single_object(self) -> None:
        assert len(parse_operator_summaries(json.dumps(JOIN))) == 1

    def test_mixed_array_takes_what_it_can(self) -> None:
        payload = json.dumps([json.dumps(JOIN), JOIN, "not json", 42])
        assert len(parse_operator_summaries(payload)) == 2

    @pytest.mark.parametrize("payload", [None, "", "   ", "not json", "[[]]"])
    def test_unreadable_payload_yields_nothing(self, payload: str | None) -> None:
        """Never raise: an unknown shape is reduced coverage, not a crash."""
        assert parse_operator_summaries(payload) == []


class TestFieldConventions:
    def test_camel_case(self) -> None:
        (op,) = parse_operator_summaries(json.dumps([{"operatorType": "ScanOperator"}]))
        assert op.operator_type == "ScanOperator"

    def test_snake_case(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps([{"operator_type": "ScanOperator", "output_positions": 5}])
        )
        assert op.operator_type == "ScanOperator"
        assert op.output_rows == 5

    def test_unknown_operator_type_is_recorded_not_dropped(self) -> None:
        (op,) = parse_operator_summaries(json.dumps([{"outputPositions": 5}]))
        assert op.operator_type == "unknown"


class TestValueFormats:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1kB", 1024), ("4.4GB", 4724464025), (512, 512), ("512", 512)],
    )
    def test_sizes(self, raw: object, expected: int) -> None:
        (op,) = parse_operator_summaries(
            json.dumps([{"operatorType": "X", "inputDataSize": raw}])
        )
        assert op.input_bytes == expected

    @pytest.mark.parametrize(
        ("raw", "expected"), [("1.32s", 1320.0), ("2.00m", 120_000.0), (450, 450.0)]
    )
    def test_durations(self, raw: object, expected: float) -> None:
        (op,) = parse_operator_summaries(
            json.dumps([{"operatorType": "X", "addInputWall": raw}])
        )
        assert op.wall_ms == pytest.approx(expected)

    def test_unreadable_value_becomes_none(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps([{"operatorType": "X", "inputDataSize": "banana"}])
        )
        assert op.input_bytes is None

    def test_missing_rows_yield_no_multiplier(self) -> None:
        (op,) = parse_operator_summaries(json.dumps([{"operatorType": "X"}]))
        assert op.row_multiplier is None


class TestExplodingJoin:
    def test_fires_on_row_growth(self) -> None:
        (op,) = parse_operator_summaries(json.dumps([JOIN]))
        findings = ExplodingJoinDetector().detect(_query(op), Thresholds())
        assert len(findings) == 1
        assert "171 times" in findings[0].summary

    def test_silent_on_a_normal_join(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "HashJoinOperator",
                        "inputPositions": 1000,
                        "outputPositions": 900,
                    }
                ]
            )
        )
        assert ExplodingJoinDetector().detect(_query(op), Thresholds()) == []

    def test_ignores_non_join_operators(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "ScanOperator",
                        "inputPositions": 10,
                        "outputPositions": 10_000,
                    }
                ]
            )
        )
        assert ExplodingJoinDetector().detect(_query(op), Thresholds()) == []

    def test_evidence_names_the_stage(self) -> None:
        """The whole point of operator detail: say which step."""
        (op,) = parse_operator_summaries(json.dumps([JOIN]))
        finding = ExplodingJoinDetector().detect(_query(op), Thresholds())[0]
        evidence = finding.evidence[0]
        assert isinstance(evidence, QueryEvidence)
        assert evidence.stage_id == "3"


class TestBroadcastSize:
    def test_fires_on_a_large_broadcast(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "BroadcastExchangeOperator",
                        "outputDataSize": "6.2GB",
                    }
                ]
            )
        )
        assert BroadcastSizeDetector().detect(_query(op), Thresholds())

    def test_silent_on_a_small_broadcast(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "BroadcastExchangeOperator",
                        "outputDataSize": "12MB",
                    }
                ]
            )
        )
        assert BroadcastSizeDetector().detect(_query(op), Thresholds()) == []

    def test_ignores_a_partitioned_exchange(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "PartitionedExchangeOperator",
                        "outputDataSize": "80GB",
                    }
                ]
            )
        )
        assert BroadcastSizeDetector().detect(_query(op), Thresholds()) == []


class TestOperatorSpill:
    def test_names_the_worst_offender(self) -> None:
        ops = parse_operator_summaries(
            json.dumps(
                [
                    JOIN,
                    {
                        "operatorType": "AggregationOperator",
                        "stageId": 5,
                        "spilledDataSize": "2GB",
                    },
                ]
            )
        )
        findings = OperatorSpillDetector().detect(_query(*ops), Thresholds())
        assert len(findings) == 1
        assert "HashJoinOperator" in findings[0].summary
        assert "2 step(s)" in (findings[0].actual or "")

    def test_silent_when_nothing_spilled(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps([{"operatorType": "HashJoinOperator", "spilledDataSize": 0}])
        )
        assert OperatorSpillDetector().detect(_query(op), Thresholds()) == []


class TestScanSelectivity:
    def test_fires_when_almost_everything_is_discarded(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "ScanFilterAndProjectOperator",
                        "physicalInputPositions": 1_800_000_000,
                        "outputPositions": 2_400_000,
                    }
                ]
            )
        )
        findings = ScanSelectivityDetector().detect(_query(op), Thresholds())
        assert len(findings) == 1
        assert "thrown away" in findings[0].summary

    def test_silent_on_a_selective_but_reasonable_scan(self) -> None:
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "ScanOperator",
                        "physicalInputPositions": 10_000_000,
                        "outputPositions": 4_000_000,
                    }
                ]
            )
        )
        assert ScanSelectivityDetector().detect(_query(op), Thresholds()) == []

    def test_ignores_small_scans(self) -> None:
        """A tiny table filtered to nothing is not worth reporting."""
        (op,) = parse_operator_summaries(
            json.dumps(
                [
                    {
                        "operatorType": "ScanOperator",
                        "physicalInputPositions": 500,
                        "outputPositions": 1,
                    }
                ]
            )
        )
        assert ScanSelectivityDetector().detect(_query(op), Thresholds()) == []


class TestEngineIntegration:
    def test_operator_detectors_skip_without_a_payload(self) -> None:
        """A source with no operator_summaries must say so, not stay silent."""
        result = analyze(_query(elapsed_ms=600_000, queued_ms=540_000))
        skipped = {s.detector_id for s in result.detectors_skipped}
        assert {"QRY-JOIN-001", "QRY-BCAST-001", "QRY-SPILL-002"} <= skipped

    def test_operator_detectors_run_with_a_payload(self) -> None:
        ops = parse_operator_summaries(json.dumps([JOIN]))
        result = analyze(_query(*ops, elapsed_ms=600_000, queued_ms=6_000))
        assert "QRY-JOIN-001" in result.detectors_run
        assert any(f.rule_id == "QRY-JOIN-001" for f in result.findings)

    def test_unparseable_payload_degrades_rather_than_crashing(self) -> None:
        result = analyze(_query(*parse_operator_summaries("{{{ not json")))
        assert result.coverage is not None
        assert any("per-operator" in spot for spot in result.coverage.blind_spots)
