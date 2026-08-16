"""The interface the MCP layer is allowed to depend on.

``mcp_server`` imports this Protocol and nothing else from ``core``'s
internals. That keeps the MCP wrapper thin by construction -- it has no way
to reach parsing, rule evaluation or storage -- and it is what makes the
eventual LangGraph -> ADK move a matter of writing a new adapter rather than
touching business logic.

Implemented by ``core.service.ConfigValidationService``. Tests substitute a
fake, which is why the tool layer can be covered without COS or a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.models import (
    ClusterDiff,
    ClusterInfo,
    ConfigDetail,
    ConfigSummary,
    InjectedContext,
    Role,
    RuleRunResult,
)
from core.scopes import Scope


@dataclass(frozen=True)
class BackupFile:
    """One config file in a backup, located but not yet read.

    ``node`` is ``None`` for a role stored without per-node directories.
    ``path`` is relative to the role (or node) directory, so it keeps any
    sub-directory such as ``catalog/hive.properties``.
    """

    role: Role
    path: str
    node: str | None = None

    @property
    def label(self) -> str:
        parts = [self.role.value, self.node, self.path]
        return "/".join(part for part in parts if part)


@dataclass(frozen=True)
class BackupManifest:
    """What the backup pipeline recorded about a cluster snapshot."""

    cluster: str
    sep_version: str | None = None
    backed_up_at: str | None = None


@runtime_checkable
class ConfigRepository(Protocol):
    """Read-only access to config backups.

    Deliberately dumb: it lists and reads bytes, and knows nothing about
    parsing, rules or scopes. That is what lets the same service run against
    a local directory in tests and IBM COS in production without a branch
    anywhere in ``core``.
    """

    def list_clusters(self) -> list[str]:
        """Names of every cluster with a backup present."""
        ...

    def manifest(self, cluster: str) -> BackupManifest:
        """Pipeline metadata for a cluster: version and backup timestamp."""
        ...

    def list_files(self, cluster: str) -> list[BackupFile]:
        """Every config file in a cluster's backup, across roles and nodes."""
        ...

    def read(self, cluster: str, file: BackupFile) -> str:
        """Read one file's text."""
        ...


@runtime_checkable
class ConfigService(Protocol):
    """Use case 1: cluster configuration validation.

    Every method is synchronous, deterministic and free of model calls. Given
    the same backup bytes, each returns the same result forever.
    """

    def list_clusters(self) -> list[ClusterInfo]:
        """Inventory of clusters that have a config backup."""
        ...

    def get_config_summary(self, cluster: str, scopes: list[Scope]) -> ConfigSummary:
        """Resolved key properties for the given scopes, with drift flagged."""
        ...

    def run_rules(
        self,
        cluster: str,
        scopes: list[Scope],
        context: InjectedContext | None = None,
    ) -> RuleRunResult:
        """Evaluate the rule catalog.

        Implementations must compute findings before reading ``context``, and
        must apply enrichment as a separate pass over the finished list.
        """
        ...

    def get_config_detail(
        self, cluster: str, role: Role, file: str, node: str | None = None
    ) -> ConfigDetail:
        """Full text of one config file, after outbound redaction checks."""
        ...

    def diff_clusters(
        self, cluster_a: str, cluster_b: str, scopes: list[Scope]
    ) -> ClusterDiff:
        """Compare two clusters within the given scopes."""
        ...
