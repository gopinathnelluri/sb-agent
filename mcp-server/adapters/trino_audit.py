"""Completed-query history read from a Starburst audit catalog.

Every cluster keeps its own audit catalog, so a cluster name selects a
connection rather than filtering rows. Connections are opened lazily, one per
cluster, and reused: a conversation about one cluster costs one connection,
and asking about a second does not disturb the first.

Endpoints come from a mapping -- ``TRINO_CLUSTER_ENDPOINTS`` as JSON, or a
mounted file -- keyed by the same cluster names the rest of the tools use. A
single ``TRINO_HOST`` still works and is then used for every cluster, which is
what a one-cluster deployment or a test wants.

Each connection authenticates over HTTPS with an AD functional ID (LDAP basic
auth). Which columns the audit table uses is not hardcoded here -- it comes
from an ``AuditProfile``, so a schema change or a differently-configured
cluster is a YAML edit.

Credentials are read from environment variables or from files mounted by the
platform, never from anything in this repository, and never logged. Under
OpenShift the container runs as a random UID, so nothing here writes to disk.

``trino`` is imported lazily inside the constructor: importing this module
opens no connection and reads no credentials, which is what keeps it safe to
import from a test process.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from core.analysis.models import QueryInfo
from core.analysis.profile import AuditProfile
from core.analysis.sql.models import TableFacts

log = logging.getLogger(__name__)

HOST_ENV = "TRINO_HOST"
PORT_ENV = "TRINO_PORT"
ENDPOINTS_ENV = "TRINO_CLUSTER_ENDPOINTS"
ENDPOINTS_FILE_ENV = "TRINO_CLUSTER_ENDPOINTS_FILE"
USER_ENV = "TRINO_USER"
PASSWORD_ENV = "TRINO_PASSWORD"
PASSWORD_FILE_ENV = "TRINO_PASSWORD_FILE"
DEFAULT_PORT = 443

# A query id is opaque but well-formed; anything else never reaches SQL.
_MAX_QUERY_ID = 128


class TrinoConnectionError(RuntimeError):
    """The cluster could not be reached or authenticated against."""


def read_password() -> str:
    """Resolve the functional ID's password from a file or the environment.

    A mounted file is preferred: it is what OpenShift secrets provide, and it
    keeps the value out of the process environment where it could be picked
    up by a crash dump or a child process.
    """
    path = os.environ.get(PASSWORD_FILE_ENV)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TrinoConnectionError(
                f"Could not read the password file at {path}. Check the secret "
                f"is mounted and readable by the container's UID."
            ) from exc
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise TrinoConnectionError(
            f"No credential configured. Set {PASSWORD_FILE_ENV} to a mounted "
            f"secret path, or {PASSWORD_ENV}."
        )
    return password


def read_endpoints() -> dict[str, str]:
    """Cluster name -> host, or "host:port", from the environment.

    A mounted file is read first so the mapping can be managed as a ConfigMap
    and changed without redeploying. Unreadable or malformed content yields an
    empty mapping rather than an exception: the single-host fallback may still
    be configured, and failing at construction would take down every tool
    including the ones that never touch a cluster.
    """
    raw = ""
    path = os.environ.get(ENDPOINTS_FILE_ENV)
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            log.warning("Could not read the endpoint mapping at %s", path)
    raw = raw or os.environ.get(ENDPOINTS_ENV, "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Endpoint mapping is not valid JSON; ignoring it.")
        return {}
    if not isinstance(parsed, dict):
        log.warning("Endpoint mapping must be an object of cluster -> host.")
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


class TrinoAuditRepository:
    """Reads query history from an audit catalog on a Starburst cluster."""

    def __init__(
        self,
        profile: AuditProfile,
        *,
        endpoints: dict[str, str] | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        connection: Any = None,
    ) -> None:
        self.profile = profile
        self._endpoints = endpoints if endpoints is not None else read_endpoints()
        self._host = host or os.environ.get(HOST_ENV, "")
        self._port = port or int(os.environ.get(PORT_ENV, DEFAULT_PORT))
        self._user = user or os.environ.get(USER_ENV, "")
        self._password = password
        # One connection per cluster. `connection=` injects a single stand-in
        # for every cluster, which is what tests want.
        self._injected = connection
        self._connections: dict[str, Any] = {}

        if connection is None and not (self._endpoints or self._host):
            raise TrinoConnectionError(
                f"No clusters configured. Set {ENDPOINTS_ENV} to a JSON object "
                f"of cluster name to host, or {HOST_ENV} for a single cluster."
            )
        if connection is None and not self._user:
            raise TrinoConnectionError(
                f"No functional ID configured. Pass user= or set {USER_ENV}."
            )

    def _endpoint(self, cluster: str) -> tuple[str, int]:
        """Resolve a cluster name to a host and port.

        Falls back to the single configured host when no mapping is given, so
        a one-cluster deployment needs no mapping at all. An unknown name with
        no fallback is an error that names what is configured -- the likely
        cause is a cluster that exists in the backup inventory but has no
        audit endpoint yet, and that is worth saying rather than timing out.
        """
        target = self._endpoints.get(cluster) or self._host
        if not target:
            known = ", ".join(sorted(self._endpoints)) or "none"
            raise TrinoConnectionError(
                f"No audit endpoint configured for cluster {cluster!r}. "
                f"Configured clusters: {known}."
            )
        host, _, port = target.partition(":")
        return host, int(port) if port.isdigit() else self._port

    # -- connection ------------------------------------------------------

    def _connect(self, cluster: str) -> Any:
        """Open a connection to one cluster, or reuse an open one."""
        if self._injected is not None:
            return self._injected
        existing = self._connections.get(cluster)
        if existing is not None:
            return existing
        host, port = self._endpoint(cluster)
        try:
            import trino
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise TrinoConnectionError(
                "The trino package is required to read query history."
            ) from exc

        password = self._password or read_password()
        # http_scheme is https because LDAP basic auth must never go in clear.
        connection = trino.dbapi.connect(  # type: ignore[no-untyped-call]
            host=host,
            port=port,
            user=self._user,
            http_scheme="https",
            auth=trino.auth.BasicAuthentication(self._user, password),
            catalog=self.profile.table.catalog,
            schema=self.profile.table.schema,
        )
        log.info(
            "Connected to %s (%s:%s) as %s (audit table %s)",
            cluster,
            host,
            port,
            self._user,
            self.profile.table.qualified,
        )
        self._connections[cluster] = connection
        return connection

    def _rows(
        self, cluster: str, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        """Run a query on one cluster and return rows keyed by column name."""
        cursor = self._connect(cluster).cursor()
        try:
            cursor.execute(sql, params) if params else cursor.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    # -- QueryRepository -------------------------------------------------

    def get_query(self, cluster: str, query_id: str) -> QueryInfo | None:
        """Fetch one completed query by id.

        Returns ``None`` when the id is unknown -- which usually means it
        aged out of retention rather than that it never existed. Callers
        should say so.
        """
        if not _valid_query_id(query_id):
            return None

        columns = ", ".join(f'"{c}"' for c in self.profile.select_list())
        where = [f'"{self.profile.columns["query_id"]}" = ?']
        params: list[Any] = [query_id]
        if self.profile.cluster_column and cluster:
            where.append(f'"{self.profile.cluster_column}" = ?')
            params.append(cluster)

        sql = (
            f"SELECT {columns} FROM {self.profile.table.qualified} "
            f"WHERE {' AND '.join(where)} LIMIT 1"
        )
        rows = self._rows(cluster, sql, tuple(params))
        if not rows:
            return None
        return self.profile.to_query_info(
            rows[0], cluster=cluster, source=f"audit:{self.profile.name}"
        )

    def table_facts(
        self,
        cluster: str,
        table: str,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> TableFacts:
        """Discover a table's partition columns.

        Reads the column list of the Hive ``$partitions`` metadata table --
        those column names *are* the partition keys. Cheap (no rows fetched),
        and it is what upgrades a suspected SQL pattern to a confirmed one.

        Any failure yields empty facts: an unpartitioned table, an Iceberg
        table, or a permissions problem all mean "we could not confirm",
        which downstream is reported rather than guessed at.
        """
        if not catalog or not schema or not _valid_identifier(table):
            return TableFacts(name=table)
        if not (_valid_identifier(catalog) and _valid_identifier(schema)):
            return TableFacts(name=table)

        target = f'{catalog}.{schema}."{table}$partitions"'
        try:
            cursor = self._connect(cluster).cursor()
            try:
                cursor.execute(f"SELECT * FROM {target} LIMIT 0")
                partition_columns = frozenset(d[0] for d in (cursor.description or []))
            finally:
                cursor.close()
        except Exception:  # noqa: BLE001 - any failure means "cannot confirm"
            log.debug("No $partitions metadata for %s", target, exc_info=True)
            return TableFacts(name=table)

        return TableFacts(name=table, partition_columns=partition_columns)

    def previous_runs(
        self, cluster: str, query: QueryInfo, limit: int = 5
    ) -> list[QueryInfo]:
        """Earlier executions of the same SQL text.

        Matched on the exact statement rather than anything fuzzier. A
        dashboard or scheduled job sends byte-identical SQL every time, and
        those are precisely the queries where "it used to be fast" gets asked.
        An ad-hoc query typed slightly differently will not match, which is
        the right outcome -- a baseline drawn from a different query would be
        worse than no baseline at all.
        """
        sql_column = self.profile.columns.get("sql")
        ended_column = self.profile.columns.get("ended_at")
        if not sql_column or not query.sql:
            return []

        columns = ", ".join(f'"{c}"' for c in self.profile.select_list())
        where = [f'"{sql_column}" = ?', f'"{self.profile.columns["query_id"]}" <> ?']
        params: list[Any] = [query.sql, query.query_id]
        if self.profile.cluster_column and cluster:
            where.append(f'"{self.profile.cluster_column}" = ?')
            params.append(cluster)

        order = f'ORDER BY "{ended_column}" DESC' if ended_column else ""
        sql = (
            f"SELECT {columns} FROM {self.profile.table.qualified} "
            f"WHERE {' AND '.join(where)} {order} LIMIT {max(1, min(limit, 20))}"
        )
        try:
            rows = self._rows(cluster, sql, tuple(params))
        except Exception:  # noqa: BLE001 - history is a bonus, never a blocker
            log.debug("Could not look up previous runs", exc_info=True)
            return []
        return [
            self.profile.to_query_info(
                row, cluster=cluster, source=f"audit:{self.profile.name}"
            )
            for row in rows
        ]

    def retention_days(self, cluster: str) -> int | None:
        """How far back this cluster's audit table goes, when it can say."""
        column = self.profile.columns.get("ended_at")
        if not column:
            return None
        sql = (
            f'SELECT date_diff(\'day\', min("{column}"), max("{column}")) AS days '
            f"FROM {self.profile.table.qualified}"
        )
        try:
            rows = self._rows(cluster, sql)
        except Exception:  # noqa: BLE001 - a scan this wide may be denied
            log.debug("Could not determine retention window", exc_info=True)
            return None
        if not rows or rows[0].get("days") is None:
            return None
        return int(rows[0]["days"])


def _valid_query_id(value: str) -> bool:
    """Query ids are alphanumeric with underscores and hyphens."""
    return (
        bool(value)
        and len(value) <= _MAX_QUERY_ID
        and all(c.isalnum() or c in "_-" for c in value)
    )


def _valid_identifier(value: str) -> bool:
    """Catalog/schema/table names that are safe to interpolate."""
    return (
        bool(value)
        and len(value) <= 128
        and all(c.isalnum() or c in "_-" for c in value)
    )
