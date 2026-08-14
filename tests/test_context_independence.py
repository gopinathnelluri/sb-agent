"""The context injection contract, asserted rather than assumed.

Injected context may improve wording. It may not change what was found. This
file is the enforcement: if someone later threads a ``context`` argument into
the rules engine, these tests fail.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from core.enrich import enrich
from core.models import InjectedContext, Passage, RationaleSource
from core.rules.schema import RuleCatalog
from core.scopes import Scope
from core.service import ConfigValidationService
from tests.conftest import ALL_SCOPES

ENGINE_SOURCES = (
    Path("core/rules/engine.py"),
    Path("core/rules/checks.py"),
    Path("core/rules/consistency.py"),
    Path("core/rules/schema.py"),
)

FORBIDDEN_IN_ENGINE = {"InjectedContext", "enrich", "Annotation", "GuidanceConflict"}

# Everything except the two fields enrichment is allowed to touch.
DECISIONAL_FIELDS = tuple(
    field.name
    for field in dataclasses.fields(
        __import__("core.models", fromlist=["Finding"]).Finding
    )
    if field.name not in {"rationale", "rationale_source", "doc_ref"}
)

RICH_CONTEXT = InjectedContext(
    source="parent_rag",
    properties={
        "query.max-memory-per-node": "Recommended to stay at or below 30% of heap.",
        "memory.heap-headroom-per-node": "Keep headroom at 15% of heap or more.",
        "query.low-memory-killer.policy": "Prefer total-reservation-on-blocked-nodes.",
        "-Xmx": "Size the heap to 80% of the container memory limit.",
    },
    passages=[Passage(text="...", doc="Starburst Tuning Guide", page=14)],
    sep_version_hint="429-e",
)


class TestStructuralIsolation:
    """Context must be unreachable from evaluation code, not merely unused."""

    @pytest.mark.parametrize("source", ENGINE_SOURCES, ids=lambda p: p.name)
    def test_engine_never_names_context_types(self, source: Path) -> None:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom | ast.Import)
            for alias in node.names
        }
        leaked = imported & FORBIDDEN_IN_ENGINE
        assert not leaked, (
            f"{source} imports {leaked}; context must not reach evaluation"
        )

    def test_evaluate_takes_no_context_parameter(self) -> None:
        import inspect

        from core.rules.engine import evaluate

        parameters = set(inspect.signature(evaluate).parameters)
        assert "context" not in parameters
        assert parameters == {"snapshot", "catalog", "scopes", "sep_version"}


class TestFindingsAreUnchanged:
    """The observable guarantee: same cluster, same findings, context or not."""

    @pytest.mark.parametrize(
        "cluster", ["clean-cluster", "drifted-cluster", "bare-cluster"]
    )
    def test_rule_ids_are_identical(
        self, service: ConfigValidationService, cluster: str
    ) -> None:
        without = service.run_rules(cluster, ALL_SCOPES)
        with_context = service.run_rules(cluster, ALL_SCOPES, RICH_CONTEXT)
        assert [f.rule_id for f in without.findings] == [
            f.rule_id for f in with_context.findings
        ]

    @pytest.mark.parametrize(
        "cluster", ["clean-cluster", "drifted-cluster", "bare-cluster"]
    )
    def test_every_decisional_field_is_identical(
        self, service: ConfigValidationService, cluster: str
    ) -> None:
        """Not just the rule ids -- severity, values and evidence too."""
        without = service.run_rules(cluster, ALL_SCOPES)
        with_context = service.run_rules(cluster, ALL_SCOPES, RICH_CONTEXT)

        for plain, enriched in zip(
            without.findings, with_context.findings, strict=True
        ):
            for name in DECISIONAL_FIELDS:
                assert getattr(plain, name) == getattr(enriched, name), name

    def test_coverage_is_identical(self, service: ConfigValidationService) -> None:
        without = service.run_rules("drifted-cluster", ALL_SCOPES)
        with_context = service.run_rules("drifted-cluster", ALL_SCOPES, RICH_CONTEXT)
        assert without.coverage == with_context.coverage

    def test_context_cannot_suppress_a_finding(
        self, service: ConfigValidationService
    ) -> None:
        """Guidance saying the value is fine must not remove the finding."""
        permissive = InjectedContext(
            properties={
                "query.max-memory-per-node": "Any value up to 100% of heap is fine."
            }
        )
        baseline = service.run_rules("drifted-cluster", [Scope.MEMORY])
        with_permissive = service.run_rules(
            "drifted-cluster", [Scope.MEMORY], permissive
        )
        assert {f.rule_id for f in baseline.findings} == {
            f.rule_id for f in with_permissive.findings
        }
        assert any(f.rule_id == "SEP-MEM-002" for f in with_permissive.findings)

    def test_context_cannot_invent_a_finding(
        self, service: ConfigValidationService
    ) -> None:
        """A clean cluster stays clean no matter what context supplies."""
        result = service.run_rules("clean-cluster", ALL_SCOPES, RICH_CONTEXT)
        assert result.findings == []


class TestPermittedEffects:
    """The three things context is allowed to do."""

    def test_rationale_is_reworded_and_attributed(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("drifted-cluster", [Scope.MEMORY], RICH_CONTEXT)
        reworded = [
            f for f in result.findings if f.subject == "query.max-memory-per-node"
        ]
        assert reworded
        for finding in reworded:
            assert finding.rationale_source == RationaleSource.INJECTED_CONTEXT
            assert "30% of heap" in finding.rationale

    def test_uncovered_property_becomes_an_annotation_not_a_finding(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("drifted-cluster", ALL_SCOPES, RICH_CONTEXT)
        subjects = {a.subject for a in result.annotations}
        assert "query.low-memory-killer.policy" in subjects
        assert all(a.source == "injected_context" for a in result.annotations)
        assert "query.low-memory-killer.policy" not in {
            f.subject for f in result.findings
        }

    def test_covered_property_does_not_become_an_annotation(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("drifted-cluster", ALL_SCOPES, RICH_CONTEXT)
        assert "query.max-memory-per-node" not in {
            a.subject for a in result.annotations
        }

    def test_no_context_means_no_annotations(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("drifted-cluster", ALL_SCOPES)
        assert result.annotations == []
        assert result.guidance_conflicts == []


class TestGuidanceConflicts:
    """Disagreement is reported, never resolved."""

    def test_contradicting_threshold_is_flagged(self) -> None:
        catalog = RuleCatalog.load()
        context = InjectedContext(
            properties={"query.max-memory-per-node": "Must not exceed 15% of heap."}
        )
        result = enrich([], catalog, context)
        conflicts = {c.rule_id for c in result.guidance_conflicts}
        assert "SEP-MEM-002" in conflicts

    def test_agreeing_threshold_is_not_flagged(self) -> None:
        catalog = RuleCatalog.load()
        context = InjectedContext(
            properties={"query.max-memory-per-node": "Keep it at or below 30% of heap."}
        )
        result = enrich([], catalog, context)
        assert result.guidance_conflicts == []

    def test_version_hint_disagreeing_with_detection_is_flagged(self) -> None:
        catalog = RuleCatalog.load()
        context = InjectedContext(sep_version_hint="413")
        result = enrich([], catalog, context, detected_version="429-e")
        assert [c.rule_id for c in result.guidance_conflicts] == [
            "sep_version_detection"
        ]

    def test_unparseable_guidance_produces_no_conflict(self) -> None:
        """Prose contradiction is not something deterministic code should guess at."""
        catalog = RuleCatalog.load()
        context = InjectedContext(
            properties={"query.max-memory-per-node": "Should be sized conservatively."}
        )
        assert enrich([], catalog, context).guidance_conflicts == []


class TestVersionHint:
    """A hint fills a gap; it never overrides detection."""

    def test_detected_version_wins_over_a_hint(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules(
            "clean-cluster", ALL_SCOPES, InjectedContext(sep_version_hint="999")
        )
        assert result.sep_version == "429-e"

    def test_hint_is_used_only_when_detection_found_nothing(
        self, service_without_manifests: ConfigValidationService
    ) -> None:
        plain = service_without_manifests.run_rules("clean-cluster", ALL_SCOPES)
        assert plain.sep_version is None

        hinted = service_without_manifests.run_rules(
            "clean-cluster", ALL_SCOPES, InjectedContext(sep_version_hint="429")
        )
        assert hinted.sep_version == "429"
