"""Checks that evaluate a file's metadata rather than its contents.

A config file can hold entirely correct settings and still be a finding, if
every account on the host can read it. Those problems live in ownership and
permissions, which the pipeline captures in ``*.metadata.json`` beside each
backed-up file.

The same machinery covers files whose contents were deliberately not backed
up. A keytab's metadata says it exists and how it is protected; that is the
whole check, and it works without the secret ever entering the backup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from core.config.metadata import FileMetadata


@dataclass(frozen=True)
class FileCheckOutcome:
    """The result of applying one metadata check to one file."""

    passed: bool
    actual: str
    expected: str
    unavailable: bool = False

    @classmethod
    def cannot_tell(cls, expected: str) -> FileCheckOutcome:
        """Permissions were not recorded, so the check could not run."""
        return cls(
            passed=True,
            actual="permissions not recorded",
            expected=expected,
            unavailable=True,
        )


class FileCheck(Protocol):
    """A single test against one file's metadata."""

    kind: ClassVar[str]

    def evaluate(self, metadata: FileMetadata) -> FileCheckOutcome: ...

    def describe(self) -> str: ...


FILE_CHECK_KINDS: dict[str, Callable[[dict[str, Any]], FileCheck]] = {}


def register(kind: str) -> Callable[[type[FileCheck]], type[FileCheck]]:
    def decorate(cls: type[FileCheck]) -> type[FileCheck]:
        FILE_CHECK_KINDS[kind] = cls.from_spec  # type: ignore[attr-defined]
        return cls

    return decorate


@register("not_world_readable")
@dataclass(frozen=True)
class NotWorldReadable:
    """No permission bit granting read access to other users."""

    kind: ClassVar[str] = "not_world_readable"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> NotWorldReadable:
        return cls()

    def evaluate(self, metadata: FileMetadata) -> FileCheckOutcome:
        readable = metadata.world_readable
        if readable is None:
            return FileCheckOutcome.cannot_tell(self.describe())
        return FileCheckOutcome(
            passed=not readable,
            actual=metadata.permissions or "unknown",
            expected=self.describe(),
        )

    def describe(self) -> str:
        return "not readable by other users"


@register("not_group_writable")
@dataclass(frozen=True)
class NotGroupWritable:
    """No permission bit granting write access to the owning group."""

    kind: ClassVar[str] = "not_group_writable"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> NotGroupWritable:
        return cls()

    def evaluate(self, metadata: FileMetadata) -> FileCheckOutcome:
        writable = metadata.group_writable
        if writable is None:
            return FileCheckOutcome.cannot_tell(self.describe())
        return FileCheckOutcome(
            passed=not writable,
            actual=metadata.permissions or "unknown",
            expected=self.describe(),
        )

    def describe(self) -> str:
        return "not writable by the owning group"


@register("max_mode")
@dataclass(frozen=True)
class MaxMode:
    """Permissions must grant no more than the stated mode.

    Compared bit by bit rather than numerically: 0640 is not "less than" 0600
    in any useful sense, but it does grant a bit that 0600 does not.
    """

    limit: int
    literal: str
    kind: ClassVar[str] = "max_mode"

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> MaxMode:
        raw = str(spec.get("mode") or "")
        try:
            return cls(limit=int(raw, 8), literal=raw)
        except ValueError as exc:
            raise ValueError(
                f"'mode' must be an octal string such as '0600', got {raw!r}."
            ) from exc

    def evaluate(self, metadata: FileMetadata) -> FileCheckOutcome:
        mode = metadata.mode
        if mode is None:
            return FileCheckOutcome.cannot_tell(self.describe())
        extra = mode & ~self.limit
        return FileCheckOutcome(
            passed=extra == 0,
            actual=metadata.permissions or "unknown",
            expected=self.describe(),
        )

    def describe(self) -> str:
        return f"no permissions beyond {self.literal}"


def build_file_check(spec: dict[str, Any]) -> FileCheck:
    """Construct a metadata check from its YAML mapping."""
    if not isinstance(spec, dict) or "kind" not in spec:
        raise ValueError("A file_check must be a mapping with a 'kind' field.")
    kind = str(spec["kind"])
    factory = FILE_CHECK_KINDS.get(kind)
    if factory is None:
        known = ", ".join(sorted(FILE_CHECK_KINDS))
        raise ValueError(f"Unknown file_check kind {kind!r}. Known kinds: {known}.")
    unknown = set(spec) - {"kind", "files", "mode"}
    if unknown:
        raise ValueError(
            f"file_check kind {kind!r} does not accept: {', '.join(sorted(unknown))}."
        )
    return factory(spec)
