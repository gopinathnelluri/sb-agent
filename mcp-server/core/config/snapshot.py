"""The parsed, per-node view of one cluster's config backup.

This is the single input to rule evaluation. It is built once per request and
is entirely immutable, which is what makes a run reproducible: the engine
cannot reach back to storage, and two runs over the same backup see byte-
identical inputs.

Fleet drift is first class here. A property is resolved to the value most
nodes in a role agree on, plus an explicit list of the nodes that disagree.
Averaging that away would hide the single misconfigured worker that is
usually the actual answer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch

from core.models import ParseFailure, Role
from core.parsers import HEAP_MAX_FLAG, ParsedFile
from core.scopes import SCOPES, Scope
from core.units import UnitParseError, parse_data_size

JVM_CONFIG = "jvm.config"
REPRESENTATIVE = "*"


@dataclass(frozen=True)
class NodeConfig:
    """Every config file belonging to one node.

    ``node`` is ``None`` for a role backed up without per-node directories.
    """

    role: Role
    files: dict[str, ParsedFile]
    node: str | None = None

    @property
    def label(self) -> str:
        return self.node or f"{self.role.value} (representative)"


@dataclass(frozen=True)
class Observation:
    """One node's value for one property, with where it was read from."""

    node: str | None
    label: str
    role: Role
    value: str
    source_file: str
    line: int | None = None


@dataclass(frozen=True)
class ResolvedProperty:
    """One property's value across a role, with disagreement made explicit."""

    key: str
    value: str
    role: Role
    source_file: str
    line: int | None = None
    node: str | None = None
    differing_nodes: dict[str, str] = field(default_factory=dict)

    @property
    def consistent_across_nodes(self) -> bool:
        return not self.differing_nodes


@dataclass(frozen=True)
class ClusterSnapshot:
    """A cluster's whole backup, parsed."""

    cluster: str
    nodes: list[NodeConfig]
    sep_version: str | None = None
    sep_version_source: str = "unknown"
    backed_up_at: str | None = None
    backup_age_days: int | None = None
    parse_failures: list[ParseFailure] = field(default_factory=list)

    # -- structure ------------------------------------------------------

    def roles(self) -> list[Role]:
        """Roles present in this backup, in a stable order."""
        seen = {node.role for node in self.nodes}
        return [role for role in Role if role in seen]

    def nodes_for(self, role: Role) -> list[NodeConfig]:
        return [node for node in self.nodes if node.role == role]

    def node_count(self, role: Role) -> int:
        return len(self.nodes_for(role))

    def node_counts(self) -> dict[str, int]:
        return {role.value: self.node_count(role) for role in self.roles()}

    def files_for(self, role: Role) -> list[str]:
        """Distinct file paths backed up for a role."""
        paths = {path for node in self.nodes_for(role) for path in node.files}
        return sorted(paths)

    # -- resolution -----------------------------------------------------

    def observe(self, key: str, role: Role) -> list[Observation]:
        """Every node's value for a property, one entry per node that sets it.

        The raw material for both resolution and the fleet consistency rules,
        which need to cite each disagreeing node individually.
        """
        seen: list[Observation] = []
        for node in self.nodes_for(role):
            for _, parsed in sorted(node.files.items()):
                if key in parsed.values:
                    seen.append(
                        Observation(
                            node=node.node,
                            label=node.label,
                            role=role,
                            value=parsed.values[key],
                            source_file=parsed.path,
                            line=parsed.line_of(key),
                        )
                    )
                    break
        return seen

    def resolve(self, key: str, role: Role) -> ResolvedProperty | None:
        """Resolve a property across every node in a role.

        Returns ``None`` when no node in the role defines it -- which is a
        distinct outcome from "defined but wrong", and rules treat it as such.
        """
        observations = self.observe(key, role)
        if not observations:
            return None

        majority, _ = Counter(item.value for item in observations).most_common(1)[0]
        source = next(item for item in observations if item.value == majority)
        return ResolvedProperty(
            key=key,
            value=majority,
            role=role,
            source_file=source.source_file,
            line=source.line,
            node=source.node,
            differing_nodes={
                item.label: item.value
                for item in observations
                if item.value != majority
            },
        )

    def resolve_anywhere(self, key: str) -> ResolvedProperty | None:
        """Resolve a property in whichever role defines it, coordinator first."""
        for role in self.roles():
            resolved = self.resolve(key, role)
            if resolved is not None:
                return resolved
        return None

    def heap_bytes(self, role: Role) -> int | None:
        """Maximum JVM heap for a role, in bytes, from ``-Xmx``.

        The denominator for every memory ratio rule, so a role whose
        ``jvm.config`` is missing or unparseable yields ``None`` and those
        rules are skipped for missing input rather than guessing a default.
        """
        resolved = self.resolve(HEAP_MAX_FLAG, role)
        if resolved is None:
            return None
        try:
            return parse_data_size(resolved.value)
        except UnitParseError:
            return None

    # -- scoping --------------------------------------------------------

    def keys_in_scope(self, scopes: list[Scope]) -> set[str]:
        """Every property key defined in the files a scope covers.

        Used to report which properties no rule looked at, so the parent can
        tell "we checked and it is fine" from "nothing covers this".
        """
        patterns = {pattern for scope in scopes for pattern in SCOPES[scope].files}
        keys: set[str] = set()
        for node in self.nodes:
            for path, parsed in node.files.items():
                if any(_matches(path, pattern) for pattern in patterns):
                    keys.update(parsed.values)
        return keys


def _matches(path: str, pattern: str) -> bool:
    return fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern)
