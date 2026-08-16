"""Config backups read from a local directory tree.

The layout mirrors what the Ansible pipeline writes to COS:

    <root>/<cluster>/manifest.json
    <root>/<cluster>/coordinator/<node>/config.properties
    <root>/<cluster>/worker/<node>/jvm.config
    <root>/<cluster>/worker/<node>/catalog/hive.properties

A role directory may also hold files directly, with no node level, for a
backup taken before per-node directories existed. Both are supported so the
older layout keeps working rather than failing to load.

Used for tests, for local development against a redacted sample, and as a
mounted volume in a cluster that cannot reach COS.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.errors import ClusterNotFoundError, ConfigFileNotFoundError
from core.models import Role
from core.parsers import is_parseable
from core.ports import BackupFile, BackupManifest

MANIFEST = "manifest.json"


class LocalBackupRepository:
    """Reads config backups from a directory tree."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- discovery ------------------------------------------------------

    def list_clusters(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def manifest(self, cluster: str) -> BackupManifest:
        directory = self._cluster_dir(cluster)
        path = directory / MANIFEST
        if not path.is_file():
            return BackupManifest(cluster=cluster)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BackupManifest(cluster=cluster)
        if not isinstance(raw, dict):
            return BackupManifest(cluster=cluster)
        return BackupManifest(
            cluster=cluster,
            sep_version=_text(raw.get("sep_version")),
            backed_up_at=_text(raw.get("backed_up_at")),
        )

    def list_files(self, cluster: str) -> list[BackupFile]:
        directory = self._cluster_dir(cluster)
        files: list[BackupFile] = []

        for role in Role:
            role_dir = directory / role.value
            if not role_dir.is_dir():
                continue
            for path in sorted(role_dir.rglob("*")):
                if not path.is_file() or not is_parseable(path.name):
                    continue
                relative = path.relative_to(role_dir)
                node, sub_path = _split_node(relative, role_dir)
                files.append(BackupFile(role=role, path=sub_path, node=node))

        return files

    def read(self, cluster: str, file: BackupFile) -> str:
        path = self._path_for(cluster, file)
        if not path.is_file():
            raise ConfigFileNotFoundError(
                cluster, file.role.value, file.path, file.node
            )
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigFileNotFoundError(
                cluster, file.role.value, file.path, file.node
            ) from exc

    # -- internals ------------------------------------------------------

    def _cluster_dir(self, cluster: str) -> Path:
        directory = self.root / _safe(cluster)
        if not directory.is_dir():
            raise ClusterNotFoundError(cluster, self.list_clusters())
        return directory

    def _path_for(self, cluster: str, file: BackupFile) -> Path:
        base = self._cluster_dir(cluster) / file.role.value
        if file.node:
            base = base / _safe(file.node)
        return base.joinpath(*[_safe(part) for part in file.path.split("/")])


def _split_node(relative: Path, role_dir: Path) -> tuple[str | None, str]:
    """Decide whether the first path segment is a node directory.

    A backup with per-node directories nests one level deeper than one
    without. Distinguishing them structurally avoids needing a flag in the
    manifest that an older pipeline would not have written.
    """
    parts = relative.parts
    if len(parts) == 1:
        return None, parts[0]
    if (role_dir / parts[0]).is_dir() and parts[0] != "catalog":
        return parts[0], "/".join(parts[1:])
    return None, "/".join(parts)


def _safe(component: str) -> str:
    """Reject path traversal in any component that came from a caller."""
    cleaned = component.strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Unsafe path component: {component!r}")
    return cleaned


def _text(value: object) -> str | None:
    return None if value is None else str(value)
