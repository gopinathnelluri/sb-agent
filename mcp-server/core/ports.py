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

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from core.analysis.models import QueryInfo
from core.analysis.sql.models import TableFacts
from core.config.layout import FileKind
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
    """One object in a backup, located but not yet read.

    ``path`` is everything beneath the host directory and is treated as
    opaque -- it mirrors the host's own filesystem, which differs by role and
    by deployment.

    ``kind`` says what the object is: the config itself, the pipeline's parsed
    ``.json`` form of it, or a ``.metadata.json`` holding ownership and
    permissions. ``base`` is the config all three share, which is how they are
    matched up.
    """

    role: Role
    path: str
    node: str | None = None
    kind: FileKind = FileKind.CONFIG
    base: str = ""

    def __post_init__(self) -> None:
        if not self.base:
            object.__setattr__(self, "base", self.path)

    @property
    def label(self) -> str:
        parts = [self.role.value, self.node, self.path]
        return "/".join(part for part in parts if part)


@dataclass(frozen=True)
class BackupLayout:
    """The shape of one cluster's backup, without reading any file.

    Answers "what is in here" cheaply -- a prefix listing, no object GETs --
    which is what makes a fleet of hundreds of clusters navigable.
    """

    cluster: str
    roles: dict[str, list[str]] = field(default_factory=dict)
    config_paths: dict[str, list[str]] = field(default_factory=dict)
    unknown_roles: dict[str, int] = field(default_factory=dict)
    total_objects: int = 0


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
        """Every object in a cluster's backup, across roles and nodes."""
        ...

    def layout(self, cluster: str) -> BackupLayout:
        """Describe the backup's shape without reading any file."""
        ...

    def read(self, cluster: str, file: BackupFile) -> str:
        """Read one file's text."""
        ...


@runtime_checkable
class QueryRepository(Protocol):
    """Read-only access to completed-query history and table metadata.

    Like ``ConfigRepository``, this is deliberately dumb: it fetches, it does
    not interpret. Detectors and SQL analysis run against whatever it returns,
    so the same analysis code works over an audit catalog today and a
    coordinator REST payload later.
    """

    def get_query(self, cluster: str, query_id: str) -> QueryInfo | None:
        """One completed query, or None when the id is unknown or expired."""
        ...

    def table_facts(
        self, table: str, catalog: str | None, schema: str | None
    ) -> TableFacts:
        """Partition columns and stats availability for one table.

        Used to promote a suspected SQL pattern to a confirmed one. Returns
        empty facts rather than raising when the table cannot be inspected.
        """
        ...

    def retention_days(self) -> int | None:
        """How far back the history goes, when the source can report it."""
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

    def describe_backup_layout(self, cluster: str) -> BackupLayout:
        """What is in a cluster's backup, without reading any file."""
        ...
