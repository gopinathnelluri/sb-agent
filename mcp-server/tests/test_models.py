"""Tests for the data contracts themselves."""

from __future__ import annotations

from core.models import (
    ConfigEvidence,
    Coverage,
    Evidence,
    Finding,
    RationaleSource,
    Role,
    Severity,
    compute_fingerprint,
    sort_findings,
)
from core.scopes import Scope


def _evidence(node: str = "worker-01") -> list[Evidence]:
    return [
        ConfigEvidence(
            file="worker/config.properties", role=Role.WORKER, node=node, line=27
        )
    ]


def _finding(
    rule_id: str = "SEP-MEM-002",
    severity: Severity = Severity.HIGH,
    deviation: float | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        fingerprint="x",
        severity=severity,
        domain=Scope.MEMORY,
        summary="s",
        rationale="r",
        rationale_source=RationaleSource.RULE_CATALOG,
        evidence=list(_evidence()),
        deviation=deviation,
    )


class TestFingerprint:
    def test_is_stable_across_calls(self) -> None:
        a = compute_fingerprint("R1", "prod", "k", _evidence())
        b = compute_fingerprint("R1", "prod", "k", _evidence())
        assert a == b

    def test_ignores_evidence_ordering(self) -> None:
        one = ConfigEvidence(file="a", role=Role.WORKER, node="w1")
        two = ConfigEvidence(file="b", role=Role.WORKER, node="w2")
        assert compute_fingerprint("R1", "prod", "k", [one, two]) == (
            compute_fingerprint("R1", "prod", "k", [two, one])
        )

    def test_differs_per_cluster(self) -> None:
        assert compute_fingerprint("R1", "prod", "k", _evidence()) != (
            compute_fingerprint("R1", "staging", "k", _evidence())
        )

    def test_differs_per_node(self) -> None:
        assert compute_fingerprint("R1", "prod", "k", _evidence("worker-01")) != (
            compute_fingerprint("R1", "prod", "k", _evidence("worker-02"))
        )


class TestSortFindings:
    def test_orders_by_severity_first(self) -> None:
        findings = [_finding("A", Severity.LOW), _finding("B", Severity.CRITICAL)]
        assert [f.rule_id for f in sort_findings(findings)] == ["B", "A"]

    def test_breaks_severity_ties_on_deviation(self) -> None:
        findings = [
            _finding("A", Severity.HIGH, deviation=0.05),
            _finding("B", Severity.HIGH, deviation=3.0),
        ]
        assert [f.rule_id for f in sort_findings(findings)] == ["B", "A"]

    def test_is_deterministic_when_fully_tied(self) -> None:
        findings = [_finding("Z", Severity.HIGH), _finding("A", Severity.HIGH)]
        assert [f.rule_id for f in sort_findings(findings)] == ["A", "Z"]


class TestCoverage:
    def test_a_clean_run_is_complete(self) -> None:
        coverage = Coverage.build(rules_evaluated=10, files_parsed=4)
        assert coverage.complete is True
        assert coverage.blind_spots == []

    def test_evaluating_nothing_is_never_complete(self) -> None:
        """Zero rules must not read as an all clear."""
        coverage = Coverage.build(rules_evaluated=0, rules_skipped_version=10)
        assert coverage.complete is False
        assert "no rules were evaluated at all" in coverage.blind_spots

    def test_missing_inputs_are_surfaced(self) -> None:
        coverage = Coverage.build(rules_evaluated=8, rules_skipped_missing_input=2)
        assert coverage.complete is False
        assert any("input values were not available" in s for s in coverage.blind_spots)

    def test_duplicate_blind_spots_are_reported_once(self) -> None:
        """A caller forwarding an inner coverage's spots must not double up."""
        coverage = Coverage.build(
            rules_evaluated=4,
            rules_skipped_missing_input=1,
            blind_spots=[
                "1 check(s) skipped: required input values were not available"
            ],
        )
        assert len(coverage.blind_spots) == 1

    def test_version_gating_alone_does_not_make_a_run_incomplete(self) -> None:
        """A rule that does not apply to this SEP version is skipped, not failed."""
        coverage = Coverage.build(rules_evaluated=8, rules_skipped_version=4)
        assert coverage.complete is True
