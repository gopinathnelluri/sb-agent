"""Post-hoc enrichment from injected context.

Runs strictly after evaluation, over a finished list of findings. It receives
findings it cannot un-make and a catalog it cannot change, so the only things
it can do are the three the contract permits:

* **Enrich**  -- replace the wording of a rationale on a finding that already
  fired, marking it ``injected_context`` so the parent can tell whose words
  those are.
* **Annotate** -- note guidance about a property no rule covers, in a
  separate list that is explicitly not findings.
* **Flag a conflict** -- when supplied guidance states a threshold that
  disagrees with the catalog's, report both rather than picking a winner.

What it cannot do: change a severity, a value, an expectation, an evidence
list, or the set of findings. Those fields are copied through untouched, and
``tests/test_context_independence.py`` asserts it byte for byte.

Conflict detection is deliberately narrow. Deciding whether two paragraphs of
prose disagree is a judgement call, and judgement calls do not belong in this
service -- so only a numeric threshold that can be parsed out of the guidance
is ever compared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from core.models import (
    Annotation,
    DocRef,
    Finding,
    GuidanceConflict,
    InjectedContext,
    Passage,
    RationaleSource,
)
from core.rules.checks import Max, MaxRatio, Min, MinRatio
from core.rules.schema import PropertyRule, RuleCatalog
from core.units import UnitParseError, format_data_size, parse_data_size

RATIO_TOLERANCE = 0.005

_PERCENTAGE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_DATA_SIZE = re.compile(r"\b(\d+(?:\.\d+)?)\s*([kKmMgGtT][bB]?)\b")


@dataclass(frozen=True)
class EnrichmentResult:
    """Findings with improved wording, plus everything context added alongside."""

    findings: list[Finding] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    guidance_conflicts: list[GuidanceConflict] = field(default_factory=list)


def enrich(
    findings: list[Finding],
    catalog: RuleCatalog,
    context: InjectedContext | None,
    *,
    detected_version: str | None = None,
) -> EnrichmentResult:
    """Apply injected context to a finished list of findings.

    With ``context=None`` this is the identity function on ``findings``.
    """
    if context is None:
        return EnrichmentResult(findings=list(findings))

    return EnrichmentResult(
        findings=[_reword(finding, context) for finding in findings],
        annotations=_annotate(catalog, context),
        guidance_conflicts=_conflicts(catalog, context, detected_version),
    )


def _reword(finding: Finding, context: InjectedContext) -> Finding:
    """Swap in a better rationale, changing nothing else about the finding.

    ``replace`` on a frozen dataclass is what makes this safe: every other
    field is carried across by construction rather than by remembering to
    copy it.
    """
    guidance = _guidance_for(finding.subject, context)
    if guidance is None:
        return finding
    return replace(
        finding,
        rationale=guidance,
        rationale_source=RationaleSource.INJECTED_CONTEXT,
        doc_ref=finding.doc_ref or _doc_ref_from(context.passages),
    )


def _annotate(catalog: RuleCatalog, context: InjectedContext) -> list[Annotation]:
    """Note guidance about properties the catalog has no rule for."""
    covered = {rule.property for rule in catalog.rules}
    return [
        Annotation(
            subject=key,
            note=guidance,
            source="injected_context",
            doc_ref=_doc_ref_from(context.passages),
        )
        for key, guidance in sorted(context.properties.items())
        if key not in covered
    ]


def _conflicts(
    catalog: RuleCatalog, context: InjectedContext, detected_version: str | None
) -> list[GuidanceConflict]:
    """Report guidance that numerically contradicts a rule."""
    conflicts: list[GuidanceConflict] = []

    if (
        context.sep_version_hint
        and detected_version
        and context.sep_version_hint.strip() != detected_version.strip()
    ):
        conflicts.append(
            GuidanceConflict(
                rule_id="sep_version_detection",
                catalog_says=f"detected SEP version {detected_version}",
                context_says=f"context hinted SEP version {context.sep_version_hint}",
            )
        )

    for key, guidance in sorted(context.properties.items()):
        for rule in catalog.by_property(key):
            if not isinstance(rule, PropertyRule):
                continue
            stated = _threshold_of(rule)
            supplied = _threshold_in(guidance, rule)
            if stated is None or supplied is None or _close(stated[1], supplied[1]):
                continue
            conflicts.append(
                GuidanceConflict(
                    rule_id=rule.id,
                    catalog_says=f"{key} {stated[0]}",
                    context_says=f"guidance states {supplied[0]}",
                    doc_ref=rule.doc_ref,
                )
            )
    return conflicts


def _threshold_of(rule: PropertyRule) -> tuple[str, float] | None:
    """The numeric threshold a rule states, when it states one comparably."""
    check = rule.check
    if isinstance(check, MaxRatio | MinRatio):
        return check.describe(), check.ratio
    if isinstance(check, Max | Min):
        try:
            return check.describe(), float(parse_data_size(check.literal))
        except UnitParseError:
            return None
    return None


def _threshold_in(guidance: str, rule: PropertyRule) -> tuple[str, float] | None:
    """Pull a comparable threshold out of guidance prose, if one is stated."""
    if isinstance(rule.check, MaxRatio | MinRatio):
        match = _PERCENTAGE.search(guidance)
        if match is None:
            return None
        ratio = float(match.group(1)) / 100
        return f"{match.group(1)}%", ratio
    match = _DATA_SIZE.search(guidance)
    if match is None:
        return None
    try:
        size = float(parse_data_size(f"{match.group(1)}{match.group(2)}"))
    except UnitParseError:
        return None
    return format_data_size(size), size


def _close(left: float, right: float) -> bool:
    if left == 0:
        return right == 0
    return abs(left - right) / abs(left) <= RATIO_TOLERANCE


def _guidance_for(subject: str | None, context: InjectedContext) -> str | None:
    if subject is None:
        return None
    guidance = context.properties.get(subject)
    return guidance.strip() if guidance and guidance.strip() else None


def _doc_ref_from(passages: list[Passage]) -> DocRef | None:
    if not passages:
        return None
    first = passages[0]
    return DocRef(source_doc=first.doc, page=first.page)
