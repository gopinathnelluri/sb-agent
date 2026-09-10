"""Data contracts returned by every tool.

Plain stdlib dataclasses on purpose: ``core/`` must not depend on an agent
framework, an LLM client or the MCP SDK (CLAUDE.md). The MCP layer derives
JSON Schema and serialises these for free, so nothing is lost by staying
dependency-free here.

Do not add ``slots=True`` to these dataclasses. When one is used directly as
a tool's return annotation, the slot descriptors are mistaken for field
defaults and the MCP SDK silently drops the tool's output schema. Immutability
comes from ``frozen=True``, which is the part that matters here.
``test_every_tool_has_input_and_output_schemas`` guards against a regression.

Two invariants encoded structurally rather than by convention:

* ``Finding`` and ``Annotation`` are different types living in different
  lists. Injected context can produce an ``Annotation``; it can never produce
  a ``Finding``. See ``docs`` in ``RuleRunResult``.
* Every result carries ``Coverage``. An empty findings list only means "no
  issues" when coverage says the rules actually ran.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypeAlias

from core.scopes import Scope

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Severity(StrEnum):
    """How much the finding should worry the reader."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort key. Lower is more severe."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class Role(StrEnum):
    """Which kind of node a config file belongs to."""

    COORDINATOR = "coordinator"
    WORKER = "worker"
    HMS = "hms"
    RANGER = "ranger"


class QueryDomain(StrEnum):
    """What aspect of a query's execution a finding is about.

    Deliberately separate from ``Scope``. A ``Scope`` names a domain of
    cluster *configuration* and the files that hold it; a ``QueryDomain``
    names an aspect of how one query *ran*. Reusing the config enum for
    query findings would claim, for instance, that a query which scanned too
    much data is a fact about ``catalog/*.properties`` -- which it is not.

    Config auditing and query analysis are two use cases. They share the
    shape of a finding, not its taxonomy.
    """

    DATA_ACCESS = "data_access"
    MEMORY = "memory"
    SCHEDULING = "scheduling"
    QUERY_SHAPE = "query_shape"
    EXECUTION = "execution"


class Owner(StrEnum):
    """Who can actually act on a finding.

    Severity says how bad; this says whose problem it is. An analyst reading
    two high-severity findings needs to know which one they can fix in their
    SQL and which one belongs to whoever runs the cluster -- without it, the
    only safe reading is "everything is my fault", which is wrong and wastes
    their time.
    """

    QUERY_AUTHOR = "query_author"
    PLATFORM_TEAM = "platform_team"
    DATA_OWNER = "data_owner"


class RationaleSource(StrEnum):
    """Where a finding's rationale text came from.

    Required on every finding so the parent can tell our own catalog text
    apart from text that arrived through injected context.
    """

    RULE_CATALOG = "rule_catalog"
    INJECTED_CONTEXT = "injected_context"
    LOCAL_INDEX = "local_index"


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigEvidence:
    """Points at the exact place in a config file that triggered a finding."""

    file: str
    role: Role
    node: str | None = None
    line: int | None = None
    kind: Literal["config"] = "config"


@dataclass(frozen=True)
class QueryEvidence:
    """Points at the stage/operator in a query plan that triggered a finding."""

    stage_id: str
    operator_id: str | None = None
    task_id: str | None = None
    metric: str | None = None
    value: str | None = None
    kind: Literal["query"] = "query"


Evidence: TypeAlias = ConfigEvidence | QueryEvidence


@dataclass(frozen=True)
class DocRef:
    """A pointer into documentation, never the documentation text itself.

    The parent may expand this through its own RAG. A finding's ``rationale``
    always stands alone without that expansion.
    """

    source_doc: str
    anchor: str | None = None
    page: int | None = None
    version: str | None = None


# --------------------------------------------------------------------------
# Findings and annotations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A deterministic rule or detector result, always backed by evidence.

    Produced only by ``core/rules`` and ``core/analysis/detectors``. Never by
    injected context, and never by a model.
    """

    rule_id: str
    fingerprint: str
    severity: Severity
    domain: Scope | QueryDomain
    summary: str
    rationale: str
    rationale_source: RationaleSource
    evidence: list[Evidence]
    subject: str | None = None
    actual: str | None = None
    expected: str | None = None
    deviation: float | None = None
    doc_ref: DocRef | None = None
    owner: Owner = Owner.PLATFORM_TEAM
    next_step: str | None = None
    is_root_cause: bool = False


@dataclass(frozen=True)
class Annotation:
    """A non-decisional note derived from injected context.

    Deliberately not a ``Finding`` and deliberately in a separate list. This
    is the only channel through which injected context can put anything new
    in front of the parent, and it is explicitly marked as unvalidated.
    """

    subject: str
    note: str
    source: Literal["injected_context", "local_index"]
    doc_ref: DocRef | None = None


@dataclass(frozen=True)
class GuidanceConflict:
    """Injected guidance disagrees with a rule in our catalog.

    Emitted instead of preferring either side. Usually means the catalog is
    stale against a newer document.
    """

    rule_id: str
    catalog_says: str
    context_says: str
    doc_ref: DocRef | None = None


def compute_fingerprint(
    rule_id: str,
    cluster: str,
    subject: str | None,
    evidence: Sequence[Evidence],
) -> str:
    """Stable identity for a finding, so the parent can refer back to it.

    Deterministic across runs and processes: same inputs, same fingerprint.
    Used for de-duplication and for "is this the same problem as last week".
    """
    parts = [rule_id, cluster, subject or ""]
    for item in sorted(_evidence_key(e) for e in evidence):
        parts.append(item)
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return digest[:16]


def _evidence_key(evidence: Evidence) -> str:
    if isinstance(evidence, ConfigEvidence):
        return f"config:{evidence.role.value}:{evidence.node or ''}:{evidence.file}"
    return f"query:{evidence.stage_id}:{evidence.operator_id or ''}"


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseFailure:
    """A config file we could not read or understand."""

    file: str
    role: Role
    reason: str
    node: str | None = None


@dataclass(frozen=True)
class Coverage:
    """What actually got checked.

    Without this, an empty findings list is ambiguous: it could mean the
    cluster is healthy, or it could mean every rule was skipped because the
    SEP version was undetectable. The parent must be able to tell those
    apart, so ``complete`` is part of the contract, not a debug field.
    """

    rules_evaluated: int
    rules_skipped_version: int
    rules_skipped_missing_input: int
    files_parsed: int
    nodes_seen: int
    complete: bool
    parse_failures: list[ParseFailure] = field(default_factory=list)
    properties_uncovered: list[str] = field(default_factory=list)
    blind_spots: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        rules_evaluated: int,
        rules_skipped_version: int = 0,
        rules_skipped_missing_input: int = 0,
        files_parsed: int = 0,
        nodes_seen: int = 0,
        parse_failures: list[ParseFailure] | None = None,
        properties_uncovered: list[str] | None = None,
        blind_spots: list[str] | None = None,
    ) -> Coverage:
        """Construct coverage, deriving ``complete`` from the blind spots.

        ``complete`` is never passed in by hand: it is true exactly when
        nothing prevented the run from being conclusive.
        """
        failures = parse_failures or []
        spots = list(blind_spots or [])
        if failures:
            spots.append(f"{len(failures)} config file(s) failed to parse")
        if rules_skipped_missing_input:
            spots.append(
                f"{rules_skipped_missing_input} check(s) skipped: required "
                "input values were not available"
            )
        if rules_evaluated == 0:
            spots.append("no rules were evaluated at all")
        # Deduplicate while preserving order: a caller may forward blind spots
        # from an inner coverage that already derived the same message.
        unique = list(dict.fromkeys(spots))
        return cls(
            rules_evaluated=rules_evaluated,
            rules_skipped_version=rules_skipped_version,
            rules_skipped_missing_input=rules_skipped_missing_input,
            files_parsed=files_parsed,
            nodes_seen=nodes_seen,
            complete=not unique,
            parse_failures=failures,
            properties_uncovered=list(properties_uncovered or []),
            blind_spots=unique,
        )


# --------------------------------------------------------------------------
# Injected context (non-decisional)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    """A documentation excerpt supplied by the parent's RAG."""

    text: str
    doc: str
    page: int | None = None


@dataclass(frozen=True)
class InjectedContext:
    """Optional enrichment supplied by the parent.

    Permitted effects: improve the wording of a rationale on a finding that
    already fired, annotate a property no rule covers, and supply a SEP
    version when we could not detect one.

    Forbidden effects: setting a threshold, suppressing a finding, adding a
    finding. This is enforced structurally -- findings are computed before
    this object is read, by code that cannot import it.
    """

    source: str = "parent_rag"
    properties: dict[str, str] = field(default_factory=dict)
    passages: list[Passage] = field(default_factory=list)
    sep_version_hint: str | None = None


# --------------------------------------------------------------------------
# Cluster and config views
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterInfo:
    """One row of the cluster inventory."""

    name: str
    sep_version: str | None
    sep_version_source: Literal["backup_manifest", "config", "hint", "unknown"]
    backed_up_at: str | None
    backup_age_days: int | None
    roles: list[Role]
    node_counts: dict[str, int]


@dataclass(frozen=True)
class PropertyValue:
    """One resolved config property, plus how consistent it is across nodes.

    ``value`` is the value shared by most nodes in the role. When nodes
    disagree, ``differing_nodes`` names the outliers -- that drift is often
    the actual problem, so it must not be silently averaged away.
    """

    key: str
    value: str
    role: Role
    source_file: str
    consistent_across_nodes: bool = True
    differing_nodes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigSummary:
    """Key properties for the requested scopes, not whole files."""

    cluster: str
    scopes: list[Scope]
    sep_version: str | None
    properties: list[PropertyValue]
    anomalies: list[str]
    source_files: list[str]
    coverage: Coverage


@dataclass(frozen=True)
class ConfigDetail:
    """Full text of a single config file, after outbound redaction."""

    cluster: str
    role: Role
    file: str
    content: str
    redaction_verified: bool
    redactions_applied: list[str]
    node: str | None = None


@dataclass(frozen=True)
class PropertyDifference:
    """One property that differs between two clusters."""

    key: str
    role: Role
    value_a: str | None
    value_b: str | None
    classification: Literal["expected", "unexpected", "unknown"]


@dataclass(frozen=True)
class ClusterDiff:
    """Result of comparing two clusters within a set of scopes."""

    cluster_a: str
    cluster_b: str
    scopes: list[Scope]
    differences: list[PropertyDifference]
    coverage: Coverage


@dataclass(frozen=True)
class RuleRunResult:
    """Result of evaluating the rule catalog against a cluster.

    ``findings`` is produced with no access to injected context.
    ``annotations`` and ``guidance_conflicts`` are the only fields context
    can influence, and they are advisory.
    """

    cluster: str
    scopes: list[Scope]
    sep_version: str | None
    findings: list[Finding]
    coverage: Coverage
    annotations: list[Annotation] = field(default_factory=list)
    guidance_conflicts: list[GuidanceConflict] = field(default_factory=list)
    backup_age_days: int | None = None


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Rank findings so the most useful are read first.

    A validation or analysis can fire dozens of findings and the reader
    attends to the top of the list, so ordering is part of the contract, not
    a presentation detail.

    Within a severity, a root cause outranks a symptom. A finding that says
    *why* something happened is more actionable than one that says *what*
    happened, and without this a diagnosis can sort below the very symptom it
    explains.

    Remaining ties break on relative deviation (a value 300% over a limit
    outranks one 5% over), then on rule id so the output stays deterministic.
    """
    return sorted(
        findings,
        key=lambda f: (
            f.severity.rank,
            not f.is_root_cause,
            -(f.deviation or 0.0),
            f.rule_id,
        ),
    )
