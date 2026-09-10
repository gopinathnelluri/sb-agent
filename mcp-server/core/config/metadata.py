"""File metadata captured alongside each config.

The pipeline writes a ``<file>.metadata.json`` next to every backed-up config
holding the ownership and permissions it had on the host. Two things make it
worth reading.

First, permissions are an audit surface the config contents cannot express: a
world-readable file holding a connection string is a finding no property-level
rule would catch.

Second, a metadata file may exist for a config whose *contents* were too
sensitive to back up -- a keytab, a licence. Its presence tells us the file
is on the host and how it is protected, which is exactly what we want to
check, without the secret ever entering the backup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FileMetadata:
    """Ownership and permissions of one file on its host."""

    path: str
    owner: str | None = None
    group: str | None = None
    permissions: str | None = None
    size_bytes: int | None = None
    content_backed_up: bool = True

    @property
    def mode(self) -> int | None:
        """Permissions as an octal integer, or None if unreadable."""
        if not self.permissions:
            return None
        try:
            return int(self.permissions, 8)
        except ValueError:
            return None

    @property
    def world_readable(self) -> bool | None:
        """Whether any user on the host can read the file."""
        mode = self.mode
        return None if mode is None else bool(mode & 0o004)

    @property
    def group_writable(self) -> bool | None:
        mode = self.mode
        return None if mode is None else bool(mode & 0o020)


def parse_metadata(path: str, text: str, *, content_backed_up: bool) -> FileMetadata:
    """Read a ``.metadata.json`` file.

    Unknown keys are ignored rather than rejected: the pipeline may add fields
    over time, and refusing to read a metadata file because it grew a column
    would lose the permissions data we came for.
    """
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        return FileMetadata(path=path, content_backed_up=content_backed_up)
    if not isinstance(raw, dict):
        return FileMetadata(path=path, content_backed_up=content_backed_up)

    size = raw.get("size") if raw.get("size") is not None else raw.get("size_bytes")
    try:
        size_bytes = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_bytes = None

    return FileMetadata(
        path=path,
        owner=_text(raw.get("owner")),
        group=_text(raw.get("group")),
        permissions=_text(raw.get("permissions") or raw.get("mode")),
        size_bytes=size_bytes,
        content_backed_up=content_backed_up,
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value).strip() or None
