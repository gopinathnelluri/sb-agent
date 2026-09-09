"""Detector evaluation.

Mirrors ``core/rules/engine.py``: deterministic, no model, no network, and it
reports what it could not check rather than staying silent about it. A
detector whose required fields are absent from the source is skipped and
named in coverage, so "no findings" is only good news when coverage says the
detectors actually ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.analysis.detectors.base import Detector, Thresholds
from core.analysis.detectors.timing import QueryFailedDetector, QueueDominatedDetector
from core.analysis.detectors.volume import ScanAmplificationDetector, SpillDetector
from core.analysis.models import QueryInfo
from core.models import Coverage, Finding, sort_findings

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    QueryFailedDetector(),
    QueueDominatedDetector(),
    SpillDetector(),
    ScanAmplificationDetector(),
)


@dataclass(frozen=True)
class SkippedDetector:
    """A detector that could not run, and the fields that were missing."""

    detector_id: str
    missing: list[str]


@dataclass(frozen=True)
class AnalysisResult:
    """Findings plus an honest account of what was and was not checked."""

    query_id: str
    cluster: str
    findings: list[Finding] = field(default_factory=list)
    detectors_run: list[str] = field(default_factory=list)
    detectors_skipped: list[SkippedDetector] = field(default_factory=list)
    coverage: Coverage | None = None


def analyze(
    query: QueryInfo,
    detectors: tuple[Detector, ...] = DEFAULT_DETECTORS,
    thresholds: Thresholds | None = None,
) -> AnalysisResult:
    """Run every detector whose inputs are present on this query."""
    limits = thresholds or Thresholds()
    available = query.available_fields()

    findings: list[Finding] = []
    ran: list[str] = []
    skipped: list[SkippedDetector] = []

    for detector in detectors:
        missing = sorted(detector.requires - available)
        if missing:
            skipped.append(SkippedDetector(detector.id, missing))
            continue
        ran.append(detector.id)
        findings.extend(detector.detect(query, limits))

    blind_spots = [
        f"{s.detector_id} skipped: source did not provide {', '.join(s.missing)}"
        for s in skipped
    ]
    if not query.has_operator_detail:
        blind_spots.append(
            "no per-operator statistics available from this source, so skew, "
            "join explosion, broadcast sizing and dynamic-filter effectiveness "
            "could not be assessed"
        )

    return AnalysisResult(
        query_id=query.query_id,
        cluster=query.cluster,
        findings=sort_findings(findings),
        detectors_run=ran,
        detectors_skipped=skipped,
        coverage=Coverage.build(
            rules_evaluated=len(ran),
            rules_skipped_missing_input=len(skipped),
            blind_spots=blind_spots,
        ),
    )
