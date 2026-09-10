"""Config backups read from IBM Cloud Object Storage.

COS is S3-compatible, so this speaks S3 through boto3 against an explicit
endpoint. Object keys mirror the local layout:

    <prefix>/<cluster>/manifest.json
    <prefix>/<cluster>/<role>/<node>/<file>

Credentials come from the environment or from mounted files, never from a
file in this repository, and are never logged. Under OpenShift the container
runs as a random UID, so nothing here writes to disk or to ``$HOME``.

boto3 is imported lazily inside the constructor. Importing this module
therefore opens no connection and reads no credentials, which is what keeps
``adapters`` safe to import from a test process.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from core.config.layout import FileKind, classify_all, split_role_host
from core.errors import ClusterNotFoundError, ConfigFileNotFoundError
from core.models import Role
from core.parsers import is_parseable
from core.ports import BackupFile, BackupLayout, BackupManifest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

MANIFEST = "manifest.json"
DEFAULT_ENDPOINT_ENV = "COS_ENDPOINT"
DEFAULT_BUCKET_ENV = "COS_BUCKET"
DEFAULT_PREFIX_ENV = "COS_PREFIX"


class CosBackupRepository:
    """Reads config backups from an S3-compatible COS bucket."""

    def __init__(
        self,
        bucket: str | None = None,
        *,
        endpoint_url: str | None = None,
        prefix: str = "",
        client: Any = None,
    ) -> None:
        self.bucket = bucket or os.environ.get(DEFAULT_BUCKET_ENV, "")
        if not self.bucket:
            raise ValueError(
                f"No COS bucket configured. Pass bucket= or set {DEFAULT_BUCKET_ENV}."
            )
        self.prefix = (prefix or os.environ.get(DEFAULT_PREFIX_ENV, "")).strip("/")
        self._client = client or self._build_client(
            endpoint_url or os.environ.get(DEFAULT_ENDPOINT_ENV)
        )

    @staticmethod
    def _build_client(endpoint_url: str | None) -> Any:
        """Construct the S3 client.

        Credentials are resolved by boto3 from the environment or a mounted
        credentials file. HMAC keys are assumed; a fleet using IAM API keys
        needs an ``ibm-cos-sdk`` client passed in as ``client=`` instead.
        """
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(
                "boto3 is required to read from COS. Install it, or pass a "
                "pre-built client as client=."
            ) from exc
        if not endpoint_url:
            raise ValueError(
                f"No COS endpoint configured. Pass endpoint_url= or set "
                f"{DEFAULT_ENDPOINT_ENV}."
            )
        return boto3.client("s3", endpoint_url=endpoint_url)

    # -- discovery ------------------------------------------------------

    def list_clusters(self) -> list[str]:
        root = f"{self.prefix}/" if self.prefix else ""
        response = self._client.list_objects_v2(
            Bucket=self.bucket, Prefix=root, Delimiter="/"
        )
        return sorted(
            item["Prefix"][len(root) :].strip("/")
            for item in response.get("CommonPrefixes", [])
        )

    def manifest(self, cluster: str) -> BackupManifest:
        self._require_cluster(cluster)
        try:
            body = self._get(f"{self._cluster_prefix(cluster)}/{MANIFEST}")
        except ConfigFileNotFoundError:
            return BackupManifest(cluster=cluster)
        try:
            raw = json.loads(body)
        except json.JSONDecodeError:
            return BackupManifest(cluster=cluster)
        if not isinstance(raw, dict):
            return BackupManifest(cluster=cluster)
        return BackupManifest(
            cluster=cluster,
            sep_version=_text(raw.get("sep_version")),
            backed_up_at=_text(raw.get("backed_up_at")),
        )

    def list_files(self, cluster: str) -> list[BackupFile]:
        """Every object worth reading, classified.

        One prefix listing for the whole cluster; classification happens on
        the keys, so this costs no object GETs.
        """
        self._require_cluster(cluster)
        base = self._cluster_prefix(cluster)

        by_host: dict[tuple[Role, str], list[str]] = {}
        for key in self._keys(f"{base}/"):
            split = split_role_host(key[len(base) + 1 :])
            if split is None:
                continue
            role_name, host, path = split
            role = Role.parse(role_name)
            if role is None:
                continue
            by_host.setdefault((role, host), []).append(path)

        files: list[BackupFile] = []
        for (role, host), paths in sorted(
            by_host.items(), key=lambda item: (item[0][0].value, item[0][1])
        ):
            for entry in classify_all(sorted(paths), is_parseable):
                if entry.kind is FileKind.UNRECOGNISED:
                    continue
                files.append(
                    BackupFile(
                        role=role,
                        path=entry.path,
                        node=host,
                        kind=entry.kind,
                        base=entry.base,
                    )
                )
        return files

    def layout(self, cluster: str) -> BackupLayout:
        """Describe the backup's shape from object keys alone.

        Prefix listings only -- no object is fetched. That is what makes this
        usable across a fleet of hundreds of clusters, where reading even one
        file per cluster would run to thousands of requests.
        """
        self._require_cluster(cluster)
        base = self._cluster_prefix(cluster)

        roles: dict[str, set[str]] = {}
        paths: dict[str, set[str]] = {}
        unknown: dict[str, set[str]] = {}
        total = 0

        for key in self._keys(f"{base}/"):
            total += 1
            split = split_role_host(key[len(base) + 1 :])
            if split is None:
                continue
            role_name, host, path = split
            if Role.parse(role_name) is None:
                unknown.setdefault(role_name, set()).add(host)
                continue
            roles.setdefault(role_name, set()).add(host)
            paths.setdefault(role_name, set()).add(
                path.rsplit("/", 1)[0] if "/" in path else "."
            )

        return BackupLayout(
            cluster=cluster,
            roles={k: sorted(v) for k, v in sorted(roles.items())},
            config_paths={k: sorted(v) for k, v in sorted(paths.items())},
            unknown_roles={k: len(v) for k, v in sorted(unknown.items())},
            total_objects=total,
        )

    def read(self, cluster: str, file: BackupFile) -> str:
        self._require_cluster(cluster)
        parts = [self._cluster_prefix(cluster), file.role.value]
        if file.node:
            parts.append(file.node)
        parts.append(file.path)
        try:
            return self._get("/".join(parts))
        except ConfigFileNotFoundError as exc:
            raise ConfigFileNotFoundError(
                cluster, file.role.value, file.path, file.node
            ) from exc

    # -- internals ------------------------------------------------------

    def _cluster_prefix(self, cluster: str) -> str:
        return f"{self.prefix}/{cluster}" if self.prefix else cluster

    def _require_cluster(self, cluster: str) -> None:
        if cluster not in self._cluster_names():
            raise ClusterNotFoundError(cluster, self._cluster_names())

    @lru_cache(maxsize=1)  # noqa: B019 - bounded to one entry, lives with the client
    def _cluster_names(self) -> list[str]:
        return self.list_clusters()

    def _keys(self, prefix: str) -> Iterator[str]:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                yield str(item["Key"])
            if not response.get("IsTruncated"):
                return
            token = response.get("NextContinuationToken")

    def _get(self, key: str) -> str:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore raises client-specific errors
            raise ConfigFileNotFoundError("", "", key, None) from exc
        body = response["Body"].read()
        return str(body.decode("utf-8", errors="replace"))


def _text(value: object) -> str | None:
    return None if value is None else str(value)
