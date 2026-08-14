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

from core.errors import ClusterNotFoundError, ConfigFileNotFoundError
from core.models import Role
from core.parsers import is_parseable
from core.ports import BackupFile, BackupManifest

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
        self._require_cluster(cluster)
        base = self._cluster_prefix(cluster)
        files: list[BackupFile] = []

        for key in self._keys(f"{base}/"):
            relative = key[len(base) + 1 :]
            parts = relative.split("/")
            if len(parts) < 2 or not is_parseable(parts[-1]):
                continue
            try:
                role = Role(parts[0])
            except ValueError:
                continue
            node, path = _split_node(parts[1:])
            files.append(BackupFile(role=role, path=path, node=node))

        return sorted(
            files, key=lambda item: (item.role.value, item.node or "", item.path)
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


def _split_node(parts: list[str]) -> tuple[str | None, str]:
    """Treat a single intermediate segment as a node directory."""
    if len(parts) == 1:
        return None, parts[0]
    if parts[0] == "catalog":
        return None, "/".join(parts)
    return parts[0], "/".join(parts[1:])


def _text(value: object) -> str | None:
    return None if value is None else str(value)
