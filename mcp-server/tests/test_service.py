"""End-to-end tests of the service against the backup fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.local_backup import LocalBackupRepository
from core.errors import (
    ClusterNotFoundError,
    ConfigFileNotFoundError,
    UnredactedSecretError,
)
from core.models import ConfigEvidence, Role
from core.ports import BackupFile
from core.rules.schema import RuleCatalog
from core.scopes import Scope
from core.service import ConfigValidationService
from tests.conftest import ALL_SCOPES, FROZEN_NOW


class TestListClusters:
    def test_lists_every_backed_up_cluster(
        self, service: ConfigValidationService
    ) -> None:
        assert {c.name for c in service.list_clusters()} == {
            "clean-cluster",
            "drifted-cluster",
            "bare-cluster",
        }

    def test_reports_version_and_where_it_came_from(
        self, service: ConfigValidationService
    ) -> None:
        clean = next(c for c in service.list_clusters() if c.name == "clean-cluster")
        assert clean.sep_version == "429-e"
        assert clean.sep_version_source == "backup_manifest"

    def test_counts_nodes_per_role(self, service: ConfigValidationService) -> None:
        clean = next(c for c in service.list_clusters() if c.name == "clean-cluster")
        assert clean.node_counts == {"coordinator": 1, "worker": 2}

    def test_reports_backup_age(self, service: ConfigValidationService) -> None:
        clean = next(c for c in service.list_clusters() if c.name == "clean-cluster")
        assert clean.backup_age_days == 1

    def test_unknown_cluster_names_the_alternatives(
        self, service: ConfigValidationService
    ) -> None:
        with pytest.raises(ClusterNotFoundError, match="clean-cluster"):
            service.run_rules("nope", [Scope.MEMORY])


class TestRunRules:
    def test_a_clean_cluster_yields_no_findings(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("clean-cluster", ALL_SCOPES)
        assert result.findings == []

    def test_a_clean_result_is_marked_complete(
        self, service: ConfigValidationService
    ) -> None:
        """Empty findings only mean 'healthy' when coverage says rules ran."""
        coverage = service.run_rules("clean-cluster", ALL_SCOPES).coverage
        assert coverage.complete is True
        assert coverage.rules_evaluated > 0
        assert coverage.blind_spots == []

    def test_threshold_breach_is_found_with_evidence(
        self, service: ConfigValidationService
    ) -> None:
        result = service.run_rules("drifted-cluster", [Scope.MEMORY])
        finding = next(f for f in result.findings if f.rule_id == "SEP-MEM-002")
        assert finding.actual == "40GB"
        assert finding.expected is not None
        assert "24GB" in finding.expected
        evidence = finding.evidence[0]
        assert isinstance(evidence, ConfigEvidence)
        assert evidence.file == "config.properties"
        assert evidence.line is not None

    def test_findings_are_sorted_most_severe_first(
        self, service: ConfigValidationService
    ) -> None:
        findings = service.run_rules("drifted-cluster", ALL_SCOPES).findings
        ranks = [f.severity.rank for f in findings]
        assert ranks == sorted(ranks)

    def test_fingerprints_are_stable_across_runs(
        self, service: ConfigValidationService
    ) -> None:
        first = service.run_rules("drifted-cluster", ALL_SCOPES).findings
        second = service.run_rules("drifted-cluster", ALL_SCOPES).findings
        assert [f.fingerprint for f in first] == [f.fingerprint for f in second]

    def test_scope_limits_what_is_evaluated(
        self, service: ConfigValidationService
    ) -> None:
        memory_only = service.run_rules("drifted-cluster", [Scope.MEMORY])
        assert {f.domain for f in memory_only.findings} == {Scope.MEMORY}

    def test_node_drift_is_found_and_names_the_outlier(
        self, service: ConfigValidationService
    ) -> None:
        """The check per-node backup directories exist to make possible."""
        result = service.run_rules("drifted-cluster", [Scope.NODE_IDENTITY])
        finding = next(f for f in result.findings if f.rule_id == "SEP-NODE-001")
        assert finding.actual is not None
        assert "worker-02" in finding.actual
        assert any(
            isinstance(e, ConfigEvidence) and e.node == "worker-02"
            for e in finding.evidence
        )

    def test_absent_required_property_is_its_own_finding(
        self, service: ConfigValidationService
    ) -> None:
        """A missing property is a distinct outcome from a wrong one."""
        result = service.run_rules("bare-cluster", [Scope.JVM])
        finding = next(f for f in result.findings if f.rule_id == "SEP-JVM-001")
        assert finding.actual == "not set"

    def test_missing_inputs_are_reported_as_a_blind_spot(
        self, service: ConfigValidationService
    ) -> None:
        coverage = service.run_rules("bare-cluster", ALL_SCOPES).coverage
        assert coverage.complete is False
        assert coverage.rules_skipped_missing_input > 0

    def test_uncovered_properties_are_listed(
        self, service: ConfigValidationService
    ) -> None:
        coverage = service.run_rules("clean-cluster", ALL_SCOPES).coverage
        assert "http-server.http.port" in coverage.properties_uncovered

    def test_a_stale_backup_is_flagged(
        self, stale_backup: Path, catalog: RuleCatalog
    ) -> None:
        service = ConfigValidationService(
            LocalBackupRepository(stale_backup), catalog, clock=lambda: FROZEN_NOW
        )
        result = service.run_rules("clean-cluster", ALL_SCOPES)
        assert any("days old" in spot for spot in result.coverage.blind_spots)
        assert result.coverage.complete is False


class TestUnknownVersion:
    def test_version_gated_rules_are_skipped_and_reported(
        self, service_without_manifests: ConfigValidationService
    ) -> None:
        """The failure mode that would fake a clean bill of health."""
        result = service_without_manifests.run_rules("clean-cluster", ALL_SCOPES)
        assert result.sep_version is None
        assert result.coverage.complete is False
        assert any("SEP version" in spot for spot in result.coverage.blind_spots)

    def test_unconstrained_rules_still_run(
        self, service_without_manifests: ConfigValidationService
    ) -> None:
        result = service_without_manifests.run_rules("bare-cluster", [Scope.JVM])
        assert any(f.rule_id == "SEP-JVM-001" for f in result.findings)


class TestConfigSummary:
    def test_returns_resolved_properties(
        self, service: ConfigValidationService
    ) -> None:
        summary = service.get_config_summary("clean-cluster", [Scope.MEMORY])
        keys = {p.key for p in summary.properties}
        assert "query.max-memory-per-node" in keys

    def test_flags_drift_and_names_the_nodes(
        self, service: ConfigValidationService
    ) -> None:
        summary = service.get_config_summary("drifted-cluster", [Scope.JVM])
        drifted = next(
            p for p in summary.properties if p.key == "-Xmx" and p.role is Role.WORKER
        )
        assert drifted.consistent_across_nodes is False
        assert "worker-02" in drifted.differing_nodes
        assert summary.anomalies


class TestConfigDetail:
    def test_returns_file_content(self, service: ConfigValidationService) -> None:
        detail = service.get_config_detail(
            "clean-cluster", Role.WORKER, "config.properties"
        )
        assert "query.max-memory-per-node" in detail.content
        assert detail.redaction_verified is True

    def test_can_select_a_specific_node(self, service: ConfigValidationService) -> None:
        detail = service.get_config_detail(
            "drifted-cluster", Role.WORKER, "jvm.config", node="worker-02"
        )
        assert detail.node == "worker-02"
        assert "-Xmx64G" in detail.content

    def test_missing_file_raises(self, service: ConfigValidationService) -> None:
        with pytest.raises(ConfigFileNotFoundError):
            service.get_config_detail("clean-cluster", Role.WORKER, "nope.properties")

    def test_unredacted_secret_is_refused(
        self, tmp_path: Path, catalog: RuleCatalog
    ) -> None:
        """We do not trust the backup pipeline's redaction."""
        root = tmp_path / "backups" / "leaky" / "coordinator" / "coord-01"
        root.mkdir(parents=True)
        (root / "config.properties").write_text(
            "http-server.authentication.type=PASSWORD\n"
            "internal-communication.shared-secret=hunter2realsecret\n",
            encoding="utf-8",
        )
        service = ConfigValidationService(
            LocalBackupRepository(tmp_path / "backups"), catalog
        )
        with pytest.raises(UnredactedSecretError) as excinfo:
            service.get_config_detail("leaky", Role.COORDINATOR, "config.properties")
        assert "hunter2realsecret" not in str(excinfo.value)


class TestDiffClusters:
    def test_reports_differences(self, service: ConfigValidationService) -> None:
        diff = service.diff_clusters("clean-cluster", "drifted-cluster", [Scope.MEMORY])
        keys = {d.key for d in diff.differences}
        assert "query.max-memory-per-node" in keys

    def test_rule_governed_differences_lead(
        self, service: ConfigValidationService
    ) -> None:
        diff = service.diff_clusters(
            "clean-cluster", "drifted-cluster", [Scope.MEMORY, Scope.NODE_IDENTITY]
        )
        assert diff.differences[0].classification == "unexpected"

    def test_machine_identity_differences_are_expected(
        self, service: ConfigValidationService
    ) -> None:
        diff = service.diff_clusters("clean-cluster", "drifted-cluster", ALL_SCOPES)
        node_id = [d for d in diff.differences if d.key == "node.id"]
        assert all(d.classification == "expected" for d in node_id)


class TestCaching:
    def test_a_second_call_reuses_the_parsed_snapshot(
        self, repository: LocalBackupRepository, catalog: RuleCatalog
    ) -> None:
        reads = 0
        original = repository.read

        def counting_read(cluster: str, file: BackupFile) -> str:
            nonlocal reads
            reads += 1
            return original(cluster, file)

        repository.read = counting_read  # type: ignore[method-assign]
        service = ConfigValidationService(repository, catalog)

        service.run_rules("clean-cluster", ALL_SCOPES)
        after_first = reads
        service.run_rules("clean-cluster", ALL_SCOPES)
        assert reads == after_first


class TestDeterminism:
    def test_identical_input_yields_identical_output(
        self, repository: LocalBackupRepository, catalog: RuleCatalog
    ) -> None:
        """The property the whole design rests on."""
        one = ConfigValidationService(repository, catalog, clock=lambda: FROZEN_NOW)
        two = ConfigValidationService(
            LocalBackupRepository(repository.root),
            RuleCatalog.load(),
            clock=lambda: FROZEN_NOW,
        )
        assert one.run_rules("drifted-cluster", ALL_SCOPES) == two.run_rules(
            "drifted-cluster", ALL_SCOPES
        )
