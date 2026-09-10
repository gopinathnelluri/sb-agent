"""Understanding the shape of a backup.

The Ansible pipeline writes one directory per host and, beneath it, a copy of
that host's real filesystem layout:

    configs/<cluster>/<role>/<host>/etc/starburst/config.properties
    configs/<cluster>/<role>/<host>/etc/starburst/config.properties.json
    configs/<cluster>/<role>/<host>/etc/starburst/config.properties.metadata.json
    configs/<cluster>/hms/<host>/opt/sbhms/conf/hive-site.xml

Everything after the host is treated as an opaque path. It differs by role and
by deployment -- Starburst under ``etc/starburst``, a metastore somewhere else
entirely -- and hardcoding any of it would break the first time a fleet is
laid out differently. The only structural assumption is
``<role>/<host>/<rest>``, which the pipeline guarantees.

Each config file may appear three times: the file itself, a ``.json`` parsed
form, and a ``.metadata.json`` carrying owner, group and permissions. Files
too sensitive to back up may still have their ``.metadata.json`` present,
which is how we can tell a keytab or licence exists without seeing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

METADATA_SUFFIX = ".metadata.json"
PARSED_SUFFIX = ".json"


class FileKind(StrEnum):
    """What a file in the backup is."""

    CONFIG = "config"
    PARSED_CONFIG = "parsed_config"
    METADATA = "metadata"
    UNRECOGNISED = "unrecognised"


@dataclass(frozen=True)
class ClassifiedFile:
    """One object in the backup, and what it represents.

    ``base`` is the config file this entry belongs to -- the same value for
    ``config.properties``, ``config.properties.json`` and
    ``config.properties.metadata.json``, which is what lets the three be
    matched up.
    """

    path: str
    kind: FileKind
    base: str

    @property
    def filename(self) -> str:
        return self.base.rsplit("/", 1)[-1]


def classify(path: str, *, is_parseable_name: bool) -> ClassifiedFile:
    """Decide what a backup object is from its name.

    ``is_parseable_name`` is supplied by the caller (the parser registry) so
    this module stays free of parser knowledge.
    """
    if path.endswith(METADATA_SUFFIX):
        return ClassifiedFile(
            path=path,
            kind=FileKind.METADATA,
            base=path[: -len(METADATA_SUFFIX)],
        )
    return ClassifiedFile(
        path=path,
        kind=FileKind.CONFIG if is_parseable_name else FileKind.UNRECOGNISED,
        base=path,
    )


def classify_all(paths: list[str], is_parseable: object) -> list[ClassifiedFile]:
    """Classify a directory listing, resolving ``.json`` parsed siblings.

    A ``.json`` file is a parsed form only when a recognised config file sits
    beside it under the same base name. That distinction matters: a genuine
    JSON config such as ``resource-groups.json`` has no such sibling and must
    not be mistaken for one.
    """
    check = is_parseable  # callable(str) -> bool, injected to avoid a cycle
    present = set(paths)
    out: list[ClassifiedFile] = []

    for path in paths:
        if path.endswith(METADATA_SUFFIX):
            out.append(classify(path, is_parseable_name=False))
            continue

        if path.endswith(PARSED_SUFFIX):
            stem = path[: -len(PARSED_SUFFIX)]
            # A parsed sibling only if the raw file is actually there.
            if stem in present and check(stem):  # type: ignore[operator]
                out.append(
                    ClassifiedFile(path=path, kind=FileKind.PARSED_CONFIG, base=stem)
                )
                continue

        out.append(classify(path, is_parseable_name=bool(check(path))))  # type: ignore[operator]

    return out


def split_role_host(relative: str) -> tuple[str, str, str] | None:
    """Split ``<role>/<host>/<rest>`` from a cluster-relative object key.

    Returns ``None`` for anything shallower than that, such as the cluster
    manifest. Everything after the host is left untouched -- this function
    deliberately makes no assumption about how deep a config sits or what it
    is called.
    """
    parts = relative.split("/")
    if len(parts) < 3:
        return None
    return parts[0], parts[1], "/".join(parts[2:])
