"""Tests for the runtime detectors and the analysis engine.

The property that matters most here is not that findings fire -- it is that a
thin source produces *fewer* findings with a stated reason, never a wrong one.
"""

from __future__ import annotations

import pytest

from core.analysis.detectors.base import Thresholds
from core.analysis.detectors.timing import QueryFailedDetector, QueueDominatedDetector
from core.analysis.detectors.volume import ScanAmplificationDetector, SpillDetector
from core.analysis.engine import DEFAULT_DETECTORS, analyze
from core.analysis.models import QueryInfo, QueryState
from core.models import Owner, Severity


def _query(**overrides: object) -> QueryInfo:
    base: dict[str, object] = {
        "query_id": "20260909_00001",
        "cluster": "prod",
        "source": "audit:test",
    }
    base.update(overrides)
    return QueryInfo(**base)  # type: ignore[arg-type]


class TestAvailableFields:
    def test_reports_only_populated_metrics(self) -> None:
        query = _query(elapsed_ms=100, queued_ms=10)
        assert query.available_fields() == {"elapsed_ms", "queued_ms"}

    def test_zero_is_present_not_missing(self) -> None:
        """A real zero must not read as an absent column."""
        assert "spilled_bytes" in _query(spilled_bytes=0).available_fields()

    def test_operator_detail_absent_by_default(self) -> None:
        assert _query().has_operator_detail is False


class TestQueueDominated:
    def test_fires_when_queueing_dominates(self) -> None:
        findings = QueueDominatedDetector().detect(
            _query(elapsed_ms=600_000, queued_ms=540_000), Thresholds()
        )
        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_blames_the_platform_not_the_author(self) -> None:
        """The whole point: do not tell the user to rewrite a query that waited."""
        finding = QueueDominatedDetector().detect(
            _query(elapsed_ms=600_000, queued_ms=540_000), Thresholds()
        )[0]
        assert finding.owner is Owner.PLATFORM_TEAM
        assert finding.next_step is not None
        assert "No change to your SQL" in finding.next_step

    def test_silent_when_execution_dominates(self) -> None:
        assert (
            QueueDominatedDetector().detect(
                _query(elapsed_ms=600_000, queued_ms=6_000), Thresholds()
            )
            == []
        )

    def test_ignores_short_queries(self) -> None:
        """A 2s query that queued 90% of that is not worth reporting."""
        assert (
            QueueDominatedDetector().detect(
                _query(elapsed_ms=2_000, queued_ms=1_900), Thresholds()
            )
            == []
        )

    def test_silent_when_timing_absent(self) -> None:
        assert QueueDominatedDetector().detect(_query(), Thresholds()) == []

    def test_threshold_is_tunable(self) -> None:
        query = _query(elapsed_ms=600_000, queued_ms=180_000)
        assert QueueDominatedDetector().detect(query, Thresholds()) == []
        loose = Thresholds(queued_fraction=0.2)
        assert QueueDominatedDetector().detect(query, loose)


class TestQueryFailed:
    def test_fires_on_failure(self) -> None:
        findings = QueryFailedDetector().detect(
            _query(state=QueryState.FAILED, error_code="EXCEEDED_MEMORY_LIMIT"),
            Thresholds(),
        )
        assert len(findings) == 1
        assert findings[0].owner is Owner.QUERY_AUTHOR

    def test_silent_on_success(self) -> None:
        assert (
            QueryFailedDetector().detect(
                _query(state=QueryState.FINISHED), Thresholds()
            )
            == []
        )


class TestSpill:
    def test_fires_on_any_spill(self) -> None:
        findings = SpillDetector().detect(
            _query(spilled_bytes=5_000_000_000), Thresholds()
        )
        assert len(findings) == 1
        assert "GB" in findings[0].summary

    def test_silent_on_zero_spill(self) -> None:
        assert SpillDetector().detect(_query(spilled_bytes=0), Thresholds()) == []

    def test_silent_when_unrecorded(self) -> None:
        """Unmapped column must not read as 'did not spill'."""
        assert SpillDetector().detect(_query(), Thresholds()) == []


class TestScanAmplification:
    def test_fires_on_huge_scan_for_tiny_result(self) -> None:
        findings = ScanAmplificationDetector().detect(
            _query(total_bytes_scanned=4_400_000_000_000, output_rows=112),
            Thresholds(),
        )
        assert len(findings) == 1
        assert findings[0].owner is Owner.QUERY_AUTHOR

    def test_silent_on_proportionate_scan(self) -> None:
        assert (
            ScanAmplificationDetector().detect(
                _query(total_bytes_scanned=1_000_000, output_rows=50_000), Thresholds()
            )
            == []
        )

    def test_deviation_scales_with_severity_of_the_problem(self) -> None:
        detector = ScanAmplificationDetector()
        mild = detector.detect(
            _query(total_bytes_scanned=20_000_000, output_rows=1), Thresholds()
        )[0]
        severe = detector.detect(
            _query(total_bytes_scanned=4_000_000_000_000, output_rows=1), Thresholds()
        )[0]
        assert severe.deviation is not None
        assert mild.deviation is not None
        assert severe.deviation > mild.deviation


class TestEngine:
    def test_runs_what_it_can_and_names_what_it_cannot(self) -> None:
        result = analyze(_query(elapsed_ms=600_000, queued_ms=540_000))
        assert "QRY-QUEUE-001" in result.detectors_run
        skipped = {s.detector_id for s in result.detectors_skipped}
        assert "QRY-SPILL-001" in skipped

    def test_thin_source_is_never_reported_as_complete(self) -> None:
        """The failure that would let silence read as an all clear."""
        result = analyze(_query(elapsed_ms=600_000, queued_ms=6_000))
        assert result.findings == []
        assert result.coverage is not None
        assert result.coverage.complete is False

    def test_missing_operator_stats_is_an_explicit_blind_spot(self) -> None:
        result = analyze(_query(elapsed_ms=1, queued_ms=1))
        assert result.coverage is not None
        assert any("per-operator" in spot for spot in result.coverage.blind_spots)

    def test_findings_sorted_most_severe_first(self) -> None:
        result = analyze(
            _query(
                elapsed_ms=600_000,
                queued_ms=540_000,
                spilled_bytes=1_000_000,
                total_bytes_scanned=4_000_000_000_000,
                output_rows=10,
            )
        )
        ranks = [f.severity.rank for f in result.findings]
        assert ranks == sorted(ranks)

    def test_every_detector_declares_its_inputs(self) -> None:
        for detector in DEFAULT_DETECTORS:
            assert isinstance(detector.requires, frozenset)
            assert detector.id


class TestThresholds:
    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValueError, match="Unknown threshold"):
            Thresholds.from_mapping({"queued_fractionn": 0.5})

    def test_builds_from_config(self) -> None:
        assert Thresholds.from_mapping({"queued_fraction": 0.8}).queued_fraction == 0.8

    def test_empty_config_yields_defaults(self) -> None:
        assert Thresholds.from_mapping(None) == Thresholds()


class TestUseCaseSeparation:
    """Config auditing and query analysis are two use cases.

    They share the shape of a finding -- severity, owner, evidence -- but not
    its taxonomy. A query finding tagged with a config ``Scope`` would claim
    to be a fact about config files, which it is not.
    """

    def test_query_findings_never_carry_a_config_scope(self) -> None:
        """mypy proves QueryDomain and Scope cannot overlap; this pins it at
        runtime too, so a future widening of the union is caught here."""
        from core.models import QueryDomain

        result = analyze(
            _query(
                state=QueryState.FAILED,
                error_code="EXCEEDED_MEMORY_LIMIT",
                elapsed_ms=600_000,
                queued_ms=540_000,
                spilled_bytes=1_000,
                total_bytes_scanned=4_000_000_000_000,
                output_rows=1,
            )
        )
        assert result.findings
        for finding in result.findings:
            assert isinstance(finding.domain, QueryDomain), finding.rule_id

    def test_every_query_domain_is_reachable(self) -> None:
        """A taxonomy value nothing emits is dead weight -- keep them earning it."""
        from core.analysis.correlate import CORRELATIONS
        from core.models import QueryDomain

        emitted: set[QueryDomain] = {c.domain for c in CORRELATIONS}
        result = analyze(
            _query(
                state=QueryState.FAILED,
                elapsed_ms=600_000,
                queued_ms=540_000,
                spilled_bytes=1_000,
                total_bytes_scanned=4_000_000_000_000,
                output_rows=1,
            )
        )
        emitted |= {
            f.domain for f in result.findings if isinstance(f.domain, QueryDomain)
        }
        assert emitted == set(QueryDomain)
