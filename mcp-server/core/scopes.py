"""Config domains that a validation request can be scoped to.

The parent agent's model picks scopes straight from this enum. There is no
``resolve_scope`` tool and no free-text intent parsing on this side of the
boundary: interpreting what a human meant is the parent's job, and it has a
model for it. Ours is to make the choices legible and the evaluation exact.

``SCOPE_GUIDE`` is rendered into the tool descriptions so the parent's model
sees the symptom -> scope mapping at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scope(StrEnum):
    """A config domain. Always passed explicitly; never inferred."""

    MEMORY = "memory"
    JVM = "jvm"
    SPILL = "spill"
    NODE_IDENTITY = "node_identity"
    RESOURCE_GROUPS = "resource_groups"
    HIVE_CATALOG = "hive_catalog"
    HMS = "hms"
    S3 = "s3"
    RANGER = "ranger"
    AUTH = "auth"
    FILE_SECURITY = "file_security"


@dataclass(frozen=True)
class ScopeInfo:
    """Everything the parent's model needs to choose a scope correctly."""

    scope: Scope
    summary: str
    symptoms: tuple[str, ...]
    files: tuple[str, ...]


SCOPES: dict[Scope, ScopeInfo] = {
    Scope.MEMORY: ScopeInfo(
        scope=Scope.MEMORY,
        summary="Query and node memory limits, and how they relate to heap size.",
        symptoms=("out of memory", "query exceeded memory limit", "OOM killed"),
        files=("config.properties",),
    ),
    Scope.JVM: ScopeInfo(
        scope=Scope.JVM,
        summary="JVM heap sizing, GC collector and flags.",
        symptoms=("long GC pauses", "heap sizing", "JVM crash", "coordinator stalls"),
        files=("jvm.config",),
    ),
    Scope.SPILL: ScopeInfo(
        scope=Scope.SPILL,
        summary="Spill-to-disk enablement, spill paths and their capacity.",
        symptoms=("spilling", "disk full during query", "spill path missing"),
        files=("config.properties",),
    ),
    Scope.NODE_IDENTITY: ScopeInfo(
        scope=Scope.NODE_IDENTITY,
        summary=(
            "Node environment, node id and coordinator/worker role flags. "
            "Detects nodes that drifted from the rest of the fleet."
        ),
        symptoms=("node not joining cluster", "worker missing", "environment mismatch"),
        files=("node.properties", "config.properties"),
    ),
    Scope.RESOURCE_GROUPS: ScopeInfo(
        scope=Scope.RESOURCE_GROUPS,
        summary="Queueing, concurrency limits and resource group assignment.",
        symptoms=("queries stuck in queue", "long queue time", "cluster contention"),
        files=("resource-groups.json", "config.properties"),
    ),
    Scope.HIVE_CATALOG: ScopeInfo(
        scope=Scope.HIVE_CATALOG,
        summary="Hive connector catalog settings, including caching and split sizing.",
        symptoms=("hive catalog slow", "too many splits", "slow listing"),
        files=("catalog/*.properties",),
    ),
    Scope.HMS: ScopeInfo(
        scope=Scope.HMS,
        summary="Hive Metastore endpoint, timeouts and connection pooling.",
        symptoms=("metastore timeout", "table not found", "slow DDL"),
        files=("catalog/*.properties", "hive-site.xml"),
    ),
    Scope.S3: ScopeInfo(
        scope=Scope.S3,
        summary="Object storage endpoint, path style, retries and connection limits.",
        symptoms=("S3 slow", "403 from object storage", "connection pool timeout"),
        files=("catalog/*.properties", "hive-site.xml"),
    ),
    Scope.RANGER: ScopeInfo(
        scope=Scope.RANGER,
        summary="Ranger plugin wiring and policy refresh behaviour.",
        symptoms=("access denied", "policy not applied", "stale permissions"),
        files=("ranger/*.properties", "ranger/policies.json"),
    ),
    Scope.AUTH: ScopeInfo(
        scope=Scope.AUTH,
        summary="Authentication and TLS on the coordinator.",
        symptoms=("cannot log in", "certificate error", "authentication failed"),
        files=("config.properties",),
    ),
    Scope.FILE_SECURITY: ScopeInfo(
        scope=Scope.FILE_SECURITY,
        summary=(
            "Ownership and permissions of config files on disk, including "
            "files whose contents are too sensitive to back up."
        ),
        symptoms=(
            "world-readable config",
            "wrong file owner",
            "permissions audit",
            "keytab exposed",
        ),
        files=("*.metadata.json",),
    ),
}


def render_scope_guide() -> str:
    """Render the scope table for embedding in a tool description.

    Tool descriptions are prompts. This puts the symptom -> scope mapping in
    front of the parent's model at the moment it has to choose.
    """
    lines = ["| scope | use when the user mentions |", "|---|---|"]
    lines.extend(
        f"| `{info.scope.value}` | {info.summary} "
        f"Symptoms: {', '.join(info.symptoms)}. |"
        for info in SCOPES.values()
    )
    return "\n".join(lines)


SCOPE_GUIDE = render_scope_guide()
