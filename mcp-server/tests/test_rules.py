"""Rule catalog, check kinds and version gating."""

from __future__ import annotations

import pytest

from core.config.snapshot import ClusterSnapshot, NodeConfig
from core.models import Role, Severity
from core.parsers import parse_jvm_config, parse_properties
from core.rules.checks import RuleContext, RuleDefinitionError, build_check
from core.rules.schema import ConsistencyRule, PropertyRule, RuleCatalog, parse_rule
from core.rules.versions import VersionRange
from core.scopes import Scope


def _snapshot(heap: str = "80G", **properties: str) -> ClusterSnapshot:
    body = "".join(f"{key}={value}\n" for key, value in properties.items())
    return ClusterSnapshot(
        cluster="test",
        nodes=[
            NodeConfig(
                role=Role.WORKER,
                node="worker-01",
                files={
                    "config.properties": parse_properties("config.properties", body),
                    "jvm.config": parse_jvm_config("jvm.config", f"-Xmx{heap}\n"),
                },
            )
        ],
    )


def _context(heap: str = "80G", **properties: str) -> RuleContext:
    return RuleContext(_snapshot(heap, **properties), Role.WORKER)


class TestCheckKinds:
    def test_max_ratio_passes_at_the_boundary(self) -> None:
        check = build_check({"kind": "max_ratio", "of": "jvm_heap", "ratio": 0.3})
        assert check.evaluate("24GB", _context()).passed

    def test_max_ratio_fails_above_the_boundary(self) -> None:
        check = build_check({"kind": "max_ratio", "of": "jvm_heap", "ratio": 0.3})
        outcome = check.evaluate("40GB", _context())
        assert not outcome.passed
        assert "24GB" in outcome.expected

    def test_deviation_scales_with_how_far_past_the_limit(self) -> None:
        """Ranking depends on this: 300% over must outrank 5% over."""
        check = build_check({"kind": "max_ratio", "of": "jvm_heap", "ratio": 0.3})
        small = check.evaluate("25GB", _context()).deviation
        large = check.evaluate("96GB", _context()).deviation
        assert small is not None
        assert large is not None
        assert large > small

    def test_ratio_reports_missing_input_when_heap_is_unknown(self) -> None:
        """No heap means no denominator -- skipped, never guessed."""
        snapshot = ClusterSnapshot(
            cluster="t",
            nodes=[NodeConfig(role=Role.WORKER, node="w1", files={})],
        )
        outcome = build_check(
            {"kind": "max_ratio", "of": "jvm_heap", "ratio": 0.3}
        ).evaluate("40GB", RuleContext(snapshot, Role.WORKER))
        assert outcome.missing == ["JVM heap (-Xmx)"]

    def test_units_are_normalised_before_comparison(self) -> None:
        check = build_check({"kind": "max", "value": "24GB"})
        assert check.evaluate("24576MB", _context()).passed

    def test_max_compares_durations_when_written_as_one(self) -> None:
        check = build_check({"kind": "max", "value": "30s"})
        assert check.evaluate("10s", _context()).passed
        assert not check.evaluate("45s", _context()).passed

    def test_range_bounds_are_inclusive(self) -> None:
        check = build_check({"kind": "range", "min": "16M", "max": "64M"})
        assert check.evaluate("16M", _context()).passed
        assert check.evaluate("64M", _context()).passed
        assert not check.evaluate("8M", _context()).passed

    def test_one_of_is_case_insensitive(self) -> None:
        check = build_check({"kind": "one_of", "values": ["G1", "ZGC"]})
        assert check.evaluate("zgc", _context()).passed

    def test_matches_applies_the_regex(self) -> None:
        check = build_check({"kind": "matches", "pattern": "^[a-z0-9_]+$"})
        assert check.evaluate("production", _context()).passed
        assert not check.evaluate("Prod-East", _context()).passed

    def test_property_reference_resolves_another_setting(self) -> None:
        check = build_check(
            {"kind": "max_ratio", "of": "property:query.max-memory", "ratio": 0.5}
        )
        context = _context(**{"query.max-memory": "100GB"})
        assert check.evaluate("40GB", context).passed
        assert not check.evaluate("80GB", context).passed


class TestCatalogValidation:
    """A malformed rule must fail loudly at load, never silently disappear."""

    def _rule(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": "T-001",
            "domain": "memory",
            "property": "a.b",
            "severity": "high",
            "rationale": "because",
            "check": {"kind": "required"},
        }
        base.update(overrides)
        return base

    def test_a_valid_rule_parses(self) -> None:
        assert isinstance(parse_rule(self._rule()), PropertyRule)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="unknown field"):
            parse_rule(self._rule(sevarity="high"))

    def test_unknown_check_field_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="does not accept"):
            parse_rule(self._rule(check={"kind": "max", "vlaue": "1GB"}))

    def test_unknown_check_kind_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="Unknown check kind"):
            parse_rule(self._rule(check={"kind": "lessthanish", "value": "1"}))

    def test_unknown_domain_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="unknown domain"):
            parse_rule(self._rule(domain="memry"))

    def test_missing_rationale_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="rationale"):
            parse_rule(self._rule(rationale=""))

    def test_a_rule_needs_exactly_one_of_check_or_consistency(self) -> None:
        with pytest.raises(RuleDefinitionError, match="exactly one"):
            parse_rule(
                self._rule(consistency={"kind": "equal_across_nodes", "property": "a"})
            )

    def test_invalid_on_missing_is_rejected(self) -> None:
        with pytest.raises(RuleDefinitionError, match="on_missing"):
            parse_rule(self._rule(on_missing="explode"))

    def test_consistency_rule_parses(self) -> None:
        rule = parse_rule(
            {
                "id": "T-002",
                "domain": "node_identity",
                "severity": "high",
                "rationale": "because",
                "consistency": {
                    "kind": "equal_across_nodes",
                    "property": "node.environment",
                },
            }
        )
        assert isinstance(rule, ConsistencyRule)


class TestVersionGating:
    @pytest.mark.parametrize(
        ("spec", "version", "expected"),
        [
            (">=413", "429-e", True),
            (">=413", "406", False),
            (">=413,<440", "429-e.3", True),
            (">=413,<440", "451", False),
            ("*", "429", True),
            (None, "429", True),
            (None, None, True),
        ],
    )
    def test_applicability(
        self, spec: str | None, version: str | None, expected: bool
    ) -> None:
        assert VersionRange.parse(spec).applies_to(version) is expected

    def test_unknown_version_is_undecidable_not_false(self) -> None:
        """The failure that would make an unknown cluster look healthy."""
        assert VersionRange.parse(">=413").applies_to(None) is None

    def test_unconstrained_rules_still_run_without_a_version(self) -> None:
        assert VersionRange.parse(None).applies_to(None) is True

    def test_malformed_constraint_is_rejected(self) -> None:
        with pytest.raises(Exception, match="Cannot parse version constraint"):
            VersionRange.parse(">>413")


class TestShippedCatalog:
    """Properties the catalog itself must hold."""

    def test_it_loads(self, catalog: RuleCatalog) -> None:
        assert catalog.rules

    def test_rule_ids_are_unique(self, catalog: RuleCatalog) -> None:
        ids = [rule.id for rule in catalog.rules]
        assert len(ids) == len(set(ids))

    def test_every_rule_has_a_standalone_rationale(self, catalog: RuleCatalog) -> None:
        """Rationale must read without expanding doc_ref through anyone's RAG."""
        for rule in catalog.rules:
            assert len(rule.rationale) > 40, rule.id

    def test_every_rule_domain_is_a_real_scope(self, catalog: RuleCatalog) -> None:
        for rule in catalog.rules:
            assert rule.domain in set(Scope)

    def test_severities_are_spread(self, catalog: RuleCatalog) -> None:
        """A catalog where everything is high severity cannot be ranked."""
        used = {rule.severity for rule in catalog.rules}
        assert len(used) >= 3
        assert Severity.CRITICAL in used
