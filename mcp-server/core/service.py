"""The real ``ConfigService``: use case 1, end to end.

Orchestration only. It loads a snapshot, hands it to the engine, and turns
the result into the wire types. Every decision about what counts as a problem
lives in ``core/rules``; every decision about where bytes come from lives in
an adapter.

The ordering in :meth:`run_rules` is the contract, not a style choice:
findings are computed first, by code that has no parameter through which
injected context could arrive, and enrichment is a separate pass over the
finished list.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from core.config.diffing import classify
from core.config.layout import FileKind
from core.config.metadata import FileMetadata, parse_metadata
from core.config.redaction import find_unredacted, redact_text
from core.config.snapshot import ClusterSnapshot, NodeConfig
from core.enrich import enrich
from core.errors import (
    ClusterNotFoundError,
    ConfigFileNotFoundError,
    UnredactedSecretError,
)
from core.models import (
    ClusterDiff,
    ClusterInfo,
    ConfigDetail,
    ConfigSummary,
    Coverage,
    InjectedContext,
    ParseFailure,
    PropertyDifference,
    PropertyValue,
    Role,
    RuleRunResult,
    sort_findings,
)
from core.parsers import ConfigParseError, ParsedFile, accepts_parsed_sibling
from core.parsers import parse as parse_config
from core.parsers.parsed_json import disagreements as disagreements_between
from core.parsers.parsed_json import parse_parsed_json
from core.ports import BackupFile, BackupLayout, ConfigRepository
from core.rules.engine import evaluate
from core.rules.schema import RuleCatalog
from core.scopes import Scope

VersionSource = Literal["backup_manifest", "config", "hint", "unknown"]

DEFAULT_CACHE_SIZE = 8
STALE_BACKUP_DAYS = 14

Clock = Callable[[], datetime]


class ConfigValidationService:
    """Implements :class:`core.ports.ConfigService` over a backup repository."""

    def __init__(
        self,
        repository: ConfigRepository,
        catalog: RuleCatalog | None = None,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._catalog = catalog or RuleCatalog.load()
        self._cache: OrderedDict[tuple[str, str | None], ClusterSnapshot] = (
            OrderedDict()
        )
        self._cache_size = cache_size
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- tools ----------------------------------------------------------

    def list_clusters(self) -> list[ClusterInfo]:
        """Inventory every cluster with a backup.

        Deliberately reads no config file. The manifest carries the version
        and timestamp, and roles and node counts come from a prefix listing,
        so the cost is two cheap calls per cluster rather than one parse of
        every file it contains. On a fleet of hundreds of clusters that is
        the difference between a usable inventory and thousands of object
        reads for a question nobody asked about file contents.
        """
        clusters: list[ClusterInfo] = []
        for name in self._repository.list_clusters():
            manifest = self._repository.manifest(name)
            layout = self._repository.layout(name)
            roles = [role for role in Role if role.value in layout.roles]
            clusters.append(
                ClusterInfo(
                    name=name,
                    sep_version=manifest.sep_version,
                    sep_version_source=(
                        "backup_manifest" if manifest.sep_version else "unknown"
                    ),
                    backed_up_at=manifest.backed_up_at,
                    backup_age_days=self._age_days(manifest.backed_up_at),
                    roles=roles,
                    node_counts={
                        name: len(hosts) for name, hosts in layout.roles.items()
                    },
                )
            )
        return clusters

    def describe_backup_layout(self, cluster: str) -> BackupLayout:
        """What is in this cluster's backup, without reading any of it."""
        if cluster not in self._repository.list_clusters():
            raise ClusterNotFoundError(cluster, self._repository.list_clusters())
        return self._repository.layout(cluster)

    def get_config_summary(self, cluster: str, scopes: list[Scope]) -> ConfigSummary:
        snapshot = self._snapshot(cluster)
        keys = sorted(snapshot.keys_in_scope(scopes) & self._scope_properties(scopes))
        properties: list[PropertyValue] = []
        anomalies: list[str] = []
        source_files: set[str] = set()

        for role in snapshot.roles():
            for key in keys:
                resolved = snapshot.resolve(key, role)
                if resolved is None:
                    continue
                source_files.add(f"{role.value}/{resolved.source_file}")
                properties.append(
                    PropertyValue(
                        key=resolved.key,
                        value=resolved.value,
                        role=role,
                        source_file=resolved.source_file,
                        consistent_across_nodes=resolved.consistent_across_nodes,
                        differing_nodes=dict(resolved.differing_nodes),
                    )
                )
                if resolved.differing_nodes:
                    anomalies.append(
                        f"{key} differs on {len(resolved.differing_nodes)} of "
                        f"{snapshot.node_count(role)} {role.value} nodes"
                    )

        return ConfigSummary(
            cluster=cluster,
            scopes=list(scopes),
            sep_version=snapshot.sep_version,
            properties=properties,
            anomalies=anomalies,
            source_files=sorted(source_files),
            coverage=self._coverage(snapshot, scopes, evaluated=len(properties)),
        )

    def run_rules(
        self,
        cluster: str,
        scopes: list[Scope],
        context: InjectedContext | None = None,
    ) -> RuleRunResult:
        snapshot = self._snapshot(cluster)
        version, source = self._resolve_version(snapshot, context)

        # Step 1: findings. `evaluate` takes a plain version string and has no
        # access to `context` -- that is what makes the contract structural.
        result = evaluate(snapshot, self._catalog, scopes, version)

        # Step 2: enrichment, over an already-finished list.
        enriched = enrich(
            result.findings,
            self._catalog,
            context,
            detected_version=snapshot.sep_version,
        )

        blind_spots: list[str] = []
        if result.rules_skipped_unknown_version:
            blind_spots.append(
                f"{result.rules_skipped_unknown_version} rule(s) were skipped because "
                f"the cluster's SEP version could not be determined"
            )
        if snapshot.backup_age_days and snapshot.backup_age_days > STALE_BACKUP_DAYS:
            blind_spots.append(
                f"the backup is {snapshot.backup_age_days} days old; findings "
                f"describe the config as of {snapshot.backed_up_at}"
            )

        return RuleRunResult(
            cluster=cluster,
            scopes=list(scopes),
            sep_version=version if source != "unknown" else None,
            findings=sort_findings(enriched.findings),
            coverage=self._coverage(
                snapshot,
                scopes,
                evaluated=result.rules_evaluated,
                skipped_version=result.rules_skipped_version,
                skipped_missing=result.rules_skipped_missing_input,
                checked=result.properties_checked,
                blind_spots=blind_spots,
            ),
            annotations=enriched.annotations,
            guidance_conflicts=enriched.guidance_conflicts,
            backup_age_days=snapshot.backup_age_days,
        )

    def get_config_detail(
        self, cluster: str, role: Role, file: str, node: str | None = None
    ) -> ConfigDetail:
        snapshot = self._snapshot(cluster)
        match = self._locate(snapshot, role, file, node)
        if match is None:
            raise ConfigFileNotFoundError(cluster, role.value, file, node)

        found_node, parsed = match
        hits = find_unredacted(parsed.values)
        if hits:
            raise UnredactedSecretError(cluster, file, [hit.key for hit in hits])

        content, masked = redact_text(parsed.raw, parsed.values)
        return ConfigDetail(
            cluster=cluster,
            role=role,
            file=parsed.path,
            node=found_node,
            content=content,
            redaction_verified=True,
            redactions_applied=masked,
        )

    def diff_clusters(
        self, cluster_a: str, cluster_b: str, scopes: list[Scope]
    ) -> ClusterDiff:
        left = self._snapshot(cluster_a)
        right = self._snapshot(cluster_b)
        governed = self._scope_properties(scopes)
        keys = sorted(left.keys_in_scope(scopes) | right.keys_in_scope(scopes))
        differences: list[PropertyDifference] = []

        for role in sorted(
            set(left.roles()) | set(right.roles()), key=lambda r: r.value
        ):
            for key in keys:
                first = left.resolve(key, role)
                second = right.resolve(key, role)
                if first is None and second is None:
                    continue
                value_a = first.value if first else None
                value_b = second.value if second else None
                if value_a == value_b:
                    continue
                differences.append(
                    PropertyDifference(
                        key=key,
                        role=role,
                        value_a=value_a,
                        value_b=value_b,
                        classification=classify(key, key in governed),
                    )
                )

        return ClusterDiff(
            cluster_a=cluster_a,
            cluster_b=cluster_b,
            scopes=list(scopes),
            differences=_rank(differences),
            coverage=self._coverage(left, scopes, evaluated=len(keys)),
        )

    # -- internals ------------------------------------------------------

    def _snapshot(self, cluster: str) -> ClusterSnapshot:
        """Load and parse a cluster backup, reusing a cached parse when possible.

        Cached on the backup timestamp, so a fresh backup invalidates the
        entry without needing an explicit flush. In memory only: the container
        runs as a random UID with no writable home.
        """
        if cluster not in self._repository.list_clusters():
            raise ClusterNotFoundError(cluster, self._repository.list_clusters())

        manifest = self._repository.manifest(cluster)
        key = (cluster, manifest.backed_up_at)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        snapshot = self._build_snapshot(cluster)
        self._cache[key] = snapshot
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return snapshot

    def _build_snapshot(self, cluster: str) -> ClusterSnapshot:
        manifest = self._repository.manifest(cluster)
        entries = self._repository.list_files(cluster)

        grouped: dict[tuple[Role, str | None], dict[str, ParsedFile]] = {}
        metadata: dict[tuple[Role, str | None], dict[str, FileMetadata]] = {}
        failures: list[ParseFailure] = []
        disagreements: list[str] = []

        # A config whose contents were too sensitive to back up still has its
        # metadata, so track which bases actually have a file behind them.
        backed_up = {
            (f.role, f.node, f.base) for f in entries if f.kind is FileKind.CONFIG
        }
        siblings = {
            (f.role, f.node, f.base): f
            for f in entries
            if f.kind is FileKind.PARSED_CONFIG
        }

        for entry in entries:
            key = (entry.role, entry.node)
            try:
                text = self._repository.read(cluster, entry)
            except (ConfigFileNotFoundError, OSError) as exc:
                failures.append(
                    ParseFailure(
                        file=entry.path,
                        role=entry.role,
                        node=entry.node,
                        reason=str(exc),
                    )
                )
                continue

            if entry.kind is FileKind.METADATA:
                metadata.setdefault(key, {})[entry.base] = parse_metadata(
                    entry.base,
                    text,
                    content_backed_up=(entry.role, entry.node, entry.base) in backed_up,
                )
                continue

            if entry.kind is not FileKind.CONFIG:
                continue

            parsed = self._parse_config(
                cluster, entry, text, siblings, failures, disagreements
            )
            if parsed is not None:
                grouped.setdefault(key, {})[entry.path] = parsed

        keys = sorted(
            set(grouped) | set(metadata),
            key=lambda item: (item[0].value, item[1] or ""),
        )
        nodes = [
            NodeConfig(
                role=role,
                node=node,
                files=grouped.get((role, node), {}),
                metadata=metadata.get((role, node), {}),
            )
            for role, node in keys
        ]

        version, source = _detect_version(manifest.sep_version, nodes)
        return ClusterSnapshot(
            cluster=cluster,
            nodes=nodes,
            sep_version=version,
            sep_version_source=source,
            backed_up_at=manifest.backed_up_at,
            backup_age_days=self._age_days(manifest.backed_up_at),
            parse_failures=failures,
            unknown_roles=self._repository.layout(cluster).unknown_roles,
            parse_disagreements=disagreements,
        )

    def _parse_config(
        self,
        cluster: str,
        entry: BackupFile,
        text: str,
        siblings: dict[tuple[Role, str | None, str], BackupFile],
        failures: list[ParseFailure],
        disagreements: list[str],
    ) -> ParsedFile | None:
        """Parse one config, using the pipeline's `.json` form where it helps.

        The raw file stays authoritative wherever it parses, because only it
        carries line numbers and a finding that cannot cite a line is one the
        reader must take on trust. The sibling earns its place two ways: as a
        fallback when our parser fails, and as a cross-check when it does not
        -- a disagreement means one of the two parses is wrong about what the
        cluster is running, which is worth surfacing rather than hiding.
        """
        sibling = siblings.get((entry.role, entry.node, entry.base))
        sibling_text: str | None = None
        if sibling is not None and accepts_parsed_sibling(entry.path):
            try:
                sibling_text = self._repository.read(cluster, sibling)
            except (ConfigFileNotFoundError, OSError):
                sibling_text = None

        try:
            parsed = parse_config(entry.path, text)
        except ConfigParseError as exc:
            if sibling_text is None:
                failures.append(
                    ParseFailure(
                        file=entry.path,
                        role=entry.role,
                        node=entry.node,
                        reason=str(exc),
                    )
                )
                return None
            try:
                recovered = parse_parsed_json(entry.path, sibling_text)
            except ConfigParseError:
                failures.append(
                    ParseFailure(
                        file=entry.path,
                        role=entry.role,
                        node=entry.node,
                        reason=str(exc),
                    )
                )
                return None
            disagreements.append(
                f"{entry.label}: unreadable, fell back to the pipeline's parsed "
                f"copy (no line numbers, so findings cite the file only)"
            )
            return recovered

        if sibling_text is not None:
            try:
                theirs = parse_parsed_json(entry.path, sibling_text)
            except ConfigParseError:
                return parsed
            for note in disagreements_between(parsed, theirs):
                disagreements.append(f"{entry.label}: {note}")
        return parsed

    def _resolve_version(
        self, snapshot: ClusterSnapshot, context: InjectedContext | None
    ) -> tuple[str | None, str]:
        """Settle on a SEP version before evaluation begins.

        A detected version always wins. A hint is used only when detection
        found nothing, and the result is labelled ``hint`` so the parent can
        see that version-gated results rest on something the parent supplied
        rather than on the backup.
        """
        if snapshot.sep_version:
            return snapshot.sep_version, snapshot.sep_version_source
        if context is not None and context.sep_version_hint:
            return context.sep_version_hint, "hint"
        return None, "unknown"

    def _locate(
        self, snapshot: ClusterSnapshot, role: Role, file: str, node: str | None
    ) -> tuple[str | None, ParsedFile] | None:
        wanted = file.strip().lstrip("/")
        for candidate in snapshot.nodes_for(role):
            if node is not None and candidate.node != node:
                continue
            for path, parsed in sorted(candidate.files.items()):
                if path == wanted or path.endswith(f"/{wanted}"):
                    return candidate.node, parsed
        return None

    def _scope_properties(self, scopes: list[Scope]) -> set[str]:
        return self._catalog.properties_covered(scopes)

    def _coverage(
        self,
        snapshot: ClusterSnapshot,
        scopes: list[Scope],
        *,
        evaluated: int,
        skipped_version: int = 0,
        skipped_missing: int = 0,
        checked: set[str] | None = None,
        blind_spots: list[str] | None = None,
    ) -> Coverage:
        uncovered = sorted(
            snapshot.keys_in_scope(scopes) - self._scope_properties(scopes)
        )
        return Coverage.build(
            rules_evaluated=evaluated,
            rules_skipped_version=skipped_version,
            rules_skipped_missing_input=skipped_missing,
            files_parsed=sum(len(node.files) for node in snapshot.nodes),
            nodes_seen=len(snapshot.nodes),
            parse_failures=list(snapshot.parse_failures),
            properties_uncovered=uncovered[:25],
            blind_spots=list(blind_spots or []),
        )

    def _age_days(self, backed_up_at: str | None) -> int | None:
        if not backed_up_at:
            return None
        try:
            stamp = datetime.fromisoformat(backed_up_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return max(0, (self._clock() - stamp).days)


def _detect_version(
    manifest_version: str | None, nodes: list[NodeConfig]
) -> tuple[str | None, str]:
    """Find the SEP version, preferring the manifest over the config."""
    if manifest_version:
        return manifest_version, "backup_manifest"
    for node in nodes:
        for parsed in node.files.values():
            for key in ("starburst.version", "sep.version", "trino.version"):
                if key in parsed.values:
                    return parsed.values[key], "config"
    return None, "unknown"


_VERSION_SOURCES: dict[str, VersionSource] = {
    "backup_manifest": "backup_manifest",
    "config": "config",
    "hint": "hint",
}


def _version_source(source: str) -> VersionSource:
    """Narrow a detected source to the values the wire type allows."""
    return _VERSION_SOURCES.get(source, "unknown")


def _rank(differences: list[PropertyDifference]) -> list[PropertyDifference]:
    """Lead with real divergence; keep expected differences at the back."""
    order = {"unexpected": 0, "unknown": 1, "expected": 2}
    return sorted(
        differences, key=lambda d: (order[d.classification], d.role.value, d.key)
    )
