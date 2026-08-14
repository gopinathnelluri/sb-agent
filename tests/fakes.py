"""A ``ConfigService`` that returns canned data.

Exists so the tool surface can be tested without COS, a cluster or a network.
It is the reason ``core.ports.ConfigService`` is a Protocol: the MCP layer
cannot tell this apart from the real implementation, which is exactly the
property we want when the real one arrives.
"""

from __future__ import annotations

from core.errors import ClusterNotFoundError, UnredactedSecretError
from core.models import (
    ClusterDiff,
    ClusterInfo,
    ConfigDetail,
    ConfigEvidence,
    ConfigSummary,
    Coverage,
    Evidence,
    Finding,
    InjectedContext,
    ParseFailure,
    PropertyDifference,
    PropertyValue,
    RationaleSource,
    Role,
    RuleRunResult,
    Severity,
    compute_fingerprint,
    sort_findings,
)
from core.scopes import Scope

KNOWN_CLUSTERS = ("prod-analytics", "staging-analytics")


class FakeConfigService:
    """Canned implementation of the use case 1 service."""

    def __init__(self, *, complete: bool = True, findings: bool = True) -> None:
        self.complete = complete
        self.emit_findings = findings
        self.calls: list[tuple[str, object]] = []

    def _check(self, cluster: str) -> None:
        if cluster not in KNOWN_CLUSTERS:
            raise ClusterNotFoundError(cluster, list(KNOWN_CLUSTERS))

    def _coverage(self) -> Coverage:
        if self.complete:
            return Coverage.build(rules_evaluated=12, files_parsed=9, nodes_seen=4)
        return Coverage.build(
            rules_evaluated=0,
            rules_skipped_version=12,
            files_parsed=9,
            nodes_seen=4,
            parse_failures=[
                ParseFailure(
                    file="jvm.config",
                    role=Role.WORKER,
                    node="worker-03",
                    reason="unreadable byte at offset 41",
                )
            ],
            blind_spots=["SEP version could not be detected; all rules were gated out"],
        )

    def list_clusters(self) -> list[ClusterInfo]:
        self.calls.append(("list_clusters", None))
        return [
            ClusterInfo(
                name="prod-analytics",
                sep_version="429",
                sep_version_source="backup_manifest",
                backed_up_at="2026-08-10T02:00:00Z",
                backup_age_days=3,
                roles=[Role.COORDINATOR, Role.WORKER, Role.HMS],
                node_counts={"coordinator": 1, "worker": 24, "hms": 1},
            ),
            ClusterInfo(
                name="staging-analytics",
                sep_version=None,
                sep_version_source="unknown",
                backed_up_at="2026-06-01T02:00:00Z",
                backup_age_days=73,
                roles=[Role.COORDINATOR, Role.WORKER],
                node_counts={"coordinator": 1, "worker": 3},
            ),
        ]

    def get_config_summary(self, cluster: str, scopes: list[Scope]) -> ConfigSummary:
        self._check(cluster)
        self.calls.append(("get_config_summary", (cluster, tuple(scopes))))
        return ConfigSummary(
            cluster=cluster,
            scopes=list(scopes),
            sep_version="429",
            properties=[
                PropertyValue(
                    key="query.max-memory-per-node",
                    value="40GB",
                    role=Role.WORKER,
                    source_file="worker/config.properties",
                ),
                PropertyValue(
                    key="node.environment",
                    value="production",
                    role=Role.WORKER,
                    source_file="worker/node.properties",
                    consistent_across_nodes=False,
                    differing_nodes={"worker-17": "prod"},
                ),
            ],
            anomalies=["node.environment differs on 1 of 24 workers"],
            source_files=["worker/config.properties", "worker/node.properties"],
            coverage=self._coverage(),
        )

    def run_rules(
        self,
        cluster: str,
        scopes: list[Scope],
        context: InjectedContext | None = None,
    ) -> RuleRunResult:
        self._check(cluster)
        self.calls.append(("run_rules", (cluster, tuple(scopes), context)))
        findings: list[Finding] = []
        if self.emit_findings:
            evidence: list[Evidence] = [
                ConfigEvidence(
                    file="worker/config.properties",
                    role=Role.WORKER,
                    node="worker-01",
                    line=27,
                )
            ]
            findings = [
                Finding(
                    rule_id="SEP-MEM-002",
                    fingerprint=compute_fingerprint(
                        "SEP-MEM-002", cluster, "query.max-memory-per-node", evidence
                    ),
                    severity=Severity.HIGH,
                    scope=Scope.MEMORY,
                    summary="query.max-memory-per-node exceeds 30% of JVM heap",
                    rationale="Leaves headroom for non-query JVM allocation.",
                    rationale_source=RationaleSource.RULE_CATALOG,
                    evidence=evidence,
                    subject="query.max-memory-per-node",
                    actual="40GB",
                    expected="<= 24GB (30% of 80GB heap)",
                    deviation=0.67,
                )
            ]
        return RuleRunResult(
            cluster=cluster,
            scopes=list(scopes),
            sep_version="429",
            findings=sort_findings(findings),
            coverage=self._coverage(),
            backup_age_days=3,
        )

    def get_config_detail(
        self, cluster: str, role: Role, file: str, node: str | None = None
    ) -> ConfigDetail:
        self._check(cluster)
        self.calls.append(("get_config_detail", (cluster, role, file, node)))
        if file == "leaky.properties":
            raise UnredactedSecretError(cluster, file, ["hive.s3.aws-secret-key"])
        return ConfigDetail(
            cluster=cluster,
            role=role,
            file=file,
            node=node,
            content="query.max-memory-per-node=40GB\n",
            redaction_verified=True,
            redactions_applied=[],
        )

    def diff_clusters(
        self, cluster_a: str, cluster_b: str, scopes: list[Scope]
    ) -> ClusterDiff:
        self._check(cluster_a)
        self._check(cluster_b)
        self.calls.append(("diff_clusters", (cluster_a, cluster_b, tuple(scopes))))
        return ClusterDiff(
            cluster_a=cluster_a,
            cluster_b=cluster_b,
            scopes=list(scopes),
            differences=[
                PropertyDifference(
                    key="query.max-memory-per-node",
                    role=Role.WORKER,
                    value_a="40GB",
                    value_b="24GB",
                    classification="unexpected",
                ),
                PropertyDifference(
                    key="node.id",
                    role=Role.WORKER,
                    value_a="prod-worker-01",
                    value_b="stg-worker-01",
                    classification="expected",
                ),
            ],
            coverage=self._coverage(),
        )
