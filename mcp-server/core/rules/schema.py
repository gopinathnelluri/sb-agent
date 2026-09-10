"""Rule definitions and catalog loading.

Two rule shapes, both written in the same YAML list and told apart by which
key they carry:

* ``check:``       -- a property rule, one value against a threshold
* ``consistency:`` -- a fleet rule, comparing the same property across nodes
  or roles, which no single-property check can express

Validation is strict and happens at load time. An unrecognised field is an
error rather than an ignored line, because a rule silently disabled by a
typo is worse than one that fails loudly: it looks exactly like a clean
cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml

from core.models import DocRef, Role, Severity
from core.rules.checks import Check, RuleDefinitionError, build_check
from core.rules.file_checks import FileCheck, build_file_check
from core.rules.versions import VersionRange
from core.scopes import Scope

OnMissing: TypeAlias = Literal["skip", "fail", "pass"]
ConsistencyKind: TypeAlias = Literal["equal_across_nodes", "equal_across_roles"]

_ON_MISSING: frozenset[str] = frozenset({"skip", "fail", "pass"})
_CONSISTENCY_KINDS: frozenset[str] = frozenset(
    {"equal_across_nodes", "equal_across_roles"}
)

_COMMON_FIELDS = {
    "id",
    "next_step",
    "domain",
    "severity",
    "rationale",
    "source",
    "applies_to",
    "doc_ref",
}
_PROPERTY_FIELDS = _COMMON_FIELDS | {"property", "check", "on_missing", "summary"}
_CONSISTENCY_FIELDS = _COMMON_FIELDS | {"consistency", "summary"}
_FILE_FIELDS = _COMMON_FIELDS | {"file_check", "summary"}


@dataclass(frozen=True)
class AppliesTo:
    """Which roles and SEP versions a rule is written for."""

    roles: tuple[Role, ...]
    version: VersionRange

    @classmethod
    def parse(cls, raw: Any, rule_id: str) -> AppliesTo:
        if raw is None:
            return cls(
                roles=(Role.COORDINATOR, Role.WORKER), version=VersionRange.parse(None)
            )
        if not isinstance(raw, dict):
            raise RuleDefinitionError(f"{rule_id}: 'applies_to' must be a mapping.")
        unknown = set(raw) - {"role", "roles", "sep_version"}
        if unknown:
            raise RuleDefinitionError(
                f"{rule_id}: unknown applies_to field(s): {', '.join(sorted(unknown))}."
            )
        raw_roles = raw.get("roles") or raw.get("role")
        roles: tuple[Role, ...]
        if raw_roles is None:
            roles = (Role.COORDINATOR, Role.WORKER)
        else:
            names = raw_roles if isinstance(raw_roles, list) else [raw_roles]
            roles = tuple(_role(str(name), rule_id) for name in names)
        return cls(roles=roles, version=VersionRange.parse(raw.get("sep_version")))


@dataclass(frozen=True)
class PropertyRule:
    """One property compared against one threshold."""

    id: str
    domain: Scope
    property: str
    check: Check
    severity: Severity
    rationale: str
    applies_to: AppliesTo
    summary: str | None = None
    on_missing: OnMissing = "skip"
    doc_ref: DocRef | None = None
    next_step: str | None = None

    def headline(self) -> str:
        return self.summary or f"{self.property} {self.check.describe()}"


@dataclass(frozen=True)
class ConsistencyRule:
    """One property that must agree across the fleet.

    ``equal_across_nodes`` catches the single drifted worker.
    ``equal_across_roles`` catches coordinator/worker mismatches such as
    differing heap sizes.
    """

    id: str
    domain: Scope
    property: str
    kind: ConsistencyKind
    severity: Severity
    rationale: str
    applies_to: AppliesTo
    summary: str | None = None
    doc_ref: DocRef | None = None
    next_step: str | None = None

    def headline(self) -> str:
        if self.summary:
            return self.summary
        where = "nodes" if self.kind == "equal_across_nodes" else "roles"
        return f"{self.property} must match across {where}"


@dataclass(frozen=True)
class MetadataRule:
    """A check against a file's ownership and permissions.

    Matches files by glob rather than naming one property, because the
    concern -- "no config file should be world-readable" -- is about a class
    of files, not a specific setting inside one.
    """

    id: str
    domain: Scope
    file_pattern: str
    check: FileCheck
    severity: Severity
    rationale: str
    applies_to: AppliesTo
    summary: str | None = None
    doc_ref: DocRef | None = None
    next_step: str | None = None

    @property
    def property(self) -> str:
        """The subject this rule concerns, for coverage accounting."""
        return f"file:{self.file_pattern}"

    def headline(self) -> str:
        return self.summary or f"{self.file_pattern} {self.check.describe()}"


Rule: TypeAlias = PropertyRule | ConsistencyRule | MetadataRule


@dataclass(frozen=True)
class RuleCatalog:
    """Every rule loaded from ``core/rules/catalog``."""

    rules: tuple[Rule, ...] = ()
    sources: tuple[str, ...] = field(default=())

    @classmethod
    def load(cls, directory: Path | None = None) -> RuleCatalog:
        """Load and validate every ``.yaml`` file in the catalog directory."""
        root = directory or Path(__file__).parent / "catalog"
        rules: list[Rule] = []
        sources: list[str] = []
        seen: dict[str, str] = {}

        for path in sorted(root.glob("*.yaml")):
            sources.append(path.name)
            for raw in _read_documents(path):
                rule = parse_rule(raw)
                if rule.id in seen:
                    raise RuleDefinitionError(
                        f"Duplicate rule id {rule.id!r} in {path.name}; "
                        f"already defined in {seen[rule.id]}."
                    )
                seen[rule.id] = path.name
                rules.append(rule)

        return cls(rules=tuple(rules), sources=tuple(sources))

    def for_scopes(self, scopes: list[Scope]) -> tuple[Rule, ...]:
        """Rules whose domain is one of the requested scopes."""
        wanted = set(scopes)
        return tuple(rule for rule in self.rules if rule.domain in wanted)

    def properties_covered(self, scopes: list[Scope]) -> set[str]:
        """Every property some rule in scope looks at."""
        return {rule.property for rule in self.for_scopes(scopes)}

    def by_property(self, key: str) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.property == key)


def _read_documents(path: Path) -> list[dict[str, Any]]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleDefinitionError(f"{path.name} is not valid YAML: {exc}") from exc
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise RuleDefinitionError(f"{path.name} must contain a list of rules.")
    for entry in loaded:
        if not isinstance(entry, dict):
            raise RuleDefinitionError(f"{path.name} contains a non-mapping rule entry.")
    return loaded


def parse_rule(raw: dict[str, Any]) -> Rule:
    """Validate one rule mapping and build the corresponding rule object."""
    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        raise RuleDefinitionError("Every rule needs a non-empty 'id'.")

    kinds = [k for k in ("check", "consistency", "file_check") if k in raw]
    if len(kinds) != 1:
        raise RuleDefinitionError(
            f"{rule_id}: a rule needs exactly one of 'check', 'consistency' or "
            f"'file_check'; found {len(kinds)}."
        )
    has_check = kinds[0] == "check"

    allowed = {
        "check": _PROPERTY_FIELDS,
        "consistency": _CONSISTENCY_FIELDS,
        "file_check": _FILE_FIELDS,
    }[kinds[0]]
    unknown = set(raw) - allowed
    if unknown:
        raise RuleDefinitionError(
            f"{rule_id}: unknown field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )

    domain = _domain(raw, rule_id)
    severity = _severity(raw, rule_id)
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        raise RuleDefinitionError(
            f"{rule_id}: 'rationale' is required and must stand alone without "
            f"documentation lookup."
        )
    applies_to = AppliesTo.parse(raw.get("applies_to"), rule_id)
    doc_ref = _doc_ref(raw.get("doc_ref"), raw.get("source"), rule_id)
    summary = str(raw["summary"]).strip() if raw.get("summary") else None
    next_step = str(raw["next_step"]).strip() if raw.get("next_step") else None

    if has_check:
        prop = str(raw.get("property") or "").strip()
        if not prop:
            raise RuleDefinitionError(f"{rule_id}: 'property' is required.")
        on_missing = str(raw.get("on_missing") or "skip")
        if on_missing not in _ON_MISSING:
            raise RuleDefinitionError(
                f"{rule_id}: 'on_missing' must be one of "
                f"{', '.join(sorted(_ON_MISSING))}, got {on_missing!r}."
            )
        try:
            check = build_check(raw["check"])
        except RuleDefinitionError as exc:
            raise RuleDefinitionError(f"{rule_id}: {exc}") from exc
        return PropertyRule(
            id=rule_id,
            domain=domain,
            property=prop,
            check=check,
            severity=severity,
            rationale=rationale,
            applies_to=applies_to,
            summary=summary,
            on_missing=_narrow_on_missing(on_missing),
            doc_ref=doc_ref,
            next_step=next_step,
        )

    if kinds[0] == "file_check":
        spec = raw["file_check"]
        if not isinstance(spec, dict):
            raise RuleDefinitionError(f"{rule_id}: 'file_check' must be a mapping.")
        pattern = str(spec.get("files") or "").strip()
        if not pattern:
            raise RuleDefinitionError(
                f"{rule_id}: file_check needs a 'files' glob, such as '*.properties'."
            )
        try:
            file_check = build_file_check(spec)
        except ValueError as exc:
            raise RuleDefinitionError(f"{rule_id}: {exc}") from exc
        return MetadataRule(
            id=rule_id,
            domain=domain,
            file_pattern=pattern,
            check=file_check,
            severity=severity,
            rationale=rationale,
            applies_to=applies_to,
            summary=summary,
            doc_ref=doc_ref,
            next_step=next_step,
        )

    spec = raw["consistency"]
    if not isinstance(spec, dict):
        raise RuleDefinitionError(f"{rule_id}: 'consistency' must be a mapping.")
    unknown_spec = set(spec) - {"kind", "property"}
    if unknown_spec:
        raise RuleDefinitionError(
            f"{rule_id}: unknown consistency field(s): "
            f"{', '.join(sorted(unknown_spec))}."
        )
    kind = str(spec.get("kind") or "")
    if kind not in _CONSISTENCY_KINDS:
        raise RuleDefinitionError(
            f"{rule_id}: consistency 'kind' must be one of "
            f"{', '.join(sorted(_CONSISTENCY_KINDS))}, got {kind!r}."
        )
    prop = str(spec.get("property") or "").strip()
    if not prop:
        raise RuleDefinitionError(f"{rule_id}: consistency needs a 'property'.")
    return ConsistencyRule(
        id=rule_id,
        domain=domain,
        property=prop,
        kind=_narrow_consistency(kind),
        severity=severity,
        rationale=rationale,
        applies_to=applies_to,
        summary=summary,
        doc_ref=doc_ref,
        next_step=next_step,
    )


def _domain(raw: dict[str, Any], rule_id: str) -> Scope:
    value = str(raw.get("domain") or "").strip()
    try:
        return Scope(value)
    except ValueError as exc:
        known = ", ".join(scope.value for scope in Scope)
        raise RuleDefinitionError(
            f"{rule_id}: unknown domain {value!r}. Known domains: {known}."
        ) from exc


def _severity(raw: dict[str, Any], rule_id: str) -> Severity:
    value = str(raw.get("severity") or "").strip()
    try:
        return Severity(value)
    except ValueError as exc:
        known = ", ".join(item.value for item in Severity)
        raise RuleDefinitionError(
            f"{rule_id}: unknown severity {value!r}. Known severities: {known}."
        ) from exc


def _role(name: str, rule_id: str) -> Role:
    try:
        return Role(name)
    except ValueError as exc:
        known = ", ".join(item.value for item in Role)
        raise RuleDefinitionError(
            f"{rule_id}: unknown role {name!r}. Known roles: {known}."
        ) from exc


def _doc_ref(raw: Any, source: Any, rule_id: str) -> DocRef | None:
    if raw is None:
        return DocRef(source_doc=str(source)) if source else None
    if not isinstance(raw, dict):
        raise RuleDefinitionError(f"{rule_id}: 'doc_ref' must be a mapping.")
    unknown = set(raw) - {"source_doc", "anchor", "page", "version"}
    if unknown:
        raise RuleDefinitionError(
            f"{rule_id}: unknown doc_ref field(s): {', '.join(sorted(unknown))}."
        )
    return DocRef(
        source_doc=str(raw.get("source_doc") or source or "unknown"),
        anchor=_optional_str(raw.get("anchor")),
        page=int(raw["page"]) if raw.get("page") is not None else None,
        version=_optional_str(raw.get("version")),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _narrow_on_missing(value: str) -> OnMissing:
    assert value in _ON_MISSING
    return value  # type: ignore[return-value]


def _narrow_consistency(value: str) -> ConsistencyKind:
    assert value in _CONSISTENCY_KINDS
    return value  # type: ignore[return-value]
