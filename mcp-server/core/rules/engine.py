"""Rule evaluation.

The one place a pass/fail decision is made. Everything here is deterministic
Python over an immutable snapshot: no model, no network, no clock, no
randomness. The same backup evaluates to the same findings forever.

Note what this module does *not* import: ``core.enrich`` and
``core.models.InjectedContext`` are both absent, and ``evaluate`` takes the
SEP version as a plain string. Injected context is structurally unable to
reach a threshold, because there is no parameter through which it could
arrive. ``tests/test_context_independence.py`` asserts that this stays true.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config.snapshot import ClusterSnapshot
from core.models import (
    ConfigEvidence,
    Evidence,
    Finding,
    RationaleSource,
    Role,
    compute_fingerprint,
)
from core.rules.checks import CheckOutcome, RuleContext
from core.rules.consistency import evaluate_consistency
from core.rules.schema import ConsistencyRule, PropertyRule, Rule, RuleCatalog
from core.scopes import Scope


@dataclass(frozen=True)
class EvaluationResult:
    """Findings plus the tallies that make coverage reportable."""

    findings: list[Finding] = field(default_factory=list)
    rules_evaluated: int = 0
    rules_skipped_version: int = 0
    rules_skipped_missing_input: int = 0
    rules_skipped_unknown_version: int = 0
    properties_checked: set[str] = field(default_factory=set)


def evaluate(
    snapshot: ClusterSnapshot,
    catalog: RuleCatalog,
    scopes: list[Scope],
    sep_version: str | None,
) -> EvaluationResult:
    """Evaluate every in-scope rule against a snapshot.

    ``sep_version`` is a plain string by design. Resolving where the version
    came from happens upstream in the service; the engine only needs the
    value, which keeps it unable to see anything else the caller knows.
    """
    findings: list[Finding] = []
    evaluated = 0
    skipped_version = 0
    skipped_missing = 0
    skipped_unknown_version = 0
    checked: set[str] = set()

    for rule in catalog.for_scopes(scopes):
        applies = rule.applies_to.version.applies_to(sep_version)
        if applies is False:
            skipped_version += 1
            continue
        if applies is None:
            skipped_unknown_version += 1
            continue

        roles = _roles_present(rule, snapshot)
        if not roles:
            continue

        checked.add(rule.property)
        if isinstance(rule, ConsistencyRule):
            findings.extend(evaluate_consistency(rule, snapshot))
            evaluated += 1
            continue

        for role in roles:
            outcome = _evaluate_property_rule(rule, snapshot, role)
            if outcome is None:
                skipped_missing += 1
                continue
            evaluated += 1
            if outcome.finding is not None:
                findings.append(outcome.finding)

    return EvaluationResult(
        findings=findings,
        rules_evaluated=evaluated,
        rules_skipped_version=skipped_version,
        rules_skipped_missing_input=skipped_missing,
        rules_skipped_unknown_version=skipped_unknown_version,
        properties_checked=checked,
    )


@dataclass(frozen=True)
class _RuleOutcome:
    """A rule that ran. ``finding`` is None when it passed."""

    finding: Finding | None


def _roles_present(rule: Rule, snapshot: ClusterSnapshot) -> list[Role]:
    """Roles the rule targets that actually exist in this backup."""
    return [role for role in rule.applies_to.roles if snapshot.node_count(role) > 0]


def _evaluate_property_rule(
    rule: PropertyRule, snapshot: ClusterSnapshot, role: Role
) -> _RuleOutcome | None:
    """Run one property rule for one role.

    Returns ``None`` when the rule could not run for lack of input, which the
    caller counts towards coverage rather than treating as a pass.
    """
    resolved = snapshot.resolve(rule.property, role)

    if resolved is None:
        return _handle_absent(rule, snapshot, role)

    outcome = rule.check.evaluate(resolved.value, RuleContext(snapshot, role))
    if outcome.missing:
        return None
    if outcome.passed:
        return _RuleOutcome(finding=None)

    evidence: list[Evidence] = [
        ConfigEvidence(
            file=resolved.source_file,
            role=role,
            node=resolved.node,
            line=resolved.line,
        )
    ]
    return _RuleOutcome(
        finding=_finding(
            rule=rule,
            snapshot=snapshot,
            evidence=evidence,
            actual=outcome.actual,
            expected=outcome.expected,
            deviation=outcome.deviation,
            summary=rule.summary or _headline(rule, role, outcome),
        )
    )


def _handle_absent(
    rule: PropertyRule, snapshot: ClusterSnapshot, role: Role
) -> _RuleOutcome | None:
    """Decide what an unset property means for this rule.

    A missing property is its own outcome, not a quiet pass. Many real
    misconfigurations are a property nobody set, where the built-in default
    is wrong for the fleet -- so rules say explicitly what absence means.
    """
    if rule.check.kind != "required" and rule.on_missing == "skip":
        return None
    if rule.on_missing == "pass":
        return _RuleOutcome(finding=None)

    evidence: list[Evidence] = [
        ConfigEvidence(file=_expected_file(rule, snapshot, role), role=role)
    ]
    return _RuleOutcome(
        finding=_finding(
            rule=rule,
            snapshot=snapshot,
            evidence=evidence,
            actual="not set",
            expected=rule.check.describe(),
            deviation=None,
            summary=rule.summary or f"{rule.property} is not set on {role.value}",
        )
    )


def _expected_file(rule: PropertyRule, snapshot: ClusterSnapshot, role: Role) -> str:
    """Best available path to cite when the property is absent everywhere."""
    files = snapshot.files_for(role)
    for candidate in files:
        if candidate.endswith("config.properties"):
            return candidate
    return files[0] if files else f"{role.value}/config.properties"


def _headline(rule: PropertyRule, role: Role, outcome: CheckOutcome) -> str:
    return (
        f"{rule.property} on {role.value} is {outcome.actual}, "
        f"expected {outcome.expected}"
    )


def _finding(
    *,
    rule: PropertyRule,
    snapshot: ClusterSnapshot,
    evidence: list[Evidence],
    actual: str,
    expected: str,
    deviation: float | None,
    summary: str,
) -> Finding:
    return Finding(
        rule_id=rule.id,
        fingerprint=compute_fingerprint(
            rule.id, snapshot.cluster, rule.property, evidence
        ),
        severity=rule.severity,
        scope=rule.domain,
        summary=summary,
        rationale=rule.rationale,
        rationale_source=RationaleSource.RULE_CATALOG,
        evidence=evidence,
        subject=rule.property,
        actual=actual,
        expected=expected,
        deviation=deviation,
        doc_ref=rule.doc_ref,
        next_step=rule.next_step,
    )
