"""Fleet consistency rules.

Some problems are invisible to any single-property check because the defect
is disagreement, not a value: one worker in a 24-node fleet with the wrong
``node.environment``, or a coordinator and workers with mismatched heap. Both
configs look individually reasonable.

These rules cite every node that disagrees, so the answer names the outlier
rather than saying "something differs".
"""

from __future__ import annotations

from core.config.snapshot import ClusterSnapshot, Observation
from core.models import (
    ConfigEvidence,
    Evidence,
    Finding,
    RationaleSource,
    Role,
    compute_fingerprint,
)
from core.rules.schema import ConsistencyRule

MAX_CITED_NODES = 10


def evaluate_consistency(
    rule: ConsistencyRule, snapshot: ClusterSnapshot
) -> list[Finding]:
    """Apply one consistency rule, returning zero or one finding per role group."""
    if rule.kind == "equal_across_nodes":
        return _across_nodes(rule, snapshot)
    return _across_roles(rule, snapshot)


def _across_nodes(rule: ConsistencyRule, snapshot: ClusterSnapshot) -> list[Finding]:
    findings: list[Finding] = []
    for role in rule.applies_to.roles:
        observations = snapshot.observe(rule.property, role)
        if len(observations) < 2:
            continue
        majority = _majority_value(observations)
        outliers = [item for item in observations if item.value != majority]
        if not outliers:
            continue
        findings.append(
            _finding(
                rule=rule,
                snapshot=snapshot,
                actual=_describe_outliers(outliers),
                expected=f"all {len(observations)} {role.value} node(s) set "
                f"{rule.property}={majority}",
                evidence=_evidence(outliers),
                deviation=len(outliers) / len(observations),
                summary=f"{rule.property} differs on {len(outliers)} of "
                f"{len(observations)} {role.value} nodes",
            )
        )
    return findings


def _across_roles(rule: ConsistencyRule, snapshot: ClusterSnapshot) -> list[Finding]:
    per_role: dict[Role, Observation] = {}
    for role in rule.applies_to.roles:
        resolved = snapshot.resolve(rule.property, role)
        if resolved is None:
            continue
        per_role[role] = Observation(
            node=resolved.node,
            label=role.value,
            role=role,
            value=resolved.value,
            source_file=resolved.source_file,
            line=resolved.line,
        )

    if len(per_role) < 2 or len({item.value for item in per_role.values()}) == 1:
        return []

    observations = list(per_role.values())
    return [
        _finding(
            rule=rule,
            snapshot=snapshot,
            actual=", ".join(
                f"{item.role.value}={item.value}" for item in observations
            ),
            expected=f"{rule.property} identical across "
            f"{', '.join(role.value for role in per_role)}",
            evidence=_evidence(observations),
            deviation=None,
            summary=f"{rule.property} differs between "
            f"{' and '.join(role.value for role in per_role)}",
        )
    ]


def _majority_value(observations: list[Observation]) -> str:
    counts: dict[str, int] = {}
    for item in observations:
        counts[item.value] = counts.get(item.value, 0) + 1
    return max(sorted(counts), key=lambda value: counts[value])


def _describe_outliers(outliers: list[Observation]) -> str:
    shown = outliers[:MAX_CITED_NODES]
    text = ", ".join(f"{item.label}={item.value}" for item in shown)
    if len(outliers) > len(shown):
        text += f", and {len(outliers) - len(shown)} more"
    return text


def _evidence(observations: list[Observation]) -> list[Evidence]:
    return [
        ConfigEvidence(
            file=item.source_file, role=item.role, node=item.node, line=item.line
        )
        for item in observations[:MAX_CITED_NODES]
    ]


def _finding(
    *,
    rule: ConsistencyRule,
    snapshot: ClusterSnapshot,
    actual: str,
    expected: str,
    evidence: list[Evidence],
    deviation: float | None,
    summary: str,
) -> Finding:
    return Finding(
        rule_id=rule.id,
        fingerprint=compute_fingerprint(
            rule.id, snapshot.cluster, rule.property, evidence
        ),
        severity=rule.severity,
        domain=rule.domain,
        summary=rule.summary or summary,
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
